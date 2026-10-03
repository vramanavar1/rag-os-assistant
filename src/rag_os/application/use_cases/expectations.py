"""Expectations: a person states what the right outcome for a question is, and replays prove it.

A trace can show WHY a question went unanswered, but not whether it should have been answered: that needs
someone who knows the documents. An expectation records that judgement - "this should be answered, citing
Benefits.pdf", or "this should not be answered" - together with the attributes of the person who asked. A
replay runs the question through the real pipeline as a synthetic principal holding exactly those attributes,
and passes only if the outcome matches and every required document is cited.

Replays run on demand from the troubleshooting page and, if QUERY_EXPECTATION_REPLAY_HOURS is set, on a
schedule from the API process (the scheduler job holds no LLM credentials).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from rag_os.application.ports import QueryTraceStore
from rag_os.application.services.query_trace import TraceRecorder
from rag_os.application.use_cases.answer_query import AnswerQuery
from rag_os.domain.access import Principal
from rag_os.domain.answers import Answer
from rag_os.domain.errors import ValidationFailed
from rag_os.domain.trace import Expectation, QueryTrace, judge

log = logging.getLogger(__name__)
audit = logging.getLogger("rag_os.audit")

REPLAY_LEASE = timedelta(minutes=15)


class Expectations:
    def __init__(self, store: QueryTraceStore, answer: AnswerQuery, *, max_hits: int = 15) -> None:
        self.store = store
        self.answer = answer
        self.max_hits = max_hits

    def from_trace(self, trace: QueryTrace, *, expected: str, required_doc_ids: list[str], note: str,
                   created_by: str) -> Expectation:
        if expected not in ("answer", "no_answer"):
            raise ValidationFailed("expected must be 'answer' or 'no_answer'")
        if expected == "no_answer" and required_doc_ids:
            raise ValidationFailed("required documents only make sense for an expected answer")
        e = Expectation(
            id=uuid.uuid4().hex, question=trace.question, attributes=trace.attributes, roles=trace.roles,
            filters=trace.filters, expected=expected, required_doc_ids=list(dict.fromkeys(required_doc_ids)),
            note=note[:2000], created_by=created_by, created_at=datetime.now(UTC), from_trace_id=trace.id)
        self.store.save_expectation(e)
        audit.info("expectation created", extra={"expectation_id": e.id, "by": created_by, "expected": expected,
                                                  "from_trace": trace.id})
        return e

    async def replay(self, e: Expectation, *, by: str = "schedule") -> tuple[Expectation, QueryTrace]:
        """Run the question as the recorded attributes, judge it, and record both the trace and the result."""
        principal = Principal(subject=f"replay:{e.id}", issuer_kind="replay",
                              display_name=f"Replay ({e.created_by or 'expectation'})",
                              attributes=dict(e.attributes), roles=set(e.roles))
        rec = TraceRecorder(principal=principal, question=e.question, filters=e.filters,
                            correlation_id=f"replay-{uuid.uuid4().hex[:16]}", replay_of=e.id, max_hits=self.max_hits)
        answer: Answer | None = None
        try:
            answer = await self.answer.ask(principal, e.question, (), e.filters or None,
                                           correlation_id=rec.trace.correlation_id, trace=rec)
        except Exception as ex:  # a replay that errors is a failed expectation, not a crashed request
            rec.fail(ex)
        trace = rec.finish(answer)
        ok, detail = judge(e, trace, [c.doc_id for c in (answer.citations if answer else [])])
        now = datetime.now(UTC)
        await asyncio.to_thread(self.store.save, trace)
        await asyncio.to_thread(self.store.record_result, e.id, "pass" if ok else "fail", detail, trace.id, now)
        audit.info("expectation replayed", extra={"expectation_id": e.id, "by": by, "result": "pass" if ok else "fail",
                                                   "trace_id": trace.id})
        e = e.model_copy(update={"last_result": "pass" if ok else "fail", "last_detail": detail,
                                 "last_run_at": now, "last_trace_id": trace.id})
        return e, trace

    async def run_due(self, every: timedelta) -> dict[str, int]:
        due = await asyncio.to_thread(self.store.claim_due, datetime.now(UTC) - every, REPLAY_LEASE)
        passed = failed = 0
        for e in due:
            e, _ = await self.replay(e)
            if e.last_result == "pass":
                passed += 1
            else:
                failed += 1
        return {"replayed": len(due), "passed": passed, "failed": failed}


async def maintenance_tick(store: QueryTraceStore, expectations: Expectations | None, *, retention_days: int,
                           replay_hours: int) -> dict[str, int]:
    """Purge traces past retention, then replay whatever expectations are due. Called from the API process."""
    out: dict[str, int] = {}
    if retention_days > 0:
        out["purged"] = await asyncio.to_thread(store.purge, datetime.now(UTC) - timedelta(days=retention_days))
    if expectations is not None and replay_hours > 0:
        out.update(await expectations.run_due(timedelta(hours=replay_hours)))
    return out
