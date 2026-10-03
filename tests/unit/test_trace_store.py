"""SqlQueryTraceStore on SQLite: save/get, filters, keyset paging, purge, and the expectation claim."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_os.application.ports import TraceQuery
from rag_os.domain.errors import ValidationFailed
from rag_os.domain.trace import Expectation, NearMiss, QueryTrace, TraceStage, Verdict
from rag_os.infrastructure.state.trace_store import SqlQueryTraceStore


@pytest.fixture()
def store(tmp_path: Path) -> SqlQueryTraceStore:
    return SqlQueryTraceStore(f"sqlite:///{(tmp_path / 't.db').as_posix()}", create=True)


def trace(i: int, *, verdict: Verdict = Verdict.NOT_IN_CORPUS, subject: str = "priya", question: str = "pto?",
          minutes_ago: int = 0, replay_of: str | None = None) -> QueryTrace:
    return QueryTrace(
        id=f"{i:032x}", correlation_id=f"cid-{i}", at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        subject=subject, display_name=subject.title(), question=question, outcome="refused",
        reason="no_relevant_context", verdict=verdict, duration_ms=12.5, tokens=40, replay_of=replay_of,
        attributes={"department": ["HR"], "clearance": 2}, roles=["reviewer"],
        stages=[TraceStage(name="search", label="Search", data={"hits": [{"doc_id": "d1"}]})],
        near_miss=NearMiss(ran=True), diagnosis=["Correct no-answer."])


def test_save_and_get_by_id_or_correlation(store: SqlQueryTraceStore) -> None:
    store.save(trace(1))
    got = store.get(f"{1:032x}")
    assert got is not None and got.question == "pto?" and got.attributes == {"department": ["HR"], "clearance": 2}
    assert got.stages[0].data["hits"][0]["doc_id"] == "d1" and got.diagnosis == ["Correct no-answer."]
    assert got.near_miss.ran and got.verdict == Verdict.NOT_IN_CORPUS
    assert store.get("cid-1") is not None
    assert store.get("missing") is None


def test_filters_and_literal_search(store: SqlQueryTraceStore) -> None:
    store.save(trace(1, verdict=Verdict.MISCONFIGURATION, question="401 (K) 100%"))
    store.save(trace(2, subject="marcus", question="pricing"))
    store.save(trace(3, minutes_ago=120, replay_of="e1"))
    assert [r.id for r in store.query(TraceQuery(problems_only=True))[0]] == [f"{1:032x}"]
    assert [r.subject for r in store.query(TraceQuery(user="MARC"))[0]] == ["marcus"]
    # '%' typed into the search box is a character, not a wildcard.
    assert len(store.query(TraceQuery(text="100%"))[0]) == 1
    assert len(store.query(TraceQuery(text="%"))[0]) == 1
    assert len(store.query(TraceQuery(since=datetime.now(UTC) - timedelta(minutes=60)))[0]) == 2
    assert store.query(TraceQuery(verdict="not_in_corpus"))[0][0].replay_of is None


def test_keyset_paging_newest_first(store: SqlQueryTraceStore) -> None:
    for i in range(7):
        store.save(trace(i, minutes_ago=i))
    seen: list[str] = []
    after = None
    while True:
        page, after = store.query(TraceQuery(limit=3, after=after))
        seen += [r.id for r in page]
        if not after:
            break
    assert seen == [f"{i:032x}" for i in range(7)]
    with pytest.raises(ValidationFailed):
        store.query(TraceQuery(after="not-a-cursor!"))


def test_window_and_purge(store: SqlQueryTraceStore) -> None:
    store.save(trace(1))
    store.save(trace(2, minutes_ago=60 * 24 * 40))
    assert len(store.window(datetime.now(UTC) - timedelta(days=1))) == 1
    assert store.purge(datetime.now(UTC) - timedelta(days=30)) == 1
    assert store.get(f"{2:032x}") is None and store.get(f"{1:032x}") is not None


def test_expectations_crud_and_claim(store: SqlQueryTraceStore) -> None:
    e = Expectation(id="e1", question="401k?", attributes={"department": ["HR"]}, roles=[], expected="answer",
                    required_doc_ids=["benefits"], created_at=datetime.now(UTC), created_by="Alex")
    store.save_expectation(e)
    assert store.list_expectations()[0].required_doc_ids == ["benefits"]
    lease = timedelta(minutes=15)
    due_before = datetime.now(UTC)
    claimed = store.claim_due(due_before, lease)
    assert [x.id for x in claimed] == ["e1"]
    # A second replica asking at the same moment gets nothing: the claim is held.
    assert store.claim_due(due_before, lease) == []
    store.record_result("e1", "fail", "did not cite benefits", "t1", datetime.now(UTC))
    got = store.get_expectation("e1")
    assert got is not None and got.last_result == "fail" and got.last_trace_id == "t1"
    # Just run, so not due again until the interval passes.
    assert store.claim_due(datetime.now(UTC) - timedelta(hours=1), lease) == []
    assert store.delete_expectation("e1") and not store.delete_expectation("e1")
