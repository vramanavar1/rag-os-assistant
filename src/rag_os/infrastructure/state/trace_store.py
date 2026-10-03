"""SQL store for query traces and expectations (PostgreSQL in Azure, SQLite for tests and local runs).

Its own MetaData, like the queue and the local search tables, so migrations/env.py picks it up by listing it.
A trace row carries the columns the list and the health summary filter on; the stage data, which is most of a
trace, stays in one JSON column that is only read when a single trace is opened.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import RowMapping

from rag_os.application.ports import QueryTraceStore, TraceQuery
from rag_os.domain.errors import ValidationFailed
from rag_os.domain.trace import PROBLEM_VERDICTS, Expectation, QueryTrace, TraceSummaryRow, Verdict
from rag_os.infrastructure.state.db import make_engine

_meta = MetaData()

query_traces = Table(
    "query_traces",
    _meta,
    Column("id", String(32), primary_key=True),
    Column("correlation_id", String(64), index=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("subject", String(256), index=True),
    Column("display_name", String(256)),
    Column("outcome", String(16), nullable=False),
    Column("reason", String(48)),
    Column("verdict", String(32), nullable=False, index=True),
    Column("failed_stage", String(24)),
    Column("question", Text),
    Column("duration_ms", Float, nullable=False, default=0.0),
    Column("tokens", Integer, nullable=False, default=0),
    Column("model", String(128)),
    Column("replay_of", String(32), index=True),
    Column("data", JSON, nullable=False),
    # Newest-first paging, with the id tie-breaker so the keyset condition is covered end to end.
    Index("ix_query_traces_at", "at", "id"),
)

query_expectations = Table(
    "query_expectations",
    _meta,
    Column("id", String(32), primary_key=True),
    Column("question", Text, nullable=False),
    Column("attributes", JSON, nullable=False),
    Column("roles", JSON, nullable=False),
    Column("filters", JSON, nullable=False),
    Column("expected", String(16), nullable=False),
    Column("required_doc_ids", JSON, nullable=False),
    Column("note", Text),
    Column("created_by", String(256)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("from_trace_id", String(32)),
    Column("last_result", String(8)),
    Column("last_detail", Text),
    Column("last_run_at", DateTime(timezone=True), index=True),
    Column("last_trace_id", String(32)),
    # Set while a replica is replaying it. A lease rather than a lock, so a replica that dies mid-run does not
    # leave the expectation stuck forever.
    Column("claimed_at", DateTime(timezone=True)),
)

_SUMMARY_COLUMNS = (query_traces.c.id, query_traces.c.correlation_id, query_traces.c.at, query_traces.c.subject,
                    query_traces.c.display_name, query_traces.c.question, query_traces.c.outcome,
                    query_traces.c.reason, query_traces.c.verdict, query_traces.c.failed_stage,
                    query_traces.c.duration_ms, query_traces.c.tokens, query_traces.c.replay_of)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _row(r: RowMapping) -> TraceSummaryRow:
    try:
        verdict = Verdict(r["verdict"])
    except ValueError:  # a verdict written by a newer build, read by an older one during a rollout
        verdict = Verdict.UNVERIFIED
    return TraceSummaryRow(
        id=r["id"], correlation_id=r["correlation_id"], at=_aware(r["at"]) or datetime.now(UTC),
        subject=r["subject"] or "", display_name=r["display_name"] or "", question=(r["question"] or "")[:300],
        outcome=r["outcome"], reason=r["reason"], verdict=verdict, failed_stage=r["failed_stage"],
        duration_ms=float(r["duration_ms"] or 0.0), tokens=int(r["tokens"] or 0), replay_of=r["replay_of"])


def _expectation(r: RowMapping) -> Expectation:
    return Expectation(
        id=r["id"], question=r["question"], attributes=r["attributes"] or {}, roles=r["roles"] or [],
        filters=r["filters"] or {}, expected=r["expected"], required_doc_ids=r["required_doc_ids"] or [],
        note=r["note"] or "", created_by=r["created_by"] or "", created_at=_aware(r["created_at"]) or datetime.now(UTC),
        from_trace_id=r["from_trace_id"], last_result=r["last_result"], last_detail=r["last_detail"] or "",
        last_run_at=_aware(r["last_run_at"]), last_trace_id=r["last_trace_id"])


def _contains(text: str) -> str:
    """A LIKE pattern matching `text` literally: '%' and '_' typed into a search box are not wildcards."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _encode_cursor(at: datetime, trace_id: str) -> str:
    """base64url of "<iso>|<id>", for the same reason as the document cursor: an ISO '+' breaks in a URL."""
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{trace_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts_raw, sep, trace_id = raw.partition("|")
        if not sep or not trace_id:
            raise ValueError("no separator")
        return datetime.fromisoformat(ts_raw), trace_id
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise ValidationFailed("malformed page cursor") from e


class SqlQueryTraceStore(QueryTraceStore):
    def __init__(self, db_url: str, *, entra_auth: bool = False, create: bool = True) -> None:
        self.engine = make_engine(db_url, entra_auth=entra_auth)
        if create:
            _meta.create_all(self.engine)

    # ------------------------------------------------------------------ traces

    def save(self, trace: QueryTrace) -> None:
        data = json.loads(trace.model_dump_json(include={"stages", "near_miss", "diagnosis", "caller_problems",
                                                         "attributes", "roles", "filters", "answer", "issuer",
                                                         "history_turns", "provider"}))
        with self.engine.begin() as c:
            c.execute(insert(query_traces).values(
                id=trace.id, correlation_id=trace.correlation_id, at=trace.at, subject=trace.subject[:256],
                display_name=trace.display_name[:256], outcome=trace.outcome, reason=(trace.reason or None),
                verdict=trace.verdict.value, failed_stage=trace.failed_stage, question=trace.question,
                duration_ms=trace.duration_ms, tokens=trace.tokens, model=trace.model[:128] or None,
                replay_of=trace.replay_of, data=data))

    def get(self, key: str) -> QueryTrace | None:
        with self.engine.connect() as c:
            r = c.execute(select(query_traces).where(query_traces.c.id == key)).mappings().first()
            if r is None:
                r = c.execute(select(query_traces).where(query_traces.c.correlation_id == key)
                              .order_by(query_traces.c.at.desc()).limit(1)).mappings().first()
        if r is None:
            return None
        row = _row(r)
        return QueryTrace(**{**(r["data"] or {}), **row.model_dump(exclude={"question"}),
                             "question": r["question"] or "", "model": r["model"] or ""})

    def query(self, q: TraceQuery) -> tuple[list[TraceSummaryRow], str | None]:
        t = query_traces.c
        conds: list[Any] = []
        if q.outcome:
            conds.append(t.outcome == q.outcome)
        if q.verdict:
            conds.append(t.verdict == q.verdict)
        if q.reason:
            conds.append(t.reason == q.reason)
        if q.problems_only:
            conds.append(t.verdict.in_([v.value for v in PROBLEM_VERDICTS]))
        if q.user:
            like = _contains(q.user)
            conds.append(or_(t.subject.ilike(like, escape="\\"), t.display_name.ilike(like, escape="\\")))
        if q.text:
            conds.append(t.question.ilike(_contains(q.text), escape="\\"))
        if q.since:
            conds.append(t.at >= q.since)
        if q.after:
            ts, trace_id = _decode_cursor(q.after)
            # Spelled out rather than a row-value comparison, which older SQLite does not support.
            conds.append(or_(t.at < ts, and_(t.at == ts, t.id < trace_id)))
        limit = max(1, min(q.limit, 500))
        stmt = select(*_SUMMARY_COLUMNS)
        if conds:
            stmt = stmt.where(and_(*conds))
        stmt = stmt.order_by(t.at.desc(), t.id.desc()).limit(limit + 1)
        with self.engine.connect() as c:
            rows = [_row(r) for r in c.execute(stmt).mappings()]
        page = rows[:limit]
        if len(rows) <= limit or not page:
            return page, None
        return page, _encode_cursor(page[-1].at, page[-1].id)

    def window(self, since: datetime, limit: int = 50_000) -> list[TraceSummaryRow]:
        stmt = (select(*_SUMMARY_COLUMNS).where(query_traces.c.at >= since)
                .order_by(query_traces.c.at.desc()).limit(limit))
        with self.engine.connect() as c:
            return [_row(r) for r in c.execute(stmt).mappings()]

    def purge(self, older_than: datetime) -> int:
        with self.engine.begin() as c:
            return int(c.execute(delete(query_traces).where(query_traces.c.at < older_than)).rowcount or 0)

    # ------------------------------------------------------------------ expectations

    def save_expectation(self, e: Expectation) -> None:
        values = {
            "question": e.question, "attributes": e.attributes, "roles": e.roles, "filters": e.filters,
            "expected": e.expected, "required_doc_ids": e.required_doc_ids, "note": e.note,
            "created_by": e.created_by[:256], "created_at": e.created_at, "from_trace_id": e.from_trace_id,
            "last_result": e.last_result, "last_detail": e.last_detail, "last_run_at": e.last_run_at,
            "last_trace_id": e.last_trace_id,
        }
        with self.engine.begin() as c:
            exists = c.execute(select(query_expectations.c.id).where(query_expectations.c.id == e.id)).first()
            if exists:
                c.execute(update(query_expectations).where(query_expectations.c.id == e.id).values(**values))
            else:
                c.execute(insert(query_expectations).values(id=e.id, **values))

    def get_expectation(self, expectation_id: str) -> Expectation | None:
        with self.engine.connect() as c:
            r = c.execute(select(query_expectations).where(query_expectations.c.id == expectation_id)).mappings().first()
        return _expectation(r) if r else None

    def list_expectations(self) -> list[Expectation]:
        with self.engine.connect() as c:
            rows = c.execute(select(query_expectations).order_by(query_expectations.c.created_at.desc())).mappings()
            return [_expectation(r) for r in rows]

    def delete_expectation(self, expectation_id: str) -> bool:
        with self.engine.begin() as c:
            res = c.execute(delete(query_expectations).where(query_expectations.c.id == expectation_id))
            return bool(res.rowcount)

    def claim_due(self, due_before: datetime, lease: timedelta, limit: int = 20) -> list[Expectation]:
        x = query_expectations.c
        now = datetime.now(UTC)
        due = and_(or_(x.last_run_at.is_(None), x.last_run_at < due_before),
                   or_(x.claimed_at.is_(None), x.claimed_at < now - lease))
        claimed: list[Expectation] = []
        with self.engine.connect() as c:
            candidates = [r[0] for r in c.execute(select(x.id).where(due).order_by(x.last_run_at).limit(limit))]
        for eid in candidates:
            # The UPDATE re-checks the condition, so of two replicas racing for one row exactly one sees rowcount 1.
            with self.engine.begin() as c:
                res = c.execute(update(query_expectations).where(and_(x.id == eid, due)).values(claimed_at=now))
                if res.rowcount != 1:
                    continue
                r = c.execute(select(query_expectations).where(x.id == eid)).mappings().first()
            if r is not None:
                claimed.append(_expectation(r))
        return claimed

    def record_result(self, expectation_id: str, result: str, detail: str, trace_id: str | None,
                      at: datetime) -> None:
        with self.engine.begin() as c:
            c.execute(update(query_expectations).where(query_expectations.c.id == expectation_id).values(
                last_result=result, last_detail=detail[:2000], last_trace_id=trace_id, last_run_at=at,
                claimed_at=None))
