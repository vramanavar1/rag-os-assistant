"""Ingestion worker (Container App scaled 0..N by KEDA on queue length).

* Refuses to process until the embedding-profile guard passes (never indexes vectors from the wrong model).
* Bounded concurrency; honours operator controls (pause / max concurrency / paused sources) re-read every 30s.
* Permanent errors -> FAILED immediately; transient errors -> backoff + retry until max delivery -> FAILED + DLQ.
* SIGTERM -> stop receiving, drain in-flight work, abandon the rest (messages are redelivered elsewhere).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time

from rag_os.application.ports import ReceivedMessage
from rag_os.application.use_cases.process_item import PERMANENT_ERRORS
from rag_os.composition import Container
from rag_os.domain.answers import TokenUsage
from rag_os.domain.documents import DocumentStatus
from rag_os.domain.errors import Conflict, NotFound
from rag_os.domain.ingestion import IngestionControls
from rag_os.infrastructure.telemetry import correlation_id_var, record_ingest, record_tokens, span

log = logging.getLogger("rag_os.worker")


class Worker:
    def __init__(self, c: Container) -> None:
        self.c = c
        self.s = c.settings
        self.stop = asyncio.Event()
        self.inflight: set[asyncio.Task[None]] = set()
        self.controls = IngestionControls()
        self._controls_at = 0.0
        self._config_at = time.monotonic()
        self.processed = 0

    async def _wait_for_guard(self) -> bool:
        while not self.stop.is_set():
            st = await self.c.guard.check(self.c.index, {"ingest": self.c.embed_ingest}, force=True)
            if st.ok:
                log.info("embedding profile verified", extra={"fingerprint": self.c.guard.fp})
                return True
            log.error("worker idle: embedding profile guard failing", extra={"reasons": st.reasons})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=30)
        return False

    async def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._controls_at >= self.s.ingest_controls_refresh_s:
            self.controls = await asyncio.to_thread(self.c.state.get_controls)
            self._controls_at = now
        if now - self._config_at >= 60:
            self._config_at = now
            try:
                await self.c.reload_config(ensure_index=False)
            except Exception:
                log.exception("configuration refresh failed")

    async def run(self) -> None:
        if not await self._wait_for_guard():
            return
        log.info("worker started", extra={"max_concurrency": self.s.ingest_max_concurrency, "queue": self.s.queue})
        while not self.stop.is_set():
            await self._refresh()
            if self.controls.paused:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), timeout=5)
                continue
            limit = self.controls.max_concurrency or self.s.ingest_max_concurrency
            free = limit - len(self.inflight)
            if free <= 0:
                await asyncio.wait(self.inflight, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                continue
            try:
                msgs = await self.c.queue.receive(min(free, self.s.ingest_receive_batch), wait_seconds=5)
            except Exception:
                log.exception("queue receive failed")
                await asyncio.sleep(5)
                continue
            for m in msgs:
                if m.message.source_id in self.controls.paused_sources:
                    await self.c.queue.abandon(m)
                    await asyncio.sleep(0.2)
                    continue
                t = asyncio.create_task(self._handle(m))
                self.inflight.add(t)
                t.add_done_callback(self.inflight.discard)
        if self.inflight:
            log.info("draining in-flight work", extra={"count": len(self.inflight)})
            await asyncio.wait(self.inflight, timeout=30)

    async def _keep_lock(self, m: ReceivedMessage) -> None:
        interval = max(10, self.s.queue_lock_seconds // 2)
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self.c.queue.renew(m)

    def _fail(self, doc_id: str, e: BaseException, attempts: int) -> None:
        with contextlib.suppress(NotFound, Conflict):
            self.c.state.transition(doc_id, DocumentStatus.FAILED, error_type=type(e).__name__,
                                    error_message=str(e)[:2000], attempts=attempts)

    async def _handle(self, m: ReceivedMessage) -> None:
        msg = m.message
        token = correlation_id_var.set(msg.correlation_id or msg.doc_id)
        keeper = asyncio.create_task(self._keep_lock(m))
        try:
            with span("rag.ingest", doc_id=msg.doc_id, mode=msg.mode.value, lane=msg.lane.value,
                      source_id=msg.source_id, delivery=m.delivery_count):
                outcome = await self.c.processor.handle(msg)
            await self.c.queue.complete(m)
            self.processed += 1
            record_ingest(outcome.status, msg.source_id, chunks=outcome.chunks, reused=outcome.reused)
            if outcome.embedding_tokens:
                record_tokens(TokenUsage(embedding=outcome.embedding_tokens), self.c.profile.provider,
                              self.c.profile.model, purpose="ingest_embed")
            log.info("document processed", extra={"doc_id": msg.doc_id, "outcome": outcome.status,
                                                   "chunks": outcome.chunks, "seconds": round(outcome.seconds, 2)})
        except PERMANENT_ERRORS as e:
            self._fail(msg.doc_id, e, m.delivery_count)
            await self.c.queue.complete(m)
            record_ingest("failed", msg.source_id)
            log.warning("document failed permanently", extra={"doc_id": msg.doc_id, "error_type": type(e).__name__,
                                                               "error": str(e)[:300]})
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.c.queue.abandon(m)
            raise
        except Exception as e:
            if m.delivery_count >= self.s.queue_max_delivery:
                self._fail(msg.doc_id, e, m.delivery_count)
                await self.c.queue.dead_letter(m, type(e).__name__, str(e)[:900])
                record_ingest("dead_lettered", msg.source_id)
                log.error("document dead-lettered", extra={"doc_id": msg.doc_id, "error_type": type(e).__name__})
            else:
                with contextlib.suppress(NotFound, Conflict):
                    self.c.state.transition(msg.doc_id, DocumentStatus.QUEUED, error_type=type(e).__name__,
                                            error_message=str(e)[:2000],
                                            event_message=f"transient error, retry {m.delivery_count}")
                await asyncio.sleep(min(2 ** m.delivery_count, 60))  # backoff while holding the lock
                await self.c.queue.abandon(m)
                log.warning("document retry scheduled", extra={"doc_id": msg.doc_id, "error_type": type(e).__name__,
                                                                "delivery": m.delivery_count})
        finally:
            keeper.cancel()
            correlation_id_var.reset(token)


async def main_async(c: Container, *, once: bool = False) -> int:
    w = Worker(c)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, w.stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: w.stop.set())
    if once:
        async def _idle_stop() -> None:
            idle = 0
            while not w.stop.is_set():
                await asyncio.sleep(1)
                idle = 0 if w.inflight else idle + 1
                depth = await c.queue.depth()
                if idle >= 3 and not (depth.get("priority_active") or depth.get("bulk_active")):
                    w.stop.set()

        asyncio.create_task(_idle_stop())  # noqa: RUF006
    try:
        await w.run()
    finally:
        await c.aclose()
    return w.processed
