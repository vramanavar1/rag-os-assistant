"""Request/response models (OpenAPI is generated from these at /api/docs)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from rag_os.domain.answers import ChatTurn
from rag_os.domain.documents import DocumentRecord, DocumentStatus


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000, examples=["How many days of parental leave do I get?"])
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    filters: dict[str, list[str]] = Field(default_factory=dict, description="facet name -> allowed values")


class PublicConfigResponse(BaseModel):
    """Everything the browser needs before anyone has signed in. Never include a secret here."""

    app_name: str
    auth_mode: str = Field(description="entra = sign in with Microsoft, dev = local principals, none")
    embed_origins: list[str] = Field(description="origins allowed to frame /embed and post a token to it")
    dev_auth_enabled: bool
    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_api_scope: str | None = Field(default=None, examples=["api://00000000-0000-0000-0000-000000000000/access_as_user"])


class MeResponse(BaseModel):
    subject: str
    issuer_kind: str
    display_name: str
    attributes: dict[str, Any]
    roles: list[str]


class UploadResponse(BaseModel):
    tracking_id: str
    doc_id: str
    status: DocumentStatus


class UploadSummary(BaseModel):
    """One row of the recent-documents list.

    Deliberately NOT the whole DocumentRecord. That model carries `blob_uri` and the resolved ACL, which are
    tolerable in the single-record status call the uploader already owns, but would publish internal storage
    URIs and everyone's access tags a page at a time once the same data is served as a list.
    """

    doc_id: str
    tracking_id: str | None = None
    title: str | None = None
    path: str
    source_id: str
    status: DocumentStatus
    stage: str | None = None
    attempts: int = 0
    error_type: str | None = None
    error_message: str | None = None
    chunk_count: int = 0
    size: int = 0
    discovered_at: datetime | None = None
    updated_at: datetime | None = None
    indexed_at: datetime | None = None

    @classmethod
    def of(cls, rec: DocumentRecord) -> UploadSummary:
        return cls.model_validate(rec, from_attributes=True)


class UploadListResponse(BaseModel):
    items: list[UploadSummary]
    next: str | None = Field(default=None, description="opaque cursor; pass as `after` for the following page")
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="documents per status for these filters, ignoring the status filter itself",
    )


class RetryRequest(BaseModel):
    doc_ids: list[str] | None = Field(default=None, max_length=10_000)
    status: DocumentStatus | None = None
    source_id: str | None = None


class ExportRequest(BaseModel):
    status: DocumentStatus | None = None
    source_id: str | None = None


class TagUpdate(BaseModel):
    facets: dict[str, list[str]] = Field(default_factory=dict)
    acl: dict[str, list[str] | int] | None = None
    approve: bool = True


class ConfigWrite(BaseModel):
    yaml: str = Field(max_length=2_000_000)


class ExplainRequest(BaseModel):
    attributes: dict[str, list[str] | int | str] = Field(default_factory=dict)
    roles: list[str] = Field(default_factory=list)


class DevTokenRequest(BaseModel):
    principal_id: str
