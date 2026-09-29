"""DiscoverSource: list a source, detect changes, stage bytes if needed, enqueue claim-check messages.

Streaming and batched so a multi-million item source runs in constant memory. Unchanged items cost one
state lookup (no embedding, no queue message); tag-only changes enqueue a cheap RETAG message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from rag_os.application.ports import (
    DocumentSource,
    IngestionStateStore,
    MessageQueue,
    RawDocumentStore,
    Staged,
)
from rag_os.application.services.tagging import TagResolver
from rag_os.domain.documents import DocumentRecord, DocumentStatus, SourceItem, tags_hash
from rag_os.domain.ingestion import IngestionRun, IngestMessage, MessageMode, RunStatus, SourceConfig

log = logging.getLogger(__name__)


class DiscoverSource:
    def __init__(self, state: IngestionStateStore, queue: MessageQueue, raw: RawDocumentStore,
                 tagger: TagResolver, *, batch_size: int = 500, embedding_fp: str | None = None) -> None:
        self.state = state
        self.queue = queue
        self.raw = raw
        self.tagger = tagger
        self.batch_size = batch_size
        self.embedding_fp = embedding_fp   # changing the profile must re-queue the whole corpus

    async def submit(self, source: DocumentSource, items: list[SourceItem], trigger: str = "upload") -> IngestionRun:
        """Submit specific items (e.g. an upload) without listing the source or marking deletions."""
        cfg = source.config
        run = self.state.start_run(cfg.id, trigger)
        try:
            batch = []
            for item in items:
                tags = self.tagger.resolve(cfg, item, {})
                if item.sidecar is not None:
                    tags = tags.merged_with(item.sidecar, "uploader")
                batch.append((item, DocumentRecord(
                    doc_id=item.doc_id, source_id=cfg.id, item_id=item.item_id, path=item.path, blob_uri=item.uri,
                    content_hash=item.content_hash, version_key=item.version_key, tags=tags,
                    content_type=item.content_type, size=item.size,
                    tracking_id=item.metadata.get("tracking_id"), correlation_id=item.metadata.get("correlation_id"),
                )))
            await self._flush(source, cfg, run, batch)
            run.status = RunStatus.COMPLETED
        except Exception as e:
            run.status = RunStatus.FAILED
            run.error = f"{type(e).__name__}: {e}"[:2000]
            raise
        finally:
            run.finished_at = datetime.now(UTC)
            self.state.finish_run(run)
        return run

    async def run(self, source: DocumentSource, trigger: str, run: IngestionRun | None = None) -> IngestionRun:
        cfg = source.config
        run = run or self.state.start_run(cfg.id, trigger)
        log.info("discovery started", extra={"source_id": cfg.id, "run_id": run.run_id, "trigger": trigger})
        try:
            manifest = await asyncio.to_thread(source.read_manifest)
            batch: list[tuple[SourceItem, DocumentRecord]] = []
            iterator = iter(source.iter_items())
            while True:
                item = await asyncio.to_thread(next, iterator, None)
                if item is None:
                    break
                tags = self.tagger.resolve(cfg, item, manifest)
                rec = DocumentRecord(
                    doc_id=item.doc_id, source_id=cfg.id, item_id=item.item_id, path=item.path, blob_uri=item.uri,
                    version_key=item.version_key, tags=tags, content_type=item.content_type, size=item.size,
                    tracking_id=item.metadata.get("tracking_id"), correlation_id=run.run_id,
                )
                batch.append((item, rec))
                if len(batch) >= self.batch_size:
                    await self._flush(source, cfg, run, batch)
                    batch = []
            if batch:
                await self._flush(source, cfg, run, batch)
            if cfg.full_listing and cfg.type != "upload":
                deleted = self.state.mark_unseen_deleted(cfg.id, run.run_id)
                run.deleted = len(deleted)
                if deleted:
                    await self.queue.send([
                        IngestMessage(doc_id=d, version_key="deleted", source_id=cfg.id, lane=cfg.lane,
                                      mode=MessageMode.DELETE, run_id=run.run_id, correlation_id=run.run_id)
                        for d in deleted
                    ])
            run.status = RunStatus.COMPLETED
        except Exception as e:
            run.status = RunStatus.FAILED
            run.error = f"{type(e).__name__}: {e}"[:2000]
            log.exception("discovery failed", extra={"source_id": cfg.id, "run_id": run.run_id})
            raise
        finally:
            run.finished_at = datetime.now(UTC)
            self.state.finish_run(run)
            log.info("discovery finished", extra={"source_id": cfg.id, "run_id": run.run_id,
                                                  "discovered": run.discovered, "queued": run.queued,
                                                  "unchanged": run.unchanged, "deleted": run.deleted})
        return run

    async def _flush(self, source: DocumentSource, cfg: SourceConfig, run: IngestionRun,
                     batch: list[tuple[SourceItem, DocumentRecord]]) -> None:
        run.discovered += len(batch)
        items = {rec.doc_id: item for item, rec in batch}
        delta = self.state.upsert_discovered([rec for _, rec in batch], run.run_id, self.embedding_fp)
        run.unchanged += delta.unchanged + delta.skipped_failed + delta.in_flight
        messages: list[IngestMessage] = []
        queued_ids: list[str] = []
        for rec in delta.full:
            item = items[rec.doc_id]
            if item.metadata.get("too_large"):
                self.state.transition(rec.doc_id, DocumentStatus.FAILED, stage="discovery",
                                      error_type="FileTooLarge", error_message=f"{item.size} bytes exceeds limit")
                continue
            if source.staging_required:
                try:
                    staged = await asyncio.to_thread(self._stage, source, item)
                except Exception as e:
                    self.state.transition(rec.doc_id, DocumentStatus.FAILED, stage="staging",
                                          error_type=type(e).__name__, error_message=str(e)[:1000])
                    continue
                self.state.transition(rec.doc_id, DocumentStatus.QUEUED, blob_uri=staged.uri,
                                      content_hash=staged.content_hash)
            else:
                queued_ids.append(rec.doc_id)
            messages.append(IngestMessage(doc_id=rec.doc_id, version_key=rec.version_key, source_id=cfg.id,
                                          lane=cfg.lane, run_id=run.run_id, correlation_id=rec.correlation_id))
        for rec in delta.retag:
            messages.append(IngestMessage(doc_id=rec.doc_id, version_key=rec.version_key, source_id=cfg.id,
                                          lane=cfg.lane, mode=MessageMode.RETAG, tags_hash=tags_hash(rec.tags),
                                          run_id=run.run_id, correlation_id=rec.correlation_id))
        self.state.mark_queued(queued_ids)
        if messages:
            await self.queue.send(messages)
        run.queued += len(messages)

    def _stage(self, source: DocumentSource, item: SourceItem) -> Staged:
        # The hash comes back even when the source could not supply one (a local folder, where change
        # detection is otherwise size+mtime): staging reads every byte anyway.
        with source.open(item) as stream:
            return self.raw.stage(source.id, item.doc_id, item.path.split("/")[-1], stream,
                                  content_hash=item.content_hash)
