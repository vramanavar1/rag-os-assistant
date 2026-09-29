"""Documents, their lifecycle state machine, parsed content and chunks."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def stable_id(*parts: str, length: int = 32) -> str:
    """Deterministic id used for doc_id / chunk_id (idempotent re-processing)."""
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return h[:length]


class DocumentStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    CHUNKED = "CHUNKED"
    EMBEDDED = "EMBEDDED"
    CLASSIFIED = "CLASSIFIED"  # after embedding: the classifier reuses the chunk vectors (no extra cost)
    INDEXED = "INDEXED"
    SKIPPED_UNCHANGED = "SKIPPED_UNCHANGED"
    FAILED = "FAILED"
    DELETED = "DELETED"


TERMINAL_STATUSES = frozenset(
    {DocumentStatus.INDEXED, DocumentStatus.SKIPPED_UNCHANGED, DocumentStatus.FAILED, DocumentStatus.DELETED}
)
IN_FLIGHT_STATUSES = frozenset(
    {
        DocumentStatus.DISCOVERED,
        DocumentStatus.QUEUED,
        DocumentStatus.PARSING,
        DocumentStatus.CHUNKED,
        DocumentStatus.EMBEDDED,
        DocumentStatus.CLASSIFIED,
    }
)

# Allowed transitions. Any state may go to FAILED or DELETED; re-discovery restarts the pipeline.
_PIPELINE = [
    DocumentStatus.DISCOVERED,
    DocumentStatus.QUEUED,
    DocumentStatus.PARSING,
    DocumentStatus.CHUNKED,
    DocumentStatus.EMBEDDED,
    DocumentStatus.CLASSIFIED,
    DocumentStatus.INDEXED,
]
ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {}
for _i, _s in enumerate(_PIPELINE):
    nxt = {_PIPELINE[_i + 1]} if _i + 1 < len(_PIPELINE) else set()
    ALLOWED_TRANSITIONS[_s] = frozenset(
        nxt
        | {DocumentStatus.FAILED, DocumentStatus.DELETED, DocumentStatus.DISCOVERED, DocumentStatus.QUEUED}
        # a redelivered message restarts processing from PARSING
        | {DocumentStatus.PARSING}
    )
for _s in (DocumentStatus.SKIPPED_UNCHANGED, DocumentStatus.FAILED, DocumentStatus.DELETED):
    ALLOWED_TRANSITIONS[_s] = frozenset(
        {DocumentStatus.DISCOVERED, DocumentStatus.QUEUED, DocumentStatus.PARSING, DocumentStatus.DELETED,
         DocumentStatus.SKIPPED_UNCHANGED, DocumentStatus.FAILED}
    )
# RETAG messages update facets/ACL in place (no parse/embed): QUEUED -> INDEXED directly.
ALLOWED_TRANSITIONS[DocumentStatus.QUEUED] = ALLOWED_TRANSITIONS[DocumentStatus.QUEUED] | {DocumentStatus.INDEXED}
ALLOWED_TRANSITIONS[DocumentStatus.INDEXED] = frozenset(
    {DocumentStatus.DISCOVERED, DocumentStatus.QUEUED, DocumentStatus.PARSING, DocumentStatus.SKIPPED_UNCHANGED,
     DocumentStatus.DELETED, DocumentStatus.FAILED}
)


def can_transition(current: DocumentStatus, new: DocumentStatus) -> bool:
    return new == current or new in ALLOWED_TRANSITIONS[current]


class ReviewStatus(StrEnum):
    NONE = "NONE"
    PENDING = "PENDING"
    APPROVED = "APPROVED"


class TagSet(BaseModel):
    """Classification + ACL values assigned to a document, with provenance per key."""

    facets: dict[str, list[str]] = Field(default_factory=dict)
    acl: dict[str, list[str] | int] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)  # "facet:region" -> "path_rule" etc.

    def merged_with(self, other: TagSet, source_name: str) -> TagSet:
        """Whole-key replacement (not union), and only non-empty values overwrite - so a blank manifest cell
        inherits from the folder rule rather than clearing it.

        `source_name` labels the provenance of what came in, EXCEPT where `other` already records its own: a
        set assembled from several layers (an upload, whose facets come partly from path rules and partly from
        the person) keeps each value's real origin instead of having the whole batch relabelled by whoever
        merged it last. The sets built by crawls carry no sources, so for them nothing changes.
        """
        facets = dict(self.facets)
        acl = dict(self.acl)
        sources = dict(self.sources)
        for k, v in other.facets.items():
            if v:
                facets[k] = list(v)
                sources[f"facet:{k}"] = other.sources.get(f"facet:{k}", source_name)
        for k, av in other.acl.items():
            if av is not None and av != []:
                acl[k] = av
                sources[f"acl:{k}"] = other.sources.get(f"acl:{k}", source_name)
        return TagSet(facets=facets, acl=acl, sources=sources)


def tags_hash(tags: TagSet) -> str:
    """Fingerprint of the index-relevant part of a TagSet (facets + ACL, not provenance)."""
    import json

    payload = json.dumps({"f": tags.facets, "a": tags.acl}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class SourceItem(BaseModel):
    """One item listed by a DocumentSource."""

    source_id: str
    item_id: str  # stable within the source (relative path / blob name / remote id)
    path: str  # human-readable path used for path rules and display
    uri: str | None = None  # readable location if the source is directly readable by workers
    etag: str | None = None
    content_hash: str | None = None
    size: int = 0
    modified: datetime | None = None
    content_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sidecar: TagSet | None = None  # parsed <file>.meta.json if present

    @property
    def doc_id(self) -> str:
        return stable_id(self.source_id, self.item_id)

    @property
    def version_key(self) -> str:
        """What we compare to decide 'unchanged'. Prefer content hash, fall back to etag+size+mtime."""
        if self.content_hash:
            return self.content_hash
        mtime = self.modified.isoformat() if self.modified else ""
        return stable_id(self.etag or "", str(self.size), mtime, length=40)


class DocumentRecord(BaseModel):
    """Row in the ingestion state store (the source of truth for status reporting)."""

    doc_id: str
    source_id: str
    item_id: str
    path: str
    blob_uri: str | None = None
    # sha256 of the bytes. The identity of the CONTENT, as opposed to doc_id which identifies the document:
    # several documents (two uploaders, two sources, two folders) can share one content_hash, and the blob
    # and - when their tags also match - the indexed chunks are shared with it.
    content_hash: str | None = None
    version_key: str
    indexed_version: str | None = None
    # The content that is actually in the index right now. Compared with content_hash to answer "did the
    # bytes change?" independently of version_key, which for a local folder is only size+mtime.
    indexed_content_hash: str | None = None
    indexed_tags_hash: str | None = None
    status: DocumentStatus = DocumentStatus.DISCOVERED
    stage: str | None = None
    attempts: int = 0
    error_type: str | None = None
    error_message: str | None = None
    title: str | None = None
    content_type: str | None = None
    size: int = 0
    tags: TagSet = Field(default_factory=TagSet)
    review_status: ReviewStatus = ReviewStatus.NONE
    chunk_count: int = 0
    embedding_fp: str | None = None
    run_id: str | None = None
    last_seen_run_id: str | None = None
    tracking_id: str | None = None
    correlation_id: str | None = None
    discovered_at: datetime | None = None
    updated_at: datetime | None = None
    indexed_at: datetime | None = None


class Section(BaseModel):
    """A logical block of parsed text (paragraph run, table, sheet block, JSON record group...)."""

    text: str
    heading_path: list[str] = Field(default_factory=list)
    page: int | None = None
    kind: Literal["text", "table", "record"] = "text"


class ParsedDocument(BaseModel):
    """Parser output. `sections` may be a generator for large files (streamed)."""

    model_config = {"arbitrary_types_allowed": True}

    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sections: Iterable[Section] = Field(default_factory=list)


class ChunkDraft(BaseModel):
    ordinal: int
    text: str
    heading: str = ""
    page: int | None = None
    kind: str = "text"
    token_estimate: int = 0


class IndexedChunk(BaseModel):
    """Everything written to the search index for one chunk."""

    chunk_id: str
    doc_id: str
    doc_version: str
    ordinal: int
    title: str
    heading: str
    content: str
    page: int | None
    source_id: str
    path: str
    content_type: str | None
    embedding_fp: str
    # None when the pool was never asked. Absent evidence is recorded as absent, not guessed at from config.
    embedded_by: str | None = None
    facets: dict[str, list[str]]
    acl: dict[str, list[str] | int]
    vector: list[float]
    is_current: bool = True
    effective_date: str | None = None


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 chars/token, words floor)."""
    if not text:
        return 0
    return max(len(text) // 4, len(text.split()))
