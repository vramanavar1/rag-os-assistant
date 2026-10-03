"""Runtime settings (environment variables). Every variable is documented in Deployment.md.

Secrets: any value of the form ``kv://<secret-name>`` is resolved from Azure Key Vault
(KEY_VAULT_URL) at startup. In Azure Container Apps secrets arrive already resolved through
``keyvaultref:`` + ``secretref:`` so no kv:// indirection is needed there.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Used to tell an App ID URI whose last segment is an app id (api://<guid>, api://<tid>/<guid>) from one
# that is not (https://contoso.com/api). Only the former has a bare-GUID spelling to derive.
_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- general
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    service_name: str = "rag-api"
    key_vault_url: str | None = None
    # Read by azure-identity itself, not by this code: it is what makes DefaultAzureCredential pick the
    # USER-assigned managed identity. Declared here so the dependency is visible and can be checked at startup -
    # no workload has a system-assigned identity to fall back on, so an empty value means a 403 from whichever
    # Azure service is called first, with nothing pointing at the cause.
    azure_client_id: str | None = None

    # --- configuration repository (sources.yaml, access-policy.yaml, facets.yaml, path-rules.yaml)
    config_store: Literal["filesystem", "blob"] = "filesystem"
    config_dir: str = "./config"
    config_container: str = "config"

    # --- storage
    blob_account_url: str | None = None  # https://<account>.blob.core.windows.net
    blob_connection_string: str | None = None  # local Azurite only
    raw_store: Literal["filesystem", "blob"] = "filesystem"
    raw_dir: str = "./.data/raw"
    raw_container: str = "raw-docs"
    exports_container: str = "exports"

    # --- state store (PostgreSQL in Azure; sqlite allowed for tests)
    state_db_url: str = "sqlite:///./.data/state.db"
    pg_entra_auth: bool = False  # use an Entra token as the PostgreSQL password

    # --- queue
    queue: Literal["in_memory", "sql", "servicebus"] = "sql"
    queue_lock_seconds: int = 300
    servicebus_namespace: str | None = None  # <ns>.servicebus.windows.net
    queue_priority: str = "ingest-priority"
    queue_bulk: str = "ingest-bulk"
    queue_max_delivery: int = 5

    # --- search
    search_backend: Literal["azure", "local", "in_memory"] = "local"
    search_endpoint: str | None = None
    index_prefix: str = "kb"
    index_domain: str = "enterprise"
    active_index: str | None = None  # override; default derived from profile fingerprint
    search_semantic: bool = True
    search_compression: Literal["none", "scalar", "binary"] = "scalar"
    in_memory_index_path: str | None = "./.data/index.jsonl"

    # --- embeddings
    embedding_profile: str = "qwen3-0.6b-1024"
    tei_query_url: str | None = "http://localhost:8081"
    tei_ingest_url: str | None = None  # defaults to tei_query_url
    aoai_endpoint: str | None = None  # https://<foundry-account>.openai.azure.com
    aoai_api_version: str = "2024-10-21"
    aoai_embed_deployment: str | None = None
    aoai_chat_deployment: str | None = None
    aoai_utility_deployment: str | None = None  # falls back to aoai_chat_deployment
    aoai_api_key: str | None = None  # optional; keyless (Entra) is the default

    # --- LLMs
    llm_answer: Literal["aoai", "claude", "fake"] = "fake"
    llm_utility: Literal["aoai", "claude", "fake"] = "fake"
    claude_foundry_resource: str | None = None
    claude_model: str = "claude-sonnet-5"  # the answer role
    claude_utility_model: str | None = None  # falls back to claude_model
    claude_api_key: str | None = None  # optional; Entra token provider is the default
    claude_effort: str | None = None  # low|medium|high|xhigh|max ; None = API default
    llm_max_output_tokens: int = 8000  # includes reasoning/thinking tokens on reasoning models

    # --- retrieval / answering
    retriever: Literal["direct"] = "direct"
    retrieval_top_k: int = 8
    retrieval_candidates: int = 50
    retrieval_min_reranker_score: float = 1.2
    retrieval_min_score: float = 0.0
    answer_history_turns: int = 6

    # --- auth (Microsoft Entra ID is the only production issuer; dev tokens are local-only)
    dev_auth_enabled: bool = True
    dev_jwt_key: str | None = "dev-only-insecure-key-change-me-0123456789abcdef"
    dev_max_token_lifetime_s: int = 3600
    dev_jwt_audience: str = "rag-os"  # audience of dev tokens (Entra tokens use entra_audience)
    entra_tenant_id: str | None = None
    entra_audience: str | None = None  # api://<app-id> or the bare app id; both spellings are accepted
    entra_client_id: str | None = None  # the SPA's client id, handed to the browser for MSAL
    entra_api_scope: str | None = None  # api://<app-id>/access_as_user - what the browser asks for
    embed_origins: str = "http://localhost:8080"

    # --- directory administration (Settings (Security): assigning attributes and roles to a person)
    # "none" is the default because this holds tenant-wide Graph write permissions: it is opt-in per
    # deployment, never something a fresh environment quietly acquires.
    directory: Literal["none", "graph", "fake"] = "none"
    # The enterprise application's OBJECT id, which is not the app (client) id. App-role assignments hang off
    # it. ./infra/scripts/Set-EntraAppRegistration.ps1 records it in infra/env/<env>.outputs.json.
    entra_service_principal_object_id: str | None = None
    # The app id that OWNS the directory extensions, without which their property names cannot be composed.
    # Normally the same as entra_audience, but that may be a non-GUID form, so it can be set explicitly.
    entra_extension_app_id: str | None = None

    # --- ingestion
    ingest_max_concurrency: int = 4
    ingest_embed_batch: int = 32
    ingest_index_batch: int = 500
    ingest_index_concurrency: int = 2
    ingest_max_file_mb: int = 100
    ingest_receive_batch: int = 8
    ingest_stale_minutes: int = 30
    ingest_controls_refresh_s: int = 30

    # --- classification
    classifier: Literal["embedding", "embedding+llm", "none"] = "embedding"
    classifier_min_score: float = 0.30
    classifier_margin: float = 0.03
    classifier_llm_token_budget: int = 200_000

    # --- telemetry
    applicationinsights_connection_string: str | None = None
    otel_enabled: bool = True

    # --- query traces (Admin > Query traces). Traces hold question and answer text and are readable by
    # administrators only; they are purged after the retention period.
    query_trace_enabled: bool = True
    query_trace_retention_days: int = 30
    # On a refused question, re-run the search without the access clause to show what access withheld. One
    # extra search per refusal; its results go into the trace only, never to the person who asked.
    query_trace_near_miss: bool = True
    query_trace_max_hits: int = 15
    # Health thresholds. Rates count problem verdicts only - a correct "not in the documents" is not a problem.
    query_health_problem_rate: float = 0.10
    query_health_error_rate: float = 0.05
    query_health_p95_ms: int = 15000
    # Replay every expectation this often (uses LLM tokens). 0 = only when someone presses Replay.
    query_expectation_replay_hours: int = 24

    # --- uploads
    upload_source_id: str = "uploads"
    upload_max_mb: int = 50

    @field_validator("tei_ingest_url")
    @classmethod
    def _default_ingest_url(cls, v: str | None) -> str | None:
        return v or None

    @model_validator(mode="after")
    def _no_fake_directory_outside_tests(self) -> Settings:
        """DIRECTORY=fake reports every grant as a success and writes nowhere.

        A stub index returns no results, which is obvious. A stub directory says "Priya is now an
        administrator" when nothing happened, which is worse than an outage because nobody goes looking.
        """
        if self.directory == "fake" and self.app_env != "test":
            raise ValueError(
                f"DIRECTORY=fake is only permitted when APP_ENV=test (got {self.app_env!r}): it accepts every "
                f"write and performs none, so access grants would silently do nothing."
            )
        return self

    @property
    def tei_ingest(self) -> str | None:
        return self.tei_ingest_url or self.tei_query_url

    @property
    def uses_azure_services(self) -> bool:
        """Whether any adapter will authenticate to Azure, and therefore needs the managed identity."""
        return (self.search_backend == "azure" or self.queue == "servicebus" or self.config_store == "blob"
                or self.raw_store == "blob" or bool(self.blob_account_url) or bool(self.key_vault_url))

    @property
    def aoai_utility(self) -> str | None:
        """Deployment the utility role uses. Unset means "same model as the answer role"."""
        return self.aoai_utility_deployment or self.aoai_chat_deployment

    @property
    def claude_utility(self) -> str:
        """Model the utility role uses. Unset means "same model as the answer role"."""
        return self.claude_utility_model or self.claude_model

    @property
    def embed_origin_list(self) -> list[str]:
        return [o.strip() for o in self.embed_origins.split(",") if o.strip()]

    @property
    def entra_audiences(self) -> tuple[str, ...]:
        """Every `aud` Entra may legitimately stamp for this API, derived from ENTRA_AUDIENCE alone.

        Which one arrives is a directory setting, not ours: with ``api.requestedAccessTokenVersion = 2`` the
        `aud` is the API's bare client-id GUID, and with 1 (what ``null`` means) it is the ``api://`` resource
        URI the client requested. Both spell the same application, so this is one audience in two forms rather
        than two audiences - which is why it is derived here instead of being a second setting to keep in step.

        ENTRA_CLIENT_ID is deliberately NOT added: where the browser and the API are separate registrations that
        is a different application, and accepting it would accept a token minted for something else.
        """
        aud = (self.entra_audience or "").strip()
        if not aud:
            return ()
        forms = [aud]
        bare = aud.removeprefix("api://").rsplit("/", 1)[-1]  # also covers api://<tenant-id>/<app-id>
        if _GUID_RE.fullmatch(bare):
            forms += [bare, f"api://{bare}"]
        return tuple(dict.fromkeys(forms))  # order preserved, duplicates dropped

    @property
    def extension_app_id(self) -> str | None:
        """The app id whose directory extensions this deployment reads, as a bare GUID."""
        if self.entra_extension_app_id:
            return self.entra_extension_app_id.strip()
        return next((a for a in self.entra_audiences if _GUID_RE.fullmatch(a)), None)

    @property
    def auth_mode(self) -> str:
        """What the chat UI should offer: a real Entra sign-in, dev principals, or nothing."""
        if self.entra_tenant_id and self.entra_client_id and self.entra_api_scope:
            return "entra"
        return "dev" if self.dev_auth_enabled else "none"

    secret_fields: tuple[str, ...] = Field(
        default=(
            "dev_jwt_key",
            "aoai_api_key",
            "claude_api_key",
            "applicationinsights_connection_string",
            "blob_connection_string",
        ),
        exclude=True,
    )

    def resolve_secrets(self, resolver: object) -> Settings:
        """Replace kv:// references using the given SecretResolver."""
        from rag_os.application.ports import SecretResolver

        assert isinstance(resolver, SecretResolver)
        updates: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, str) and value.startswith("kv://"):
                updates[name] = resolver.resolve(value)
        return self.model_copy(update=updates) if updates else self

    def redacted(self) -> dict[str, object]:
        data = self.model_dump()
        for f in self.secret_fields:
            if data.get(f):
                data[f] = "***"
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
