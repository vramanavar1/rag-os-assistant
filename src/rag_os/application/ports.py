"""Ports: the interfaces the application depends on. Infrastructure provides the adapters.

Every adapter is created by a factory (see rag_os.infrastructure.registry), selected by configuration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import IO, Any, NamedTuple, Protocol, runtime_checkable

from rag_os.domain.access import AccessPolicy
from rag_os.domain.answers import SearchHit, TokenUsage
from rag_os.domain.classification import FacetSchema, PathRules
from rag_os.domain.documents import (
    ChunkDraft,
    DocumentRecord,
    DocumentStatus,
    ParsedDocument,
    SourceItem,
    TagSet,
)
from rag_os.domain.embedding import ChunkingConfig, EmbedderInfo, EmbeddingProfile
from rag_os.domain.ingestion import (
    IngestionControls,
    IngestionRun,
    IngestMessage,
    Lane,
    SourceConfig,
    SourcesFile,
)
from rag_os.domain.trace import Expectation, QueryTrace, TraceSummaryRow

# --------------------------------------------------------------------------- sources


class DocumentSource(ABC):
    """A configured source instance (local folder, blob container, SharePoint library, ...)."""

    #: True when workers cannot read items directly and discovery must stage bytes to the raw store.
    staging_required: bool = True

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    @property
    def id(self) -> str:
        return self.config.id

    @abstractmethod
    def iter_items(self) -> Iterator[SourceItem]:
        """Stream every item (paged internally; must not load the full listing in memory)."""

    @abstractmethod
    def open(self, item: SourceItem) -> IO[bytes]:
        """Open an item for reading."""

    def read_manifest(self) -> dict[str, TagSet]:
        """Optional bulk tagging file (manifest.csv) keyed by item_id. Default: none."""
        return {}

    def healthcheck(self) -> None:  # noqa: B027 - optional hook
        """Raise if the source is unreachable/misconfigured."""


# --------------------------------------------------------------------------- parsing / chunking


class DocumentParser(ABC):
    extensions: tuple[str, ...] = ()
    content_types: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, stream: IO[bytes], filename: str) -> ParsedDocument:
        """Parse a document. Large inputs should yield sections lazily."""


class Chunker(Protocol):
    def chunk(self, doc: ParsedDocument, config: ChunkingConfig) -> Iterator[ChunkDraft]: ...


# --------------------------------------------------------------------------- embeddings / llm


class EmbeddingProvider(ABC):
    """Embeds text according to an EmbeddingProfile (prefixes, normalisation, dimensions)."""

    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile

    @abstractmethod
    async def info(self) -> EmbedderInfo:
        """Ask the running model/server what it is (for the profile guard)."""

    @abstractmethod
    async def embed_documents(self, texts: Sequence[str]) -> tuple[list[list[float]], TokenUsage]: ...

    @abstractmethod
    async def embed_query(self, text: str) -> tuple[list[float], TokenUsage]: ...

    async def aclose(self) -> None:  # noqa: B027
        pass


@dataclass
class LlmMessage:
    role: str  # user | assistant
    content: str


@dataclass
class LlmResult:
    text: str
    usage: TokenUsage
    provider: str
    model: str
    stop_reason: str | None = None
    refused: bool = False


class LlmProvider(ABC):
    name: str = "llm"

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LlmMessage],
        max_tokens: int = 2048,
        purpose: str = "answer",
        json_output: bool = False,
    ) -> LlmResult: ...

    async def aclose(self) -> None:  # noqa: B027
        pass


# --------------------------------------------------------------------------- search


@dataclass
class IndexField:
    name: str
    type: str  # "string" | "strings" | "int" | "bool" | "vector" | "date"
    key: bool = False
    searchable: bool = False
    filterable: bool = False
    facetable: bool = False
    sortable: bool = False
    retrievable: bool = True
    dimensions: int | None = None
    analyzer: str | None = None


@dataclass
class IndexSchema:
    name: str
    fields: list[IndexField]
    vector_field: str = "vector"
    semantic_title: str = "title"
    semantic_content: list[str] = field(default_factory=lambda: ["content"])
    semantic_keywords: list[str] = field(default_factory=lambda: ["heading"])
    compression: str = "scalar"  # none | scalar | binary

    def field_names(self) -> set[str]:
        return {f.name for f in self.fields}


@dataclass
class SearchRequest:
    text: str | None
    vector: list[float] | None
    odata_filter: str | None
    top: int = 8
    candidates: int = 50
    semantic: bool = True
    select: list[str] | None = None


class SearchIndex(ABC):
    @abstractmethod
    async def ensure_index(self, schema: IndexSchema) -> None:
        """Create the index or add new fields in place. Must refuse incompatible changes."""

    @abstractmethod
    async def index_exists(self) -> bool:
        """Whether the index itself is there.

        Distinct from read_profile() returning None, which is also what an index that exists but was never
        stamped with a profile looks like. The two need different remedies, so they need different answers.
        """

    @abstractmethod
    async def read_profile(self) -> dict[str, Any] | None: ...

    @abstractmethod
    async def write_profile(self, profile: dict[str, Any]) -> None: ...

    @abstractmethod
    async def upsert(self, documents: Sequence[dict[str, Any]]) -> None:
        """Upsert index documents (already mapped by IndexDocumentMapper). Idempotent by chunk_id."""

    @abstractmethod
    async def merge(self, documents: Sequence[dict[str, Any]]) -> None:
        """Merge the given fields into EXISTING documents (keyed by chunk_id). Used for re-tagging."""

    @abstractmethod
    async def delete_doc_versions(self, doc_id: str, keep_version: str | None) -> int:
        """Delete chunks of a doc whose version != keep_version (None = delete all)."""

    @abstractmethod
    async def search(self, request: SearchRequest) -> list[SearchHit]: ...

    @abstractmethod
    async def facets(self, odata_filter: str | None, fields: Sequence[str]) -> dict[str, dict[str, int]]: ...

    @abstractmethod
    async def count(self) -> int: ...

    @abstractmethod
    async def clear(self, schema: IndexSchema, profile: dict[str, Any] | None) -> int:
        """Remove every chunk but keep the index usable: same schema, same embedding-profile stamp.

        Returns how many chunks were there. Full reset only.
        """

    async def aclose(self) -> None:  # noqa: B027
        pass


# --------------------------------------------------------------------------- queue


@dataclass
class ReceivedMessage:
    message: IngestMessage
    delivery_count: int
    handle: Any  # adapter-specific


class MessageQueue(ABC):
    @abstractmethod
    async def send(self, messages: Sequence[IngestMessage]) -> None: ...

    @abstractmethod
    async def receive(self, max_messages: int, wait_seconds: float) -> list[ReceivedMessage]:
        """Receive from the priority lane first, then bulk."""

    @abstractmethod
    async def complete(self, msg: ReceivedMessage) -> None: ...

    @abstractmethod
    async def abandon(self, msg: ReceivedMessage) -> None: ...

    @abstractmethod
    async def dead_letter(self, msg: ReceivedMessage, reason: str, description: str) -> None: ...

    async def renew(self, msg: ReceivedMessage) -> None:  # noqa: B027 - optional
        pass

    @abstractmethod
    async def depth(self) -> dict[str, int]:
        """Approximate active + dead-letter counts per lane."""

    @abstractmethod
    async def peek_dead_letters(self, lane: Lane, max_messages: int) -> list[IngestMessage]: ...

    @abstractmethod
    async def purge_all(self, time_budget_s: float = 120.0) -> int:
        """Drop every message, active and dead-lettered, on both lanes. Returns how many. Full reset only."""

    async def purge_messages(self, doc_ids: Sequence[str]) -> int:
        """Drop queued messages for these documents, where the transport can address single messages.

        Optional: Service Bus cannot remove a message without receiving it, so it keeps this default. A message
        left behind finds no document row and is skipped by the worker.
        """
        return 0

    async def aclose(self) -> None:  # noqa: B027
        pass


# --------------------------------------------------------------------------- state store


@dataclass
class PurgeCandidate:
    """A deleted document, and whether its content may be freed with it."""

    doc_id: str
    blob_uri: str | None
    content_hash: str | None
    free_blob: bool


@dataclass
class DocumentQuery:
    status: list[DocumentStatus] | None = None
    source_id: str | None = None
    facet: tuple[str, str] | None = None  # (facet name, value)
    text: str | None = None  # path contains
    review_pending: bool | None = None
    path_prefix: str | None = None  # ownership scope, e.g. "<subject>/" - see uploads.list_uploads
    content_hash: str | None = None  # exact bytes; several documents may share one
    after: str | None = None  # opaque keyset cursor; its shape follows `newest_first`
    limit: int = 100
    # Newest first, by when the document was DISCOVERED. Not doc_id, which is a content hash and so orders
    # arbitrarily, and not updated_at, which changes as the document progresses - a row that moved between
    # pages mid-paging would be silently skipped or shown twice.
    newest_first: bool = False


@dataclass
class DocumentEvent:
    doc_id: str
    status: DocumentStatus
    at: datetime
    stage: str | None = None
    message: str | None = None
    correlation_id: str | None = None


@dataclass
class DiscoveryDelta:
    """Outcome of upserting a batch of discovered items."""

    full: list[DocumentRecord] = field(default_factory=list)  # new / content changed -> full pipeline
    retag: list[DocumentRecord] = field(default_factory=list)  # same content, tags/ACL changed
    unchanged: int = 0
    skipped_failed: int = 0  # same version already FAILED (retry is an explicit admin action)
    in_flight: int = 0  # same version already queued/processing


class IngestionStateStore(ABC):
    # documents
    @abstractmethod
    def upsert_discovered(self, records: Sequence[DocumentRecord], run_id: str,
                          embedding_fp: str | None = None) -> DiscoveryDelta:
        """Upsert discovered docs (sets last_seen_run_id) and classify what work each needs.

        `embedding_fp` is the fingerprint of the ACTIVE embedding profile. A document already indexed under a
        different fingerprint needs full re-processing: it lives in another index, in another vector space.
        Passing None disables that comparison.
        """

    @abstractmethod
    def update_tags(self, doc_id: str, tags: TagSet, review_status: str | None = None) -> DocumentRecord: ...

    @abstractmethod
    def get(self, doc_id: str) -> DocumentRecord | None: ...

    @abstractmethod
    def by_tracking_id(self, tracking_id: str) -> DocumentRecord | None: ...

    @abstractmethod
    def transition(self, doc_id: str, status: DocumentStatus, **fields: Any) -> DocumentRecord: ...

    @abstractmethod
    def mark_queued(self, doc_ids: Sequence[str]) -> None: ...

    @abstractmethod
    def mark_unseen_deleted(self, source_id: str, run_id: str) -> list[str]:
        """Docs of the source not seen in this run -> DELETED. Returns their ids."""

    @abstractmethod
    def mark_deleted(self, doc_ids: Sequence[str], *, stage: str, message: str) -> list[str]:
        """These docs -> DELETED, with an event. Returns the ids that changed (already-deleted ones are skipped)."""

    @abstractmethod
    def query(self, q: DocumentQuery) -> tuple[list[DocumentRecord], str | None]: ...

    @abstractmethod
    def purgeable(self, older_than: datetime, limit: int = 1000) -> list[PurgeCandidate]:
        """Deleted documents past the retention window, with whether their blob is still shared."""

    @abstractmethod
    def delete_documents(self, doc_ids: Sequence[str]) -> int:
        """Remove state rows. The caller is responsible for the index and the blobs."""

    @abstractmethod
    def blob_refs(self, uris: Sequence[str], exclude_doc_ids: Sequence[str]) -> set[str]:
        """Which of these blob uris some OTHER live document still uses - by uri, or by the content hash behind
        it (staging is keyed by content, so two documents can share one blob without sharing a uri string)."""

    @abstractmethod
    def clear_all(self) -> dict[str, int]:
        """Delete every document, facet row, event and ingestion run; keep controls. Full reset only."""

    @abstractmethod
    def count_by_status(self, q: DocumentQuery) -> dict[str, int]:
        """How many documents each status holds, for the same filters MINUS `status` itself.

        Ignoring `q.status` is the point: it is what lets a tabbed view label every tab from one call. Counting
        with the status filter applied would only ever report the tab you are already looking at.
        """

    @abstractmethod
    def stale_in_flight(self, older_than: datetime, limit: int) -> list[DocumentRecord]: ...

    @abstractmethod
    def events(self, doc_id: str, limit: int = 200) -> list[DocumentEvent]: ...

    # runs
    @abstractmethod
    def start_run(self, source_id: str, trigger: str) -> IngestionRun: ...

    @abstractmethod
    def finish_run(self, run: IngestionRun) -> None: ...

    @abstractmethod
    def list_runs(self, source_id: str | None, limit: int) -> list[IngestionRun]: ...

    @abstractmethod
    def get_run(self, run_id: str) -> IngestionRun | None: ...

    @abstractmethod
    def run_progress(self, run_id: str) -> dict[str, int]: ...

    # reporting
    @abstractmethod
    def summary(self, group_by_facet: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def error_breakdown(self, run_id: str | None, limit: int = 20) -> list[dict[str, Any]]: ...

    # controls
    @abstractmethod
    def get_controls(self) -> IngestionControls: ...

    @abstractmethod
    def set_controls(self, controls: IngestionControls) -> None: ...

    # sources
    @abstractmethod
    def last_run_started(self, source_id: str) -> datetime | None: ...

    def close(self) -> None:  # noqa: B027
        pass


# --------------------------------------------------------------------------- storage / config / secrets


class Staged(NamedTuple):
    """Where staged bytes landed, and what they hashed to."""

    uri: str
    content_hash: str


class RawDocumentStore(ABC):
    @abstractmethod
    def stage(self, source_id: str, doc_id: str, filename: str, stream: IO[bytes],
              content_hash: str | None = None) -> Staged:
        """Copy bytes into the raw store, keyed by content; returns a uri readable by workers and the sha256.

        Identical bytes are stored once however many documents reference them, so a caller must never assume
        the uri is its own: see `delete`.
        """

    @abstractmethod
    def delete(self, uri: str) -> bool:
        """Remove staged bytes. Returns False when they were already gone.

        The blob is shared, so the ONLY safe caller is a purge that has established no live document still
        references this content. Deleting from the document path would take another document's bytes with it.
        """

    @abstractmethod
    def open(self, uri: str) -> IO[bytes]: ...

    @abstractmethod
    def put_export(self, name: str, data: bytes, content_type: str) -> str: ...

    @abstractmethod
    def open_export(self, name: str) -> IO[bytes]: ...

    @abstractmethod
    def owns(self, uri: str | None) -> bool:
        """Whether this store staged `uri`. A source read in place (an azure_blob source) is NOT ours to delete."""

    @abstractmethod
    def clear_staged(self) -> dict[str, int]:
        """Delete every staged copy and every export, and nothing else. Full reset only."""


class ConfigRepository(ABC):
    """Versioned domain configuration (sources, access policy, facets, path rules)."""

    @abstractmethod
    def load_sources(self) -> SourcesFile: ...

    @abstractmethod
    def load_access_policy(self) -> AccessPolicy: ...

    @abstractmethod
    def load_facets(self) -> FacetSchema: ...

    @abstractmethod
    def load_path_rules(self) -> PathRules: ...

    @abstractmethod
    def load_embedding_profile(self, name: str) -> EmbeddingProfile: ...

    @abstractmethod
    def read_raw(self, kind: str) -> tuple[str, str]:
        """(yaml text, etag) for kind in {sources, access-policy, facets, path-rules}."""

    @abstractmethod
    def write_raw(self, kind: str, text: str, if_match: str | None) -> str:
        """Validate + store a new version; returns the new etag. Raises Conflict on etag mismatch."""


@runtime_checkable
class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


# --------------------------------------------------------------------------- classification


@dataclass
class ClassificationResult:
    facets: dict[str, list[str]]
    confidence: dict[str, float]
    margin: dict[str, float]
    method: str  # embedding | llm
    needs_review: bool
    usage: TokenUsage = field(default_factory=TokenUsage)


class Classifier(ABC):
    @abstractmethod
    async def classify(
        self,
        *,
        facets_to_fill: Sequence[str],
        text_sample: str,
        vector: list[float] | None,
        schema: FacetSchema,
    ) -> ClassificationResult: ...


# --------------------------------------------------------------------------- retrieval


@dataclass
class RetrievalResult:
    hits: list[SearchHit]
    usage: TokenUsage
    timings_ms: dict[str, float]
    # What the relevance bar removed, and the bar itself. Recorded rather than silently discarded, because "the
    # search found nothing" and "the search found something the bar threw away" need different fixes.
    dropped: list[SearchHit] = field(default_factory=list)
    thresholds: dict[str, float] = field(default_factory=dict)
    vector: list[float] | None = None  # the query embedding, so a diagnostic probe need not embed again


@dataclass
class TraceQuery:
    outcome: str | None = None
    verdict: str | None = None
    reason: str | None = None
    user: str | None = None  # substring of subject or display name
    text: str | None = None  # substring of the question
    since: datetime | None = None
    problems_only: bool = False
    after: str | None = None
    limit: int = 50


class QueryTraceStore(ABC):
    """Per-question traces and the expectations they are checked against. Synchronous, like the state store."""

    @abstractmethod
    def save(self, trace: QueryTrace) -> None: ...

    @abstractmethod
    def get(self, key: str) -> QueryTrace | None:
        """By trace id, or by correlation id (the newest trace carrying it)."""

    @abstractmethod
    def query(self, q: TraceQuery) -> tuple[list[TraceSummaryRow], str | None]: ...

    @abstractmethod
    def window(self, since: datetime, limit: int = 50_000) -> list[TraceSummaryRow]:
        """Every trace since a moment, newest first - the raw material of the health summary."""

    @abstractmethod
    def purge(self, older_than: datetime) -> int: ...

    @abstractmethod
    def forget_documents(self, doc_ids: Sequence[str]) -> dict[str, int]:
        """Delete traces that mention these documents, and drop them from expectations' required documents.

        A trace can hold a document's title, path and answer text quoting it, so a permanently deleted
        document must not survive there. Returns {"traces": n, "expectations": m}.
        """

    @abstractmethod
    def clear_all(self) -> dict[str, int]:
        """Every trace and every expectation. Full reset only."""

    @abstractmethod
    def save_expectation(self, e: Expectation) -> None: ...

    @abstractmethod
    def get_expectation(self, expectation_id: str) -> Expectation | None: ...

    @abstractmethod
    def list_expectations(self) -> list[Expectation]: ...

    @abstractmethod
    def delete_expectation(self, expectation_id: str) -> bool: ...

    @abstractmethod
    def claim_due(self, due_before: datetime, lease: timedelta, limit: int = 20) -> list[Expectation]:
        """Expectations not run since `due_before`, claimed atomically so two API replicas never both run one."""

    @abstractmethod
    def record_result(self, expectation_id: str, result: str, detail: str, trace_id: str | None,
                      at: datetime) -> None: ...


class Retriever(ABC):
    @abstractmethod
    async def retrieve(
        self, *, query: str, keyword_query: str, odata_filter: str | None, top: int
    ) -> RetrievalResult: ...


AsyncByteIterator = AsyncIterator[bytes]


# --------------------------------------------------------------------------- identity directory


@dataclass(frozen=True)
class DirectoryUser:
    """A person in the identity provider, as much of them as administering access requires.

    `attributes` is keyed by POLICY attribute name - "department", not
    "extension_<appid>_department". Translating to whatever the provider calls it is the adapter's job, so
    nothing above the adapter boundary has to know that a directory extension exists. A key that is absent
    means no value is set, which the provider reports differently from an empty one.
    """

    object_id: str
    user_principal_name: str
    display_name: str
    mail: str | None
    account_enabled: bool  # so an administrator is not granting access to somebody who has left
    user_type: str  # Member | Guest - a guest's access is worth seeing before granting more of it
    attributes: dict[str, str]


@dataclass(frozen=True)
class RoleAssignment:
    """One application role a person holds, and how they hold it.

    `via_group` is the whole reason this is not just a list of strings. An app role can be assigned to a group,
    and the holder's token carries it exactly as if it were assigned to them directly - so a role that cannot
    be revoked per-person still has to be SHOWN per-person, or an administrator removes the direct assignment,
    sees the row disappear, and the person is still an administrator.

    `duplicates` exists because Microsoft Graph does not deduplicate assignments: the same grant posted twice
    becomes two rows. Revoking has to clear all of them.
    """

    role_value: str  # as it appears in the token's roles claim, e.g. rag.admin
    principal_type: str = "User"
    via_group: str | None = None  # display name of the group it comes from; None = assigned directly
    duplicates: int = 1

    @property
    def removable(self) -> bool:
        return self.via_group is None


class DirectoryAdmin(ABC):
    """Reads and writes the identity-provider records that decide what a person may read.

    An ABC rather than a Protocol on purpose: mypy is configured over domain/ and application/ only, so an
    adapter that drifts from this interface is not type-checked. An ABC at least turns a missing method into a
    TypeError when the adapter is constructed instead of an AttributeError mid-request.

    Every method takes an object id, never an email, with the single exception of find_user. Resolving an
    address to an object id exactly once and using only the id afterwards is a correctness requirement, not
    tidiness: a B2B guest's user principal name contains "#EXT#", and "#" begins a URL fragment, so a UPN
    placed in a request path is silently truncated and the write lands on a different user - or none.
    """

    @abstractmethod
    async def find_user(self, email: str) -> DirectoryUser:
        """The one person with this address. Raises NotFound if there is none, Conflict if there are several."""

    @abstractmethod
    async def read_user(self, object_id: str) -> DirectoryUser:
        """The person with this object id, attributes included.

        Separate from find_user because the caller already holds a trustworthy object id - it came from the
        `oid` claim of a signed token - so there is nothing to resolve and no address to mis-parse. This is the
        read the query path uses for a caller whose token carries no attribute claims at all, which is every
        Microsoft-account guest: Entra does not emit directory extension claims for them.
        """

    @abstractmethod
    async def set_attributes(self, object_id: str, values: Mapping[str, str | None]) -> None:
        """Set attributes by POLICY attribute name. None clears one.

        Values are strings even when the attribute is numeric, because every RAG-OS directory extension is
        declared as a string (see $script:RagOsUserAttributes in infra/scripts/common.ps1) and Graph rejects a
        JSON literal whose type does not match the declaration. Typing it this way makes clearance=1 - the
        400 that trap produces - unrepresentable rather than merely documented.
        """

    @abstractmethod
    async def list_roles(self, object_id: str) -> list[RoleAssignment]:
        """Every application role this person holds, direct and group-derived."""

    @abstractmethod
    async def grant_role(self, object_id: str, role_value: str) -> None: ...

    @abstractmethod
    async def revoke_role(self, object_id: str, role_value: str) -> int:
        """Remove every direct assignment of this role. Returns how many there were (0 is not an error)."""

    @abstractmethod
    async def revoke_sessions(self, object_id: str) -> None:
        """Invalidate this person's refresh tokens, so their next token carries the new values.

        It does NOT invalidate an access token they are already holding, and it signs them out of every other
        application in the tenant. Both facts belong in front of whoever asks for it.
        """

    async def aclose(self) -> None:
        """Release the HTTP session and credential. Adapters holding neither need not override this."""
        return None
