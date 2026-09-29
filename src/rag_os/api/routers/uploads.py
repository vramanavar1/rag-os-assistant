"""Document upload (priority lane) + status tracking."""

from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from rag_os.api.deps import get_container, get_principal
from rag_os.api.schemas import UploadListResponse, UploadResponse, UploadSummary
from rag_os.application.ports import DocumentQuery
from rag_os.composition import Container
from rag_os.domain.access import Principal
from rag_os.domain.documents import DocumentRecord, DocumentStatus, SourceItem, TagSet
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


# A browser cannot tell us a file's absolute path (File.path does not exist outside Electron). The most it
# offers is webkitRelativePath, which is relative to the FOLDER THE PERSON PICKED - so picking `HR` yields
# `HR/UK/policies/x.pdf` and every path rule fires, while picking `policies` yields one segment and none do.
MAX_REL_DEPTH = 16
MAX_REL_CHARS = 400


def _safe_relative_dir(raw: str | None) -> list[str]:
    """The directory segments of a client-supplied relative path, or [] if none was sent.

    Rejects rather than sanitises silently: this string reaches the stored `path`, which is also the ownership
    boundary for listing (see `_scope`), so quietly repairing a hostile value is how a traversal becomes a
    cross-tenant read. The final segment is dropped - the filename always comes from the flattening below.
    """
    if raw is None or not raw.strip():
        return []
    value = raw.replace("\\", "/").strip()
    if len(value) > MAX_REL_CHARS:
        raise ValidationFailed(f"relative_path exceeds {MAX_REL_CHARS} characters")
    if any(ord(ch) < 32 for ch in value):
        raise ValidationFailed("relative_path contains control characters")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValidationFailed("relative_path must be relative, not absolute")
    segments = value.split("/")[:-1]  # drop the filename
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ValidationFailed("relative_path must not contain empty, '.' or '..' segments")
    if len(segments) > MAX_REL_DEPTH:
        raise ValidationFailed(f"relative_path is deeper than {MAX_REL_DEPTH} folders")
    return segments


def _existing_upload(c: Container, principal: Principal, content_hash: str) -> DocumentRecord | None:
    """This uploader's live document holding exactly these bytes, if there is one.

    Scoped by path prefix, which is how ownership is expressed for uploads throughout (`_scope`). DELETED rows
    are excluded: re-uploading something you deleted has to work.
    """
    found, _ = c.state.query(DocumentQuery(
        content_hash=content_hash, path_prefix=f"{principal.subject}/", source_id=c.settings.upload_source_id,
        limit=1, newest_first=True,
    ))
    return found[0] if found else None


def _describe(rec: DocumentRecord, relative_path: str | None, path_facets: list[str],
              duplicate_of: str | None = None) -> UploadResponse:
    """What was actually stored, with provenance - so the UI can show that a rule fired, and say so when a
    path produced nothing (the "I picked the wrong folder" case)."""
    return UploadResponse(
        tracking_id=rec.tracking_id or "", doc_id=rec.doc_id, status=rec.status,
        relative_path=relative_path,
        facets=rec.tags.facets,
        facet_sources={k[len("facet:"):]: v for k, v in rec.tags.sources.items() if k.startswith("facet:")},
        facets_from_path=path_facets,
        duplicate_of=duplicate_of,
    )


