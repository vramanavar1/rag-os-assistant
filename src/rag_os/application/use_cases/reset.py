"""Reset all data: start again from an empty system, keeping its configuration.

For testing and for starting afresh. Removes every indexed chunk, every stored copy (staged blobs and exports),
every document row, status event and ingestion run, every queued message, and - unless told not to - every query
trace and expectation. Keeps: configuration (sources, access policy, facets, path rules, embedding profile), the
index itself with its schema and profile stamp (so nothing needs a bootstrap), people and their attributes
(those live in Entra), and telemetry already sent to Application Insights (not ours to delete).

Ingestion is paused first and LEFT paused, so nothing re-ingests until an administrator resumes it - after a
reset the next scheduled sync would otherwise start re-embedding every crawled document at once.

Every step is idempotent, so a reset that stopped part-way is finished by running it again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from rag_os.application.ports import (
    IndexSchema,
    IngestionStateStore,
    MessageQueue,
    QueryTraceStore,
    RawDocumentStore,
    SearchIndex,
)

log = logging.getLogger(__name__)
audit = logging.getLogger("rag_os.audit")


class ResetAllData:
    def __init__(self, *, state: IngestionStateStore, index: SearchIndex, raw: RawDocumentStore,
                 queue: MessageQueue, traces: QueryTraceStore | None, schema: IndexSchema,
                 profile: dict[str, Any], on_index_cleared: Callable[[], None] = lambda: None) -> None:
        self.state = state
        self.index = index
        self.raw = raw
        self.queue = queue
        self.traces = traces
        self.schema = schema
        self.profile = profile
        self.on_index_cleared = on_index_cleared

    async def run(self, *, by: str, include_traces: bool = True) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        audit.warning("data reset started", extra={"by": by, "index": self.schema.name,
                                                   "include_traces": include_traces})

        async def step(name: str, fn: Callable[[], Any]) -> None:
            t0 = time.perf_counter()
            try:
                result = fn()
                if hasattr(result, "__await__"):
                    result = await result
                steps.append({"step": name, "result": result, "ms": round((time.perf_counter() - t0) * 1000)})
            except Exception as e:
                steps.append({"step": name, "error": f"{type(e).__name__}: {e}"[:500],
                              "ms": round((time.perf_counter() - t0) * 1000)})
                raise

        def pause() -> dict[str, Any]:
            ctl = self.state.get_controls()
            was = ctl.paused
            self.state.set_controls(ctl.model_copy(update={"paused": True, "updated_by": f"reset:{by}"}))
            return {"paused": True, "was_paused": was}

        async def clear_index() -> dict[str, int]:
            n = await self.index.clear(self.schema, self.profile)
            self.on_index_cleared()
            return {"chunks": n}

        async def sweep() -> dict[str, int]:
            # A worker that was mid-document when ingestion paused can still upsert after the clear.
            left = await self.index.count()
            if left:
                await self.index.clear(self.schema, self.profile)
                self.on_index_cleared()
            return {"chunks_swept": left}

        try:
            await step("pause ingestion", pause)
            # Bounded: the console request goes through a proxy with a ~120 s read timeout; a reset that stops
            # part-way is finished by running it again (or by `rag-os reset`, which has no proxy in front).
            await step("empty the queue", lambda: self._count(self.queue.purge_all(time_budget_s=45)))
            await step("empty the search index", clear_index)
            await step("delete stored copies and exports", self.raw.clear_staged)
            await step("delete documents, history and runs", self.state.clear_all)
            if include_traces and self.traces is not None:
                await step("delete query traces and expectations", self.traces.clear_all)
            await step("final index sweep", sweep)
        except Exception:
            log.exception("data reset stopped part-way", extra={"by": by})
            audit.warning("data reset stopped part-way", extra={"by": by, "steps": steps})
            return {"ok": False, "index": self.schema.name, "steps": steps,
                    "message": "The reset stopped part-way. Every step is safe to repeat: run it again."}
        audit.warning("data reset completed", extra={"by": by, "steps": steps})
        return {"ok": True, "index": self.schema.name, "steps": steps,
                "message": "Everything is cleared. Ingestion is paused: resume it in Controls when ready."}

    @staticmethod
    async def _count(coro: Any) -> dict[str, int]:
        return {"messages": int(await coro)}
