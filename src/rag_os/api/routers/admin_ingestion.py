"""Ingestion status report, retries, dead letters, controls, exports, sources and SME review queue."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from rag_os.api.deps import get_container, require_role
from rag_os.api.schemas import ExportRequest, RetryRequest, TagUpdate
from rag_os.application.ports import DocumentQuery
from rag_os.composition import Container
from rag_os.domain.access import Principal
from rag_os.domain.documents import DocumentRecord, DocumentStatus, ReviewStatus, TagSet, tags_hash
from rag_os.domain.errors import NotFound, NotSupported, ValidationFailed
from rag_os.domain.ingestion import IngestionControls, IngestMessage, Lane, MessageMode

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin: ingestion"])
admin = require_role("admin")
reviewer = require_role("admin", "reviewer", "taxonomy_editor")

_summary_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_TERMINAL = {"INDEXED", "FAILED", "DELETED", "SKIPPED_UNCHANGED"}


@router.get("/ingestion/summary", summary="Totals by status, by source and by a facet (e.g. department)")
async def summary(group_by: str | None = Query(default=None), _: Principal = Depends(admin),
                  c: Container = Depends(get_container)) -> dict[str, Any]:
    key = group_by or ""
    hit = _summary_cache.get(key)
    if hit and time.monotonic() - hit[0] < 10:
        return hit[1]
    rows = await asyncio.to_thread(c.state.summary, group_by)
    totals: dict[str, int] = {}
    for r in rows:
        if r["kind"] == "source":
            totals[r["status"]] = totals.get(r["status"], 0) + r["count"]
    try:
        depth = await c.queue.depth()
    except Exception as e:
        depth = {"error": f"{type(e).__name__}"}
    out = {
        "totals": totals,
        "by_source": [{"source_id": r["key"], "status": r["status"], "count": r["count"]}
                      for r in rows if r["kind"] == "source"],
        "by_facet": [{"value": r["key"], "status": r["status"], "count": r["count"]}
                     for r in rows if r["kind"] == "facet"],
        "queue": {
            "priority": {"active": depth.get("priority_active"), "dead_letter": depth.get("priority_dead_letter")},
            "bulk": {"active": depth.get("bulk_active"), "dead_letter": depth.get("bulk_dead_letter")},
        },
        "controls": c.state.get_controls().model_dump(mode="json"),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _summary_cache[key] = (time.monotonic(), out)
    return out


@router.get("/ingestion/runs", summary="Recent ingestion runs")
async def runs(source_id: str | None = None, limit: int = Query(default=50, le=500), _: Principal = Depends(admin),
               c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in c.state.list_runs(source_id, limit)]


@router.get("/ingestion/runs/{run_id}", summary="Run progress, throughput, ETA and error breakdown")
async def run_detail(run_id: str, _: Principal = Depends(admin), c: Container = Depends(get_container)) -> dict[str, Any]:
    run = c.state.get_run(run_id)
    if run is None:
        raise NotFound("run not found")
    progress = c.state.run_progress(run_id)
    total = sum(progress.values())
    done = sum(n for s, n in progress.items() if s in _TERMINAL)
    elapsed_min = max((datetime.now(UTC) - run.started_at).total_seconds() / 60, 1 / 60)
    throughput = done / elapsed_min
    remaining = total - done
    return {
        "run": run.model_dump(mode="json"),
        "progress": progress,
        "percent": round(100 * done / total, 1) if total else 100.0,
        "throughput_per_min": round(throughput, 1),
        "eta_seconds": int(remaining / throughput * 60) if throughput > 0 and remaining else 0,
        "errors": c.state.error_breakdown(run_id),
    }


@router.get("/ingestion/documents", summary="Documents by status/source/facet with keyset paging")
async def documents(
    status: list[DocumentStatus] | None = Query(default=None),
    source_id: str | None = None,
    facet: str | None = Query(default=None, description="name:value"),
    q: str | None = Query(default=None, max_length=200),
    after: str | None = None,
    limit: int = Query(default=50, le=500),
    _: Principal = Depends(admin),
    c: Container = Depends(get_container),
) -> dict[str, Any]:
    fv = None
    if facet:
        name, _, value = facet.partition(":")
        if not value:
            raise ValidationFailed("facet must be name:value")
        fv = (name, value)
    # newest_first: doc_id is a content hash, so the default ordering is effectively random - never what
    # anyone scanning a document list wants.
    items, nxt = c.state.query(DocumentQuery(status=status, source_id=source_id, facet=fv, text=q, after=after,
                                             limit=limit, newest_first=True))
    return {"items": [i.model_dump(mode="json") for i in items], "next": nxt}


@router.get("/ingestion/documents/{doc_id}", summary="Document detail with event timeline")
async def document_detail(doc_id: str, _: Principal = Depends(admin),
                          c: Container = Depends(get_container)) -> dict[str, Any]:
    rec = c.state.get(doc_id)
    if rec is None:
        raise NotFound("document not found")
    return {"record": rec.model_dump(mode="json"),
            "events": [e.__dict__ | {"at": e.at.isoformat(), "status": e.status.value} for e in c.state.events(doc_id)]}


def _lane_for(c: Container, source_id: str) -> Lane:
    cfg = c.domain.sources.get(source_id)
    return cfg.lane if cfg else Lane.BULK


@router.post("/ingestion/retry", summary="Re-queue failed (or selected) documents")
async def retry(req: RetryRequest, _: Principal = Depends(admin), c: Container = Depends(get_container)) -> dict[str, int]:
    targets: list[DocumentRecord] = []
    if req.doc_ids:
        targets = [r for r in (c.state.get(d) for d in req.doc_ids) if r is not None]
    else:
        status = [req.status or DocumentStatus.FAILED]
        after = None
        while len(targets) < 100_000:
            page, after = c.state.query(DocumentQuery(status=status, source_id=req.source_id, after=after, limit=500))
            targets += page
            if not after:
                break
    nonce = uuid.uuid4().hex[:8]
    msgs = []
    for r in targets:
        if r.status == DocumentStatus.DELETED:
            continue
        c.state.transition(r.doc_id, DocumentStatus.QUEUED, stage="retry", attempts=0, event_message="manual retry")
        msgs.append(IngestMessage(doc_id=r.doc_id, version_key=r.version_key, source_id=r.source_id,
                                  lane=_lane_for(c, r.source_id), attempt_nonce=nonce, correlation_id=r.correlation_id))
    for i in range(0, len(msgs), 500):
        await c.queue.send(msgs[i:i + 500])
    return {"requeued": len(msgs)}


@router.get("/ingestion/dlq", summary="Peek dead-lettered messages")
async def dlq(lane: Lane = Lane.BULK, limit: int = Query(default=100, le=500), _: Principal = Depends(admin),
              c: Container = Depends(get_container)) -> dict[str, Any]:
    msgs = await c.queue.peek_dead_letters(lane, limit)
    return {"messages": [m.model_dump(mode="json") for m in msgs]}


@router.post("/ingestion/purge", summary="Free deleted documents: index chunks, staged blobs and state rows")
async def purge(retention_days: int = Query(default=7, ge=0, le=3650),
                limit: int = Query(default=1000, ge=1, le=10_000),
                apply: bool = Query(default=False, description="false (the default) reports without deleting"),
                principal: Principal = Depends(admin),
                c: Container = Depends(get_container)) -> dict[str, Any]:
    """Dry run unless `apply` is true: this is the one admin call that destroys data.

    A blob is content-addressed and so may be shared by several documents; `blobs_kept_shared` counts the ones
    left in place because a live document still references that content.
    """
    report = await c.purge.run(retention_days=retention_days, limit=limit, dry_run=not apply)
    if apply:
        log.warning("purge applied", extra={"by": principal.subject, **{
            k: v for k, v in report.as_dict().items() if k != "errors"}})
    return report.as_dict()


@router.get("/ingestion/controls", response_model=IngestionControls, summary="Worker controls")
async def get_controls(_: Principal = Depends(admin), c: Container = Depends(get_container)) -> IngestionControls:
    return c.state.get_controls()


@router.put("/ingestion/controls", response_model=IngestionControls, summary="Pause/resume/throttle ingestion")
async def put_controls(ctl: IngestionControls, principal: Principal = Depends(admin),
                       c: Container = Depends(get_container)) -> IngestionControls:
    if ctl.max_concurrency is not None and not (1 <= ctl.max_concurrency <= 256):
        raise ValidationFailed("max_concurrency must be between 1 and 256")
    ctl = ctl.model_copy(update={"updated_by": principal.subject})
    c.state.set_controls(ctl)
    log.info("ingestion controls changed", extra=ctl.model_dump(mode="json"))
    return c.state.get_controls()


@router.post("/ingestion/export", summary="Export the document status report as CSV")
async def export(req: ExportRequest, _: Principal = Depends(admin), c: Container = Depends(get_container)) -> dict[str, str]:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["doc_id", "source_id", "path", "status", "stage", "attempts", "error_type", "error_message",
                "chunk_count", "review_status", "facets", "updated_at", "indexed_at"])
    after = None
    rows = 0
    while rows < 1_000_000:
        page, after = c.state.query(DocumentQuery(status=[req.status] if req.status else None,
                                                  source_id=req.source_id, after=after, limit=500))
        for r in page:
            w.writerow([r.doc_id, r.source_id, r.path, r.status.value, r.stage, r.attempts, r.error_type,
                        (r.error_message or "")[:500], r.chunk_count, r.review_status.value,
                        ";".join(f"{k}={'|'.join(v)}" for k, v in r.tags.facets.items()), r.updated_at, r.indexed_at])
        rows += len(page)
        if not after:
            break
    name = f"ingestion-report-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}.csv"
    await asyncio.to_thread(c.raw.put_export, name, buf.getvalue().encode("utf-8"), "text/csv")
    return {"name": name, "url": f"/api/admin/ingestion/exports/{name}"}


@router.get("/ingestion/exports/{name}", summary="Download an export")
async def download_export(name: str, _: Principal = Depends(admin),
                          c: Container = Depends(get_container)) -> StreamingResponse:
    stream = c.raw.open_export(name)
    return StreamingResponse(iter(lambda: stream.read(65536), b""), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/sources", summary="Configured source instances")
async def sources(_: Principal = Depends(admin), c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    out = []
    for s in c.domain.sources.sources:
        last = c.state.last_run_started(s.id)
        out.append({"id": s.id, "type": s.type, "enabled": s.enabled, "domain": s.domain, "lane": s.lane.value,
                    "schedule": s.schedule, "last_run_started": last.isoformat() if last else None})
    return out


@router.post("/sources/{source_id}/sync", summary="Start a discovery run now")
async def sync_source(source_id: str, _: Principal = Depends(admin),
                      c: Container = Depends(get_container)) -> dict[str, str]:
    cfg = c.domain.sources.get(source_id)
    if cfg is None:
        raise NotFound("unknown source")
    if cfg.type == "upload":
        raise ValidationFailed("upload sources have nothing to sync")
    src = c.source_factory.create(cfg)
    try:
        await asyncio.to_thread(src.healthcheck)
    except NotSupported:
        raise
    except Exception as e:
        raise ValidationFailed(
            f"source '{source_id}' is not reachable from the API ({e}). Local folders must be synced where they "
            f"are mounted: `rag-os discover --source {source_id}`") from e
    run = c.state.start_run(source_id, "manual")

    async def _bg() -> None:
        try:
            await c.discover.run(src, "manual", run=run)
        except Exception:
            log.exception("manual discovery failed", extra={"source_id": source_id})

    task = asyncio.create_task(_bg())
    c.__dict__.setdefault("_bg_tasks", set()).add(task)
    task.add_done_callback(c.__dict__["_bg_tasks"].discard)
    return {"run_id": run.run_id}


@router.get("/review-queue", summary="Documents whose automatic classification needs SME review")
async def review_queue(after: str | None = None, limit: int = Query(default=50, le=200), _: Principal = Depends(reviewer),
                       c: Container = Depends(get_container)) -> dict[str, Any]:
    items, nxt = c.state.query(DocumentQuery(review_pending=True, after=after, limit=limit))
    return {"items": [i.model_dump(mode="json") for i in items], "next": nxt}


@router.post("/documents/{doc_id}/tags", response_model=DocumentRecord, summary="Set/approve facets (and ACL: admin)")
async def set_tags(doc_id: str, body: TagUpdate, principal: Principal = Depends(reviewer),
                   c: Container = Depends(get_container)) -> DocumentRecord:
    rec = c.state.get(doc_id)
    if rec is None:
        raise NotFound("document not found")
    facets: dict[str, list[str]] = {}
    for name, values in body.facets.items():
        fd = c.domain.facets.get(name)
        if fd is None:
            raise ValidationFailed(f"unknown facet '{name}'")
        canon = [x for x in (fd.normalise(v) for v in values) if x]
        if canon:
            facets[name] = canon
    new_facets = {**rec.tags.facets, **facets}
    acl = rec.tags.acl
    if body.acl is not None:
        if not principal.is_admin:
            raise ValidationFailed("only admins can change document ACLs")
        acl = c.engine.validate_doc_acl(body.acl)
    sources = {**rec.tags.sources, **{f"facet:{k}": f"review:{principal.subject}" for k in facets}}
    tags = TagSet(facets=new_facets, acl=acl, sources=sources)
    updated = c.state.update_tags(doc_id, tags, ReviewStatus.APPROVED.value if body.approve else None)
    if updated.indexed_version:
        await c.queue.send([IngestMessage(doc_id=doc_id, version_key=updated.version_key, source_id=updated.source_id,
                                          lane=Lane.PRIORITY, mode=MessageMode.RETAG, tags_hash=tags_hash(tags))])
    return updated
