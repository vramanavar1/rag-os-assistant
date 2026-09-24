"""Database-backed queue for docker-compose development (shared by separate API/worker processes).

Mirrors Service Bus peek-lock semantics: visibility timeout, delivery count, max-delivery -> dead-letter,
duplicate detection by message id, priority lane drained first. Uses FOR UPDATE SKIP LOCKED on PostgreSQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from rag_os.application.ports import MessageQueue, ReceivedMessage
from rag_os.domain.ingestion import IngestMessage, Lane
from rag_os.infrastructure.registry import QUEUES
from rag_os.infrastructure.state.db import make_engine

_meta = MetaData()
_q = Table(
    "ingest_queue",
    _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("message_id", String(160), unique=True, nullable=False),
    Column("lane", String(16), nullable=False, index=True),
    Column("body", JSON, nullable=False),
    Column("visible_at", DateTime(timezone=True), nullable=False, index=True),
    Column("delivery_count", Integer, nullable=False, default=0),
    Column("dead_lettered", Boolean, nullable=False, default=False, index=True),
    Column("dl_reason", String(512)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _now() -> datetime:
    return datetime.now(UTC)


@QUEUES.register("sql", description="State-DB backed queue with peek-lock semantics (docker-compose dev).")
class SqlQueue(MessageQueue):
    def __init__(self, db_url: str, entra_auth: bool = False, max_delivery: int = 5, lock_seconds: int = 300,
                 **_: Any) -> None:
        self.engine = make_engine(db_url, entra_auth=entra_auth)
        _meta.create_all(self.engine)
        self.max_delivery = max_delivery
        self.lock = timedelta(seconds=lock_seconds)
        self._pg = self.engine.dialect.name == "postgresql"

    async def send(self, messages: Sequence[IngestMessage]) -> None:
        if not messages:
            return

        def _do() -> None:
            ins = (pg_insert if self._pg else sqlite_insert)(_q)
            now = _now()
            rows = [
                {"message_id": m.message_id, "lane": m.lane.value, "body": m.model_dump(mode="json"),
                 "visible_at": now, "delivery_count": 0, "dead_lettered": False, "created_at": now}
                for m in messages
            ]
            with self.engine.begin() as c:
                c.execute(ins.on_conflict_do_nothing(index_elements=["message_id"]), rows)

        await asyncio.to_thread(_do)

    def _claim(self, max_messages: int) -> list[ReceivedMessage]:
        out: list[ReceivedMessage] = []
        now = _now()
        with self.engine.begin() as c:
            for lane in (Lane.PRIORITY, Lane.BULK):
                need = max_messages - len(out)
                if need <= 0:
                    break
                stmt = (
                    select(_q.c.id, _q.c.body, _q.c.delivery_count)
                    .where(_q.c.lane == lane.value, _q.c.dead_lettered.is_(False), _q.c.visible_at <= now)
                    .order_by(_q.c.id)
                    .limit(need)
                )
                if self._pg:
                    stmt = stmt.with_for_update(skip_locked=True)
                for row in c.execute(stmt).all():
                    count = int(row.delivery_count) + 1
                    if count > self.max_delivery:
                        c.execute(update(_q).where(_q.c.id == row.id).values(
                            dead_lettered=True, dl_reason="MaxDeliveryCountExceeded"))
                        continue
                    c.execute(update(_q).where(_q.c.id == row.id).values(
                        delivery_count=count, visible_at=now + self.lock))
                    out.append(ReceivedMessage(IngestMessage.model_validate(row.body), count, row.id))
        return out

    async def receive(self, max_messages: int, wait_seconds: float) -> list[ReceivedMessage]:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            got = await asyncio.to_thread(self._claim, max_messages)
            if got or asyncio.get_running_loop().time() >= deadline:
                return got
            await asyncio.sleep(0.5)

    async def complete(self, msg: ReceivedMessage) -> None:
        def _do() -> None:
            with self.engine.begin() as c:
                c.execute(delete(_q).where(_q.c.id == msg.handle))

        await asyncio.to_thread(_do)

    async def abandon(self, msg: ReceivedMessage) -> None:
        def _do() -> None:
            with self.engine.begin() as c:
                c.execute(update(_q).where(_q.c.id == msg.handle).values(visible_at=_now()))

        await asyncio.to_thread(_do)

    async def renew(self, msg: ReceivedMessage) -> None:
        def _do() -> None:
            with self.engine.begin() as c:
                c.execute(update(_q).where(_q.c.id == msg.handle).values(visible_at=_now() + self.lock))

        await asyncio.to_thread(_do)

    async def dead_letter(self, msg: ReceivedMessage, reason: str, description: str) -> None:
        def _do() -> None:
            with self.engine.begin() as c:
                c.execute(update(_q).where(_q.c.id == msg.handle).values(
                    dead_lettered=True, dl_reason=f"{reason}: {description}"[:500]))

        await asyncio.to_thread(_do)

    async def depth(self) -> dict[str, int]:
        def _do() -> dict[str, int]:
            with self.engine.connect() as c:
                rows = c.execute(
                    select(_q.c.lane, _q.c.dead_lettered, func.count()).group_by(_q.c.lane, _q.c.dead_lettered)
                ).all()
            out = {"priority_active": 0, "priority_dead_letter": 0, "bulk_active": 0, "bulk_dead_letter": 0}
            for lane, dl, n in rows:
                out[f"{lane}_{'dead_letter' if dl else 'active'}"] = int(n)
            return out

        return await asyncio.to_thread(_do)

    async def peek_dead_letters(self, lane: Lane, max_messages: int) -> list[IngestMessage]:
        def _do() -> list[IngestMessage]:
            with self.engine.connect() as c:
                rows = c.execute(
                    select(_q.c.body).where(_q.c.lane == lane.value, _q.c.dead_lettered.is_(True))
                    .order_by(_q.c.id).limit(max_messages)
                ).all()
            return [IngestMessage.model_validate(r.body) for r in rows]

        return await asyncio.to_thread(_do)

    async def purge_dead_letters(self, doc_ids: Sequence[str]) -> None:
        def _do() -> None:
            with self.engine.begin() as c:
                for d in doc_ids:
                    c.execute(delete(_q).where(_q.c.dead_lettered.is_(True), _q.c.message_id.like(f"{d}:%")))

        await asyncio.to_thread(_do)
