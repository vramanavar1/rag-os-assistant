"""In-process queue for unit tests (same semantics: priority lane first, delivery counts, dead-letter)."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence
from typing import Any

from rag_os.application.ports import MessageQueue, ReceivedMessage
from rag_os.domain.ingestion import IngestMessage, Lane
from rag_os.infrastructure.registry import QUEUES


@QUEUES.register("in_memory", description="Process-local queue (tests).")
class InMemoryQueue(MessageQueue):
    def __init__(self, max_delivery: int = 5, **_: Any) -> None:
        self.max_delivery = max_delivery
        self.lanes: dict[Lane, deque[tuple[IngestMessage, int]]] = {Lane.PRIORITY: deque(), Lane.BULK: deque()}
        self.dlq: dict[Lane, list[IngestMessage]] = {Lane.PRIORITY: [], Lane.BULK: []}
        self._seen: set[str] = set()
        self._inflight: dict[int, tuple[IngestMessage, int]] = {}
        self._lock = asyncio.Lock()

    async def send(self, messages: Sequence[IngestMessage]) -> None:
        async with self._lock:
            for m in messages:
                if m.message_id in self._seen:  # duplicate detection
                    continue
                self._seen.add(m.message_id)
                self.lanes[m.lane].append((m, 0))

    async def receive(self, max_messages: int, wait_seconds: float) -> list[ReceivedMessage]:
        out: list[ReceivedMessage] = []
        async with self._lock:
            for lane in (Lane.PRIORITY, Lane.BULK):
                q = self.lanes[lane]
                while q and len(out) < max_messages:
                    m, count = q.popleft()
                    count += 1
                    if count > self.max_delivery:
                        self.dlq[lane].append(m)
                        continue
                    handle = id(m) ^ count
                    self._inflight[handle] = (m, count)
                    out.append(ReceivedMessage(message=m, delivery_count=count, handle=handle))
        if not out and wait_seconds:
            await asyncio.sleep(min(wait_seconds, 0.05))
        return out

    async def complete(self, msg: ReceivedMessage) -> None:
        async with self._lock:
            entry = self._inflight.pop(msg.handle, None)
            if entry:
                self._seen.discard(entry[0].message_id)

    async def abandon(self, msg: ReceivedMessage) -> None:
        async with self._lock:
            entry = self._inflight.pop(msg.handle, None)
            if entry:
                self.lanes[entry[0].lane].append(entry)

    async def dead_letter(self, msg: ReceivedMessage, reason: str, description: str) -> None:
        async with self._lock:
            entry = self._inflight.pop(msg.handle, None)
            if entry:
                self.dlq[entry[0].lane].append(entry[0])

    async def depth(self) -> dict[str, int]:
        return {
            "priority_active": len(self.lanes[Lane.PRIORITY]),
            "priority_dead_letter": len(self.dlq[Lane.PRIORITY]),
            "bulk_active": len(self.lanes[Lane.BULK]),
            "bulk_dead_letter": len(self.dlq[Lane.BULK]),
        }

    async def peek_dead_letters(self, lane: Lane, max_messages: int) -> list[IngestMessage]:
        return list(self.dlq[lane][:max_messages])
