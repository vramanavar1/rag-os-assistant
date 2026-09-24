"""Document upload (priority lane) + status tracking."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile

from rag_os.api.deps import get_container, get_principal
from rag_os.api.schemas import UploadResponse
from rag_os.composition import Container
from rag_os.domain.access import Principal
from rag_os.domain.documents import DocumentRecord, SourceItem, TagSet
from rag_os.domain.errors import AccessDenied, ConfigError, NotFound, ValidationFailed
from rag_os.infrastructure.parsers import parser_for
from rag_os.infrastructure.telemetry import correlation_id_var

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def _default_acl(c: Container, principal: Principal) -> dict[str, list[str] | int]:
    """Non-admin uploads are visible to the uploader's own scope (never wider)."""
    pol = c.domain.policy
    acl: dict[str, list[str] | int] = {}
    for name in [*pol.combine.all_of, *pol.combine.grant_any_of]:
        v = principal.attributes.get(name)
        if v is not None and v != []:
            acl[name] = v
    return acl


@router.post("", response_model=UploadResponse, status_code=202, summary="Upload a document for ingestion")
async def upload(
    file: UploadFile = File(...),
    facets: str | None = Form(default=None, description='JSON, e.g. {"doc_type": ["Policy"]}'),
    principal: Principal = Depends(get_principal),
    c: Container = Depends(get_container),
) -> UploadResponse:
    import json

    cfg = c.domain.sources.get(c.settings.upload_source_id)
    if cfg is None or not cfg.enabled:
        raise ConfigError(f"upload source '{c.settings.upload_source_id}' is not configured in sources.yaml")
    allowed = set(cfg.settings.get("allowed_roles", ["admin", "contributor"]))
    if not (principal.roles & allowed):
        raise AccessDenied("uploading requires the contributor or admin role")
    filename = (file.filename or "upload.bin").replace("\\", "/").split("/")[-1]
    parser_for(filename, file.content_type)  # raises NotSupported for unknown formats
    limit = c.settings.upload_max_mb * 1024 * 1024

    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)  # noqa: SIM115
    digest = hashlib.sha256()
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            spool.close()
            raise ValidationFailed(f"file exceeds {c.settings.upload_max_mb} MB")
        digest.update(chunk)
        spool.write(chunk)
    spool.seek(0)

    tags = TagSet()
    if facets:
        try:
            parsed: Any = json.loads(facets)
            tags = TagSet(facets={k: [str(x) for x in (v if isinstance(v, list) else [v])] for k, v in parsed.items()})
        except (ValueError, AttributeError) as e:
            raise ValidationFailed("facets must be a JSON object") from e
    tags = tags.merged_with(TagSet(acl=_default_acl(c, principal)), "uploader")

    tracking_id = uuid.uuid4().hex
    item_id = f"{datetime.now(UTC):%Y/%m/%d}/{tracking_id}/{filename}"
    doc_id = SourceItem(source_id=cfg.id, item_id=item_id, path=item_id).doc_id
    uri = await asyncio.to_thread(c.raw.stage, cfg.id, doc_id, filename, spool)
    spool.close()
    item = SourceItem(
        source_id=cfg.id, item_id=item_id, path=f"{principal.subject}/{filename}", uri=uri,
        content_hash=digest.hexdigest(), size=size, content_type=file.content_type,
        metadata={"tracking_id": tracking_id, "correlation_id": correlation_id_var.get()}, sidecar=tags,
    )
    source = c.source_factory.create(cfg)
    await c.discover.submit(source, [item], trigger="upload")
    rec = c.state.by_tracking_id(tracking_id)
    assert rec is not None
    return UploadResponse(tracking_id=tracking_id, doc_id=rec.doc_id, status=rec.status)


@router.get("/{tracking_id}", response_model=DocumentRecord, summary="Status of an uploaded document")
async def upload_status(tracking_id: str, principal: Principal = Depends(get_principal),
                        c: Container = Depends(get_container)) -> DocumentRecord:
    rec = c.state.by_tracking_id(tracking_id)
    if rec is None or not (principal.is_admin or rec.path.startswith(f"{principal.subject}/")):
        raise NotFound("upload not found")
    return rec
