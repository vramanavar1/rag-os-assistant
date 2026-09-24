"""Ingestion runs, queue messages, source configuration and operator controls."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from rag_os.domain.access import FIELD_NAME_RE


class Lane(StrEnum):
    PRIORITY = "priority"  # interactive uploads
    BULK = "bulk"  # scheduled syncs / backfills


class SourceDefaults(BaseModel):
    facets: dict[str, list[str] | str] = Field(default_factory=dict)
    acl: dict[str, list[str] | int | str] = Field(default_factory=dict)

    def normalised(self) -> tuple[dict[str, list[str]], dict[str, list[str] | int]]:
        facets = {k: ([v] if isinstance(v, str) else list(v)) for k, v in self.facets.items()}
        acl: dict[str, list[str] | int] = {}
        for k, v in self.acl.items():
            if isinstance(v, int):
                acl[k] = v
            elif isinstance(v, str):
                acl[k] = [v]
            else:
                acl[k] = list(v)
        return facets, acl


class SourceConfig(BaseModel):
    """One configured source *instance* (many instances of the same type are allowed)."""

    id: str
    type: str
    enabled: bool = True
    domain: str = "default"
    lane: Lane = Lane.BULK
    schedule: str | None = None  # cron; None = manual (CLI / API trigger)
    full_listing: bool = True  # listing is complete -> unseen items can be marked DELETED
    settings: dict[str, Any] = Field(default_factory=dict)
    defaults: SourceDefaults = Field(default_factory=SourceDefaults)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not FIELD_NAME_RE.match(v.replace("-", "_")):
            raise ValueError(f"invalid source id {v!r}")
        return v


class SourcesFile(BaseModel):
    version: int = 1
    sources: list[SourceConfig] = Field(default_factory=list)

    def get(self, source_id: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.id == source_id), None)


class MessageMode(StrEnum):
    FULL = "full"  # parse -> classify -> chunk -> embed -> index
    RETAG = "retag"  # tags/ACL changed, content unchanged: merge fields into existing chunks (no re-embed)
    DELETE = "delete"  # remove all chunks of the document


class IngestMessage(BaseModel):
    """Queue message: a claim-check reference, never the document content."""

    doc_id: str
    version_key: str
    source_id: str
    lane: Lane = Lane.BULK
    mode: MessageMode = MessageMode.FULL
    tags_hash: str = ""
    run_id: str | None = None
    correlation_id: str | None = None
    attempt_nonce: str = ""  # set on manual retries so duplicate detection does not swallow them

    @property
    def message_id(self) -> str:
        # Service Bus duplicate detection key: same doc + version + mode (+ tags) => same message
        base = f"{self.doc_id}:{self.version_key}:{self.mode.value}"
        if self.tags_hash:
            base += f":{self.tags_hash[:12]}"
        if self.attempt_nonce:
            base += f":{self.attempt_nonce}"
        return base[:128]


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IngestionRun(BaseModel):
    run_id: str
    source_id: str
    trigger: str  # schedule | manual | upload | reconcile
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime
    finished_at: datetime | None = None
    discovered: int = 0
    queued: int = 0
    unchanged: int = 0
    deleted: int = 0
    error: str | None = None


class IngestionControls(BaseModel):
    paused: bool = False
    max_concurrency: int | None = None  # None = use the worker's configured value
    paused_sources: list[str] = Field(default_factory=list)
    updated_by: str | None = None
    updated_at: datetime | None = None