@router.post("", response_model=UploadResponse, status_code=202, summary="Upload a document for ingestion")
async def upload(
    file: UploadFile = File(...),
    facets: str | None = Form(default=None, description='JSON, e.g. {"doc_type": ["Policy"]}'),
    relative_path: str | None = Form(
        default=None,
        description="Path relative to the folder the uploader picked, e.g. HR/UK/policies/leave.pdf. "
                    "Facets are derived from it via path-rules.yaml; ACL never is.",
    ),
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

    # The folder the uploader picked, then what they explicitly chose - a deliberate pick beats the folder it
    # happened to sit in. Rules are evaluated per file, so a dropped tree spanning departments tags each file
    # from its own folder.
    rel_dir = _safe_relative_dir(relative_path)
    rel_path = "/".join([*rel_dir, filename])
    tags = c.tagger.facets_from_path(cfg.id, rel_path) if rel_dir else TagSet()
    path_facets = sorted(tags.facets)
    if facets:
        try:
            parsed: Any = json.loads(facets)
            chosen = {k: [str(x) for x in (v if isinstance(v, list) else [v])] for k, v in parsed.items()}
        except (ValueError, AttributeError) as e:
            raise ValidationFailed("facets must be a JSON object") from e
        # Refused up front rather than dropped at index time: an interactive uploader is owed the reason.
        canon, rejected = c.tagger.canonical_facets(chosen)
        if rejected:
            raise ValidationFailed("; ".join(rejected))
        tags = tags.merged_with(canon, "uploader")
    tags = tags.merged_with(TagSet(acl=_default_acl(c, principal)), "uploader")

    content_hash = digest.hexdigest()

    # Already uploaded by THIS person? Then this is the same document, not a new one. Scoped to one uploader
    # deliberately: two people uploading the same file have different access tags and each needs it in their
    # own list, so only a repeat by the same subject collapses. Nothing is staged and nothing is ingested.
    existing = _existing_upload(c, principal, content_hash)
    if existing is not None:
        spool.close()
        return _describe(existing, rel_path if rel_dir else None, path_facets, duplicate_of=existing.doc_id)

    tracking_id = uuid.uuid4().hex
    # item_id feeds doc_id (stable_id), so the client path must never reach it - it would turn one document
    # re-uploaded from two different folders into two documents.
    item_id = f"{datetime.now(UTC):%Y/%m/%d}/{tracking_id}/{filename}"
    doc_id = SourceItem(source_id=cfg.id, item_id=item_id, path=item_id).doc_id
    # Keyed by content, so a file two people upload - or one already crawled from a folder - is stored once.
    staged = await asyncio.to_thread(c.raw.stage, cfg.id, doc_id, filename, spool, content_hash)
    spool.close()
    item = SourceItem(
        # "<subject>/..." is load-bearing: it is the ownership test in `_scope`. The folders sit inside it,
        # so they show in the Documents list and are reachable by its text search.
        source_id=cfg.id, item_id=item_id, path=f"{principal.subject}/{rel_path}", uri=staged.uri,
        content_hash=content_hash, size=size, content_type=file.content_type,
        metadata={"tracking_id": tracking_id, "correlation_id": correlation_id_var.get()}, sidecar=tags,
    )
    source = c.source_factory.create(cfg)
    await c.discover.submit(source, [item], trigger="upload")
    rec = c.state.by_tracking_id(tracking_id)
    assert rec is not None
    return _describe(rec, rel_path if rel_dir else None, path_facets)


def _scope(principal: Principal) -> str | None:
    """What this caller may list. None means everything.

    The same ownership rule as the single-record read below: uploads are stored at "<subject>/<filename>"
    (see the SourceItem built in `upload`), so a path prefix IS the ownership test - expressed here as a
    query filter so the database applies it, rather than as a post-filter that would break paging.
    """
    return None if principal.is_admin else f"{principal.subject}/"


@router.get("", response_model=UploadListResponse, summary="Recent documents, newest first")
async def list_uploads(
    status: list[DocumentStatus] | None = Query(default=None, description="repeat to match any of several"),
    after: str | None = Query(default=None, description="cursor from the previous page's `next`"),
    limit: int = Query(default=10, ge=1, le=100),
    principal: Principal = Depends(get_principal),
    c: Container = Depends(get_container),
) -> UploadListResponse:
    """The list behind the chat page's recent-uploads panel and the admin console's Uploads view.

    One endpoint for both: the caller's role decides the scope, so there is a single paging implementation
    and a single access rule rather than two that can drift.
    """
    q = DocumentQuery(status=status, path_prefix=_scope(principal), after=after, limit=limit, newest_first=True)
    items, nxt = c.state.query(q)
    return UploadListResponse(
        items=[UploadSummary.of(r) for r in items], next=nxt, counts=c.state.count_by_status(q)
    )


@router.get("/{tracking_id}", response_model=DocumentRecord, summary="Status of an uploaded document")
async def upload_status(tracking_id: str, principal: Principal = Depends(get_principal),
                        c: Container = Depends(get_container)) -> DocumentRecord:
    rec = c.state.by_tracking_id(tracking_id)
    if rec is None or not (principal.is_admin or rec.path.startswith(f"{principal.subject}/")):
        raise NotFound("upload not found")
    return rec
