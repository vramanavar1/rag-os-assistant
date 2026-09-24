"""Scheduler tick (cron job every few minutes): run due source discoveries + reconcile stuck documents."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from croniter import croniter

from rag_os.application.ports import DocumentSource, IngestionStateStore, MessageQueue
from rag_os.application.use_cases.discover import DiscoverSource
from rag_os.domain.documents import DocumentStatus
from rag_os.domain.ingestion import IngestMessage, SourceConfig, SourcesFile

log = logging.getLogger(__name__)


def is_due(cfg: SourceConfig, last_started: datetime | None, now: datetime) -> bool:
    if not cfg.enabled or not cfg.schedule:
        return False
    if last_started is None:
        return True
    nxt = croniter(cfg.schedule, last_started).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=UTC)
    return bool(nxt <= now)


class Reconcile:
    """Re-enqueue documents stuck in flight (e.g. a crash between the DB write and the queue send)."""

    def __init__(self, state: IngestionStateStore, queue: MessageQueue, stale_minutes: int = 30,
                 batch: int = 1000) -> None:
        self.state = state
        self.queue = queue
        self.stale = timedelta(minutes=stale_minutes)
        self.batch = batch

    async def run(self, sources: SourcesFile) -> int:
        stuck = self.state.stale_in_flight(datetime.now(UTC) - self.stale, self.batch)
        if not stuck:
            return 0
        nonce = uuid.uuid4().hex[:8]
        msgs = []
        for rec in stuck:
            cfg = sources.get(rec.source_id)
            self.state.transition(rec.doc_id, DocumentStatus.QUEUED, stage="reconcile",
                                  event_message="re-enqueued by reconciliation")
            msgs.append(IngestMessage(doc_id=rec.doc_id, version_key=rec.version_key, source_id=rec.source_id,
                                      lane=cfg.lane if cfg else "bulk", attempt_nonce=nonce,  # type: ignore[arg-type]
                                      correlation_id=rec.correlation_id))
        await self.queue.send(msgs)
        log.warning("reconciled stuck documents", extra={"count": len(msgs)})
        return len(msgs)


class SchedulerTick:
    def __init__(self, state: IngestionStateStore, discover: DiscoverSource, reconcile: Reconcile,
                 build_source: Callable[[SourceConfig], DocumentSource]) -> None:
        self.state = state
        self.discover = discover
        self.reconcile = reconcile
        self.build_source = build_source

    async def run(self, sources: SourcesFile, *, now: datetime | None = None) -> dict[str, object]:
        now = now or datetime.now(UTC)
        ran: list[str] = []
        errors: dict[str, str] = {}
        paused = set(self.state.get_controls().paused_sources)
        for cfg in sources.sources:
            if cfg.id in paused or not is_due(cfg, self.state.last_run_started(cfg.id), now):
                continue
            try:
                src = self.build_source(cfg)
                if src.staging_required and cfg.type == "local_folder":
                    src.healthcheck()  # only runnable where the folder is mounted
                await self.discover.run(src, trigger="schedule")
                ran.append(cfg.id)
            except Exception as e:
                errors[cfg.id] = f"{type(e).__name__}: {e}"[:500]
                log.error("scheduled discovery failed", extra={"source_id": cfg.id, "error": errors[cfg.id]})
        reconciled = await self.reconcile.run(sources)
        return {"ran": ran, "errors": errors, "reconciled": reconciled}
