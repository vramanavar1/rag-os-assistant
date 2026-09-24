"""Request/response models (OpenAPI is generated from these at /api/docs)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from rag_os.domain.answers import ChatTurn
from rag_os.domain.documents import DocumentStatus


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
