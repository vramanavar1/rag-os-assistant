"""Troubleshooting: query traces, live query health, and expectations (Admin > Query traces).

Administrators only. A trace holds another person's question and answer, the attributes they asked with, and -
for a refused question - the titles of documents they were NOT allowed to read. That last part is the point of
the page and also why it must never be reachable by anyone else.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from rag_os.api.deps import get_container, require_role
from rag_os.application.ports import TraceQuery
from rag_os.application.services.query_health import summarise
from rag_os.composition import Container
from rag_os.domain.access import Principal
from rag_os.domain.errors import NotFound
from rag_os.domain.trace import PROBLEM_VERDICTS, STAGES, VERDICT_LABELS, Expectation, QueryTrace, Verdict
from rag_os.infrastructure.telemetry import record_expectation

router = APIRouter(prefix="/api/admin", tags=["admin: troubleshooting"])
admin = require_role("admin")


class ExpectationRequest(BaseModel):
    expected: Literal["answer", "no_answer"]
    required_doc_ids: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=2000)


def _get(c: Container, key: str) -> QueryTrace:
    trace = c.traces.get(key.strip())
    if trace is None:
        raise NotFound("no trace with that id or correlation id (traces are kept "
                       f"{c.settings.query_trace_retention_days} days)")
    return trace


@router.get("/traces/meta", summary="Stage names, verdicts and thresholds, for rendering traces")
async def meta(_: Principal = Depends(admin), c: Container = Depends(get_container)) -> dict[str, Any]:
    s = c.settings
    return {
        "enabled": s.query_trace_enabled,
        "near_miss": s.query_trace_near_miss,
        "retention_days": s.query_trace_retention_days,
        "replay_hours": s.query_expectation_replay_hours,
        "stages": [{"name": n, "label": label} for n, label in STAGES],
        "verdicts": [{"verdict": v.value, "label": VERDICT_LABELS[v], "problem": v in PROBLEM_VERDICTS}
                     for v in Verdict],
    }


@router.get("/traces", summary="Query traces, newest first")
async def list_traces(
    _: Principal = Depends(admin), c: Container = Depends(get_container),
    outcome: str | None = None, verdict: str | None = None, reason: str | None = None,
    user: str | None = Query(default=None, max_length=256), q: str | None = Query(default=None, max_length=256),
    minutes: int | None = Query(default=None, ge=1, le=60 * 24 * 90), problems: bool = False,
    after: str | None = None, limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(minutes=minutes) if minutes else None
    items, nxt = await asyncio.to_thread(c.traces.query, TraceQuery(
        outcome=outcome or None, verdict=verdict or None, reason=reason or None, user=user or None, text=q or None,
        since=since, problems_only=problems, after=after, limit=limit))
    return {"items": [i.model_dump(mode="json") for i in items], "next": nxt}


@router.get("/traces/summary", summary="Live query health over a recent window")
async def summary(_: Principal = Depends(admin), c: Container = Depends(get_container),
                  minutes: int = Query(default=60, ge=5, le=60 * 24 * 7)) -> dict[str, Any]:
    s = c.settings
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    rows = await asyncio.to_thread(c.traces.window, since)
    expectations = await asyncio.to_thread(c.traces.list_expectations)
    return summarise(rows, since=since, minutes=minutes, expectations=expectations,
                     problem_rate=s.query_health_problem_rate, error_rate=s.query_health_error_rate,
                     p95_ms=s.query_health_p95_ms)


@router.get("/traces/{key}", summary="One trace, by trace id or correlation id")
async def get_trace(key: str, _: Principal = Depends(admin), c: Container = Depends(get_container)) -> dict[str, Any]:
    trace = await asyncio.to_thread(_get, c, key)
    out = trace.model_dump(mode="json")
    out["verdict_label"] = VERDICT_LABELS[trace.verdict]
    out["is_problem"] = trace.is_problem
    return out


@router.post("/traces/{key}/expectation", summary="Record the right outcome for this trace's question")
async def create_expectation(key: str, req: ExpectationRequest, principal: Principal = Depends(admin),
                             c: Container = Depends(get_container)) -> dict[str, Any]:
    trace = await asyncio.to_thread(_get, c, key)
    e = await asyncio.to_thread(c.expectations.from_trace, trace, expected=req.expected,
                                required_doc_ids=req.required_doc_ids, note=req.note,
                                created_by=principal.display_name or principal.subject)
    return e.model_dump(mode="json")


@router.get("/expectations", summary="Every expectation and its last replay result")
async def list_expectations(_: Principal = Depends(admin), c: Container = Depends(get_container)) -> dict[str, Any]:
    items: list[Expectation] = await asyncio.to_thread(c.traces.list_expectations)
    return {"items": [e.model_dump(mode="json") for e in items]}


@router.post("/expectations/{expectation_id}/replay", summary="Replay one expectation now")
async def replay(expectation_id: str, principal: Principal = Depends(admin),
                 c: Container = Depends(get_container)) -> dict[str, Any]:
    e = await asyncio.to_thread(c.traces.get_expectation, expectation_id)
    if e is None:
        raise NotFound("no such expectation")
    e, trace = await c.expectations.replay(e, by=principal.display_name or principal.subject)
    record_expectation(e.last_result or "fail")
    return {"expectation": e.model_dump(mode="json"), "trace_id": trace.id, "verdict": trace.verdict.value,
            "verdict_label": VERDICT_LABELS[trace.verdict]}


@router.delete("/expectations/{expectation_id}", summary="Delete an expectation")
async def delete_expectation(expectation_id: str, _: Principal = Depends(admin),
                             c: Container = Depends(get_container)) -> dict[str, Any]:
    if not await asyncio.to_thread(c.traces.delete_expectation, expectation_id):
        raise NotFound("no such expectation")
    return {"deleted": expectation_id}
