"""Composition root: builds every adapter from Settings through the factory registries.

This is the ONLY module that knows concrete adapter classes are chosen by name; everything else depends on
ports. API, worker, scheduler and CLI all build a Container from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from rag_os.application.ports import (
    ConfigRepository,
    EmbeddingProvider,
    LlmProvider,
    MessageQueue,
    RawDocumentStore,
    Retriever,
    SearchIndex,
)
from rag_os.application.services.access_policy import AccessPolicyEngine
from rag_os.application.services.chunker import TokenChunker
from rag_os.application.services.claims import ClaimsMapper
from rag_os.application.services.index_schema import IndexDocumentMapper, build_schema
from rag_os.application.services.profile_guard import ProfileGuard
from rag_os.application.services.tagging import TagResolver
from rag_os.application.use_cases.answer_query import AnswerQuery
from rag_os.application.use_cases.discover import DiscoverSource
from rag_os.application.use_cases.process_item import ProcessItem
from rag_os.application.use_cases.purge import Purge
from rag_os.application.use_cases.scheduler import Reconcile, SchedulerTick
from rag_os.domain.access import AccessPolicy
from rag_os.domain.classification import FacetSchema, PathRules
from rag_os.domain.embedding import EmbeddingProfile
from rag_os.domain.ingestion import SourcesFile
from rag_os.infrastructure.auth.jwt_validator import IssuerConfig, JwtValidator
from rag_os.infrastructure.registry import (
    CLASSIFIERS,
    CONFIG_REPOS,
    EMBEDDERS,
    LLMS,
    QUEUES,
    RAW_STORES,
    RETRIEVERS,
    SEARCH_INDEXES,
)
from rag_os.infrastructure.secrets import KeyVaultSecretResolver
from rag_os.infrastructure.settings import Settings
from rag_os.infrastructure.sources.factory import SourceFactory
from rag_os.infrastructure.state.sql_store import SqlStateStore

log = logging.getLogger(__name__)
DEV_ISSUER = "rag-os-dev"


@dataclass
class DomainConfig:
    policy: AccessPolicy
    facets: FacetSchema
    path_rules: PathRules
    sources: SourcesFile
    etags: dict[str, str] = field(default_factory=dict)


def load_domain_config(repo: ConfigRepository) -> DomainConfig:
    etags = {k: repo.read_raw(k)[1] for k in ("sources", "access-policy", "facets", "path-rules")}
    return DomainConfig(repo.load_access_policy(), repo.load_facets(), repo.load_path_rules(), repo.load_sources(),
                        etags)


class Container:
    def __init__(self, settings: Settings) -> None:
        self.secrets = KeyVaultSecretResolver(settings.key_vault_url)
        self.settings = settings.resolve_secrets(self.secrets)
        s = self.settings
        self.config: ConfigRepository = CONFIG_REPOS.create(
            s.config_store, config_dir=s.config_dir, account_url=s.blob_account_url, container=s.config_container,
            connection_string=s.blob_connection_string)
        self.profile: EmbeddingProfile = self.config.load_embedding_profile(s.embedding_profile)
        self.index_name = s.active_index or self.profile.index_name(s.index_prefix, s.index_domain)
        self.guard = ProfileGuard(self.profile)
        if self.profile.provider == "azure_openai" and not s.aoai_embed_deployment:
            # Logged, not raised. Refusing to construct would crash-loop the API and take away the one surface
            # that can explain the problem - /api/readyz reports it, and `rag-os doctor` prints it in full.
            log.error("embedding profile '%s' uses Azure OpenAI but AOAI_EMBED_DEPLOYMENT is not set; every "
                      "embedding call will fail. Set DeployAoaiEmbedding = $true and re-run 05-foundry.ps1.",
                      s.embedding_profile)
        self._apply(load_domain_config(self.config))
        self._closables: list[Any] = []

    # ------------------------------------------------------------------ domain configuration (hot reloadable)

    def _apply(self, dc: DomainConfig) -> None:
        self.domain = dc
        self.engine = AccessPolicyEngine(dc.policy, dc.facets)
        self.claims = ClaimsMapper(dc.policy)
        self.mapper = IndexDocumentMapper(dc.policy, dc.facets)
        self.tagger = TagResolver(dc.facets, dc.path_rules)
        self.schema = build_schema(self.index_name, self.profile, dc.policy, dc.facets, self.settings.search_compression)
        self.__dict__.pop("answer", None)
        self.__dict__.pop("processor", None)
        self.__dict__.pop("discover", None)

    async def reload_config(self, *, ensure_index: bool = True) -> bool:
        """Reload YAML config if any ETag changed. New policy attributes/facets are added to the index in place."""
        dc = await asyncio.to_thread(load_domain_config, self.config)
        if dc.etags == self.domain.etags:
            return False
        self._apply(dc)
        if ensure_index:
            await self.index.ensure_index(self.schema)
        log.info("domain configuration reloaded", extra={"etags": dc.etags})
        return True

    # ------------------------------------------------------------------ infrastructure (lazy)

    @cached_property
    def state(self) -> SqlStateStore:
        # Schema is owned by Alembic (applied by `rag-os bootstrap`); create_all is only a fallback for
        # environments shipped without the migrations directory.
        return SqlStateStore(self.settings.state_db_url, entra_auth=self.settings.pg_entra_auth,
                             create=_migration_config(self.settings.state_db_url, self.settings.pg_entra_auth) is None)

    @cached_property
    def index(self) -> SearchIndex:
        s = self.settings
        idx: SearchIndex = SEARCH_INDEXES.create(
            s.search_backend, index_name=self.index_name, endpoint=s.search_endpoint, semantic=s.search_semantic,
            db_url=s.state_db_url, entra_auth=s.pg_entra_auth)
        self._closables.append(idx)
        return idx

    @cached_property
    def queue(self) -> MessageQueue:
        s = self.settings
        q: MessageQueue = QUEUES.create(
            s.queue, namespace=s.servicebus_namespace, queue_priority=s.queue_priority, queue_bulk=s.queue_bulk,
            db_url=s.state_db_url, entra_auth=s.pg_entra_auth, max_delivery=s.queue_max_delivery,
            lock_seconds=s.queue_lock_seconds)
        self._closables.append(q)
        return q

    @cached_property
    def raw(self) -> RawDocumentStore:
        s = self.settings
        return RAW_STORES.create(  # type: ignore[no-any-return]
            s.raw_store, raw_dir=s.raw_dir, account_url=s.blob_account_url,
            connection_string=s.blob_connection_string, container=s.raw_container,
            exports_container=s.exports_container)

    def _embedder(self, pool: str) -> EmbeddingProvider:
        s, p = self.settings, self.profile
        url = s.tei_query_url if pool == "query" else s.tei_ingest
        e: EmbeddingProvider = EMBEDDERS.create(
            p.provider, p, base_url=url, endpoint=s.aoai_endpoint, deployment=s.aoai_embed_deployment,
            api_version=s.aoai_api_version, api_key=s.aoai_api_key,
            batch_size=s.ingest_embed_batch, concurrency=max(2, s.ingest_max_concurrency))
        self._closables.append(e)
        return e

    @cached_property
    def embed_query(self) -> EmbeddingProvider:
        return self._embedder("query")

    @cached_property
    def embed_ingest(self) -> EmbeddingProvider:
        """The query pool's provider when both pools address the same thing, otherwise its own.

        The condition used to be tei-specific, so an `azure_openai` profile built two clients against ONE
        deployment: two credential chains, two token caches - and because /api/readyz asks both pools to
        describe themselves, two *billed* embedding calls per readiness check. There is no second endpoint for
        AOAI to point at: the deployment is whatever AOAI_EMBED_DEPLOYMENT names.
        """
        if self.profile.provider == "azure_openai":
            # One deployment, named by AOAI_EMBED_DEPLOYMENT. There is no second endpoint to point at, so a
            # second client is a second credential chain and a second billed info() probe for the same answer.
            return self.embed_query
        if self.profile.provider == "tei" and self.settings.tei_ingest == self.settings.tei_query_url:
            return self.embed_query
        # `fake` deliberately keeps two instances. Sharing would be harmless but it also merges their call
        # counters, and the tests use the ingest counter to prove that re-tagging does not re-embed.
        return self._embedder("ingest")

    def _llm(self, name: str, role: str = "answer") -> LlmProvider:
        """One provider for one role. Role picks the model, mirroring _embedder(pool) picking the URL."""
        s = self.settings
        answer = role == "answer"
        llm: LlmProvider = LLMS.create(
            name, endpoint=s.aoai_endpoint,
            deployment=s.aoai_chat_deployment if answer else s.aoai_utility,
            api_version=s.aoai_api_version,
            api_key=s.aoai_api_key if name == "aoai" else s.claude_api_key, resource=s.claude_foundry_resource,
            model=s.claude_model if answer else s.claude_utility, effort=s.claude_effort)
        self._closables.append(llm)
        return llm

    def _roles_share_a_model(self, provider: str) -> bool:
        """Whether both roles resolve to the same model on this provider.

        The provider name alone is not enough: LLM_ANSWER and LLM_UTILITY can both be "aoai" and still point at
        different deployments. Answering that question with `==` on the provider is what would silently ignore a
        cheaper utility deployment and bill every condense call at the answer model's rate.
        """
        s = self.settings
        if provider == "aoai":
            return s.aoai_utility == s.aoai_chat_deployment
        if provider == "claude":
            return s.claude_utility == s.claude_model
        return True  # "fake" has no model to differ on

    @cached_property
    def llm_answer(self) -> LlmProvider:
        return self._llm(self.settings.llm_answer)

    @cached_property
    def llm_utility(self) -> LlmProvider:
        s = self.settings
        if s.llm_utility == s.llm_answer and self._roles_share_a_model(s.llm_utility):
            return self.llm_answer
        return self._llm(s.llm_utility, role="utility")

    @cached_property
    def llm_fallback(self) -> LlmProvider | None:
        # Substitutes for a refused *answer*, so it must use the answer model, not the utility one.
        s = self.settings
        if s.llm_answer == "claude" and s.aoai_endpoint and s.aoai_chat_deployment:
            return self._llm("aoai", role="answer")
        return None

    @cached_property
    def retriever(self) -> Retriever:
        s = self.settings
        return RETRIEVERS.create(  # type: ignore[no-any-return]
            s.retriever, index=self.index, embedder=self.embed_query, candidates=s.retrieval_candidates,
            min_reranker_score=s.retrieval_min_reranker_score if s.search_backend == "azure" else 0.0,
            min_score=s.retrieval_min_score,
            semantic=s.search_semantic)

    @cached_property
    def jwt(self) -> JwtValidator:
        s = self.settings
        issuers: list[IssuerConfig] = []
        if s.dev_auth_enabled and s.dev_jwt_key:
            issuers.append(IssuerConfig("dev", DEV_ISSUER, s.dev_jwt_audience, ("HS256",), key=s.dev_jwt_key,
                                        max_lifetime_s=s.dev_max_token_lifetime_s))
        if s.entra_tenant_id and s.entra_audience:
            tid = s.entra_tenant_id
            issuers.append(IssuerConfig(
                "entra",
                # One tenant, both spellings it may stamp - a v2 access token carries the first, a v1 token the
                # second (trailing slash included: it is part of the claim). See IssuerConfig for why.
                (f"https://login.microsoftonline.com/{tid}/v2.0", f"https://sts.windows.net/{tid}/"),
                s.entra_audiences, ("RS256",),
                # The v2.0 JWKS serves the signing keys for both token versions; PyJWKClient selects on `kid`.
                jwks_url=f"https://login.microsoftonline.com/{tid}/discovery/v2.0/keys",
                required=("exp", "iat", "iss", "aud")))
        return JwtValidator(issuers)

    @cached_property
    def source_factory(self) -> SourceFactory:
        return SourceFactory(self.secrets, self.settings)

    # ------------------------------------------------------------------ use cases

    @cached_property
    def answer(self) -> AnswerQuery:
        s = self.settings
        return AnswerQuery(engine=self.engine, facets=self.domain.facets, retriever=self.retriever,
                           llm=self.llm_answer, utility_llm=self.llm_utility, fallback_llm=self.llm_fallback,
                           top_k=s.retrieval_top_k, history_turns=s.answer_history_turns,
                           max_output_tokens=s.llm_max_output_tokens)

    @cached_property
    def discover(self) -> DiscoverSource:
        return DiscoverSource(self.state, self.queue, self.raw, self.tagger, embedding_fp=self.guard.fp)

    @cached_property
    def purge(self) -> Purge:
        return Purge(self.state, self.raw, self.index)

    @cached_property
    def index_semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(max(1, self.settings.ingest_index_concurrency))

    @cached_property
    def processor(self) -> ProcessItem:
        from rag_os.infrastructure.parsers import parser_for

        s = self.settings
        classifier = CLASSIFIERS.create(
            s.classifier, embedder=self.embed_ingest, llm=self.llm_utility, min_score=s.classifier_min_score,
            margin=s.classifier_margin, token_budget=s.classifier_llm_token_budget)
        return ProcessItem(
            state=self.state, raw=self.raw, parser_for=parser_for, chunker=TokenChunker(),
            embedder=self.embed_ingest, index=self.index, mapper=self.mapper, engine=self.engine,
            facets=self.domain.facets, tagger=self.tagger, classifier=classifier, profile=self.profile,
            guard=self.guard,
            index_semaphore=self.index_semaphore, index_batch=s.ingest_index_batch, max_file_mb=s.ingest_max_file_mb)

    def scheduler(self) -> SchedulerTick:
        reconcile = Reconcile(self.state, self.queue, self.settings.ingest_stale_minutes)
        return SchedulerTick(self.state, self.discover, reconcile, self.source_factory.create)

    # ------------------------------------------------------------------ lifecycle

    async def bootstrap(self) -> dict[str, Any]:
        """Idempotent: apply migrations, ensure the index schema, record the embedding profile."""
        migrations = await asyncio.to_thread(self._migrate)
        _ = self.state
        _ = self.queue  # sql queue creates its table when migrations are unavailable
        await self.index.ensure_index(self.schema)
        stored = await self.index.read_profile()
        record = self.guard.profile_record()
        if stored is None:
            await self.index.write_profile(record)
            action = "profile recorded"
        elif stored.get("fingerprint") != self.guard.fp:
            from rag_os.domain.errors import ProfileMismatch

            raise ProfileMismatch(f"index {self.index_name} holds profile {stored.get('fingerprint')}, "
                                  f"configuration is {self.guard.fp}; use a new index (ACTIVE_INDEX) and re-ingest")
        else:
            action = "profile verified"
        # Reported, not raised. By the time we get here the three things nothing else can do are done -
        # migrations, the index, the recorded profile - and a typo in sources.yaml is not a reason to throw them
        # away. Raising here meant the job reported "Failed", 08 stopped, and an operator reasonably concluded
        # the index had never been built. A broken source still stops ITS OWN ingestion, so it is surfaced
        # loudly: in the job's output, and as a [warn] per source in step 08.
        source_problems: list[str] = []
        for cfg in self.domain.sources.sources:
            try:
                self.source_factory.validate(cfg)
            except Exception as e:
                source_problems.append(f"{cfg.id} ({cfg.type}): {e}")
        if source_problems:
            log.error("source configuration problems; ingestion for these sources will not run",
                      extra={"problems": source_problems})
        return {"index": self.index_name, "profile_fingerprint": self.guard.fp, "action": action,
                "source_problems": source_problems,
                "migrations": migrations, "fields": len(self.schema.fields),
                "sources": [s.id for s in self.domain.sources.sources]}

    def _migrate(self) -> str:
        cfg = _migration_config(self.settings.state_db_url, self.settings.pg_entra_auth)
        if cfg is None:
            return "skipped (no migrations directory; tables created directly)"
        from alembic import command

        command.upgrade(cfg, "head")
        return "upgraded to head"

    async def aclose(self) -> None:
        for c in reversed(self._closables):
            with contextlib.suppress(Exception):  # shutdown must never fail
                await c.aclose()


def _migration_config(db_url: str, entra_auth: bool) -> Any:
    """Locate alembic.ini + migrations/ (repo checkout, container image, or installed alongside the package)."""
    from pathlib import Path

    from alembic.config import Config

    candidates = [Path.cwd(), Path("/app"), Path(__file__).resolve().parents[2]]
    for root in candidates:
        ini, scripts = root / "alembic.ini", root / "migrations"
        if ini.is_file() and (scripts / "env.py").is_file():
            cfg = Config(str(ini))
            cfg.set_main_option("script_location", str(scripts))
            cfg.set_main_option("sqlalchemy.url", db_url)
            cfg.set_main_option("rag_os.entra_auth", "true" if entra_auth else "false")
            return cfg
    return None


def config_digest(dc: DomainConfig) -> str:
    return hashlib.sha256("|".join(f"{k}={v}" for k, v in sorted(dc.etags.items())).encode()).hexdigest()[:12]
