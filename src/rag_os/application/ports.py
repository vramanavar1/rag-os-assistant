"""Ports: the interfaces the application depends on. Infrastructure provides the adapters.

Every adapter is created by a factory (see rag_os.infrastructure.registry), selected by configuration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Any, Protocol, runtime_checkable

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

    async def aclose(self) -> None:  # noqa: B027
        pass


# --------------------------------------------------------------------------- state store


@dataclass
class DocumentQuery:
    status: list[DocumentStatus] | None = None
    source_id: str | None = None
    facet: tuple[str, str] | None = None  # (facet name, value)
    text: str | None = None  # path contains
    review_pending: bool | None = None
    after: str | None = None  # keyset cursor (doc_id)
    limit: int = 100


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
    def query(self, q: DocumentQuery) -> tuple[list[DocumentRecord], str | None]: ...

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


class RawDocumentStore(ABC):
    @abstractmethod
    def stage(self, source_id: str, doc_id: str, filename: str, stream: IO[bytes]) -> str:
        """Copy bytes into the raw store; returns a uri readable by workers."""

    @abstractmethod
    def open(self, uri: str) -> IO[bytes]: ...

    @abstractmethod
    def put_export(self, name: str, data: bytes, content_type: str) -> str: ...

    @abstractmethod
    def open_export(self, name: str) -> IO[bytes]: ...


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


class Retriever(ABC):
    @abstractmethod
    async def retrieve(
        self, *, query: str, keyword_query: str, odata_filter: str | None, top: int
    ) -> RetrievalResult: ...


AsyncByteIterator = AsyncIterator[bytes]
