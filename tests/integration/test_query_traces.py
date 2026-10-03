"""Query traces end to end: the real pipeline, the near-miss probe, the verdicts, the admin API and expectations.

The person in these tests mirrors the report that prompted the feature: HR, AMER, clearance 2, asking about a
document they cannot see. The test corpus has no 401(k) document, so the US PTO policy (HR / US) stands in.

The in-memory index scores with RRF, and every permitted document scores something, so without a relevance bar
nothing would ever be refused. RETRIEVAL_MIN_SCORE=0.03 is that bar here: in practice it keeps a passage only
when it matches on keywords as well as on vector similarity.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rag_os.api.app import create_app
from rag_os.application.ports import LlmResult, RetrievalResult
from rag_os.application.services.query_trace import TraceRecorder
from rag_os.composition import Container
from rag_os.domain.access import Principal
from rag_os.domain.answers import Answer
from rag_os.domain.errors import DependencyUnavailable
from rag_os.domain.trace import QueryTrace, StageStatus, Verdict
from rag_os.infrastructure.search.in_memory import InMemorySearchIndex
from rag_os.infrastructure.settings import Settings

from .test_pipeline import ingest_sample

BAR = 0.03
WITHHELD_Q = "PTO carry over"
ANSWERED_Q = "How many PTO days do US employees accrue?"
NOWHERE_Q = "zzqx flux capacitor quantum"
PTO = "hr/us/policies/pto-policy.txt"


@pytest.fixture()
async def traced(settings: Settings) -> AsyncIterator[Container]:
    c = Container(settings.model_copy(update={"retrieval_min_score": BAR}))
    await c.bootstrap()
    await ingest_sample(c)
    yield c
    await c.aclose()


def person(c: Container, **claims: Any) -> Principal:
    return c.claims.map({"sub": "amer-user", **claims}, "dev")


def hr_amer(c: Container) -> Principal:
    return person(c, departments=["HR"], regions=["AMER"], clearance=2, employee_id="E9")


async def ask(c: Container, p: Principal, q: str) -> tuple[Answer, QueryTrace]:
    rec = TraceRecorder(principal=p, question=q)
    answer = await c.answer.ask(p, q, trace=rec)
    return answer, rec.finish(answer)


def retag(c: Container, path: str, **fields: Any) -> None:
    """Rewrite a document's access tags in the index, the way a mis-tagged upload would have written them."""
    idx = c.index
    assert isinstance(idx, InMemorySearchIndex)
    hit = 0
    for doc in idx.docs.values():
        if doc["path"] == path:
            doc.update(fields)
            hit += 1
    assert hit, path


# --------------------------------------------------------------------------- verdicts on the real pipeline


async def test_withheld_by_policy_names_the_region(traced: Container) -> None:
    answer, t = await ask(traced, hr_amer(traced), WITHHELD_Q)
    assert answer.refused and answer.refusal_reason == "no_relevant_context"
    assert t.verdict == Verdict.WITHHELD_BY_POLICY and not t.is_problem and t.failed_stage is None
    pto = next(d for d in t.near_miss.docs if d.path == PTO)
    assert not pto.allowed and not pto.problems
    assert pto.checks["department"].passed and pto.checks["clearance"].passed
    assert not pto.checks["region"].passed and "US" in pto.checks["region"].note
    assert any("withheld" in line and "region" in line for line in t.diagnosis)
    # The probe's findings are evidence for the administrator, never part of the answer.
    assert not answer.citations and PTO not in answer.answer


async def test_tracing_does_not_change_the_answer(traced: Container) -> None:
    p = hr_amer(traced)
    for q in (WITHHELD_Q, ANSWERED_Q, NOWHERE_Q):
        plain = await traced.answer.ask(p, q)
        traced_answer, _ = await ask(traced, p, q)
        assert (plain.answer, plain.refused, plain.refusal_reason, [x.chunk_id for x in plain.citations]) == (
            traced_answer.answer, traced_answer.refused, traced_answer.refusal_reason,
            [x.chunk_id for x in traced_answer.citations])


async def test_answered_records_every_stage(traced: Container) -> None:
    answer, t = await ask(traced, hr_amer(traced), ANSWERED_Q)
    assert not answer.refused and t.verdict == Verdict.ANSWERED
    for name in ("request", "identity", "access", "query", "embed", "search", "relevance", "prompt", "llm",
                 "grounding", "outcome"):
        assert t.stage(name).status == StageStatus.OK, name
    assert "acl_region" in t.stage("access").data["access_filter"]
    assert t.stage("grounding").data["citations"]
    assert not t.near_miss.ran  # the probe only runs for refusals


async def test_not_in_corpus(traced: Container) -> None:
    _, t = await ask(traced, hr_amer(traced), NOWHERE_Q)
    assert t.verdict == Verdict.NOT_IN_CORPUS and t.near_miss.ran and not t.near_miss.docs
    assert t.stage("relevance").status == StageStatus.WARN  # things were found, none were good enough


async def test_upload_that_inherited_the_uploaders_tags_is_misconfiguration(traced: Container) -> None:
    # What an IT administrator's upload of an HR document looks like: classified HR, tagged IT / Global / 3.
    retag(traced, PTO, acl_department=["IT"], acl_region=["Global"], acl_clearance=3)
    _, t = await ask(traced, hr_amer(traced), WITHHELD_Q)
    assert t.verdict == Verdict.MISCONFIGURATION and t.is_problem
    assert t.failed_stage == "access" and t.stage("access").status == StageStatus.FAIL
    pto = next(d for d in t.near_miss.docs if d.path == PTO)
    assert not pto.checks["department"].passed and not pto.checks["clearance"].passed
    assert any("classified Department HR" in p and "says IT" in p for p in pto.problems)


async def test_document_without_a_region_tag_is_misconfiguration(traced: Container) -> None:
    retag(traced, PTO, acl_region=[])
    _, t = await ask(traced, person(traced, departments=["HR"], regions=["US"], clearance=2), WITHHELD_Q)
    assert t.verdict == Verdict.MISCONFIGURATION
    pto = next(d for d in t.near_miss.docs if d.path == PTO)
    assert any("no Region access tag" in p for p in pto.problems)


async def test_account_without_department_is_misconfiguration(traced: Container) -> None:
    answer, t = await ask(traced, person(traced, regions=["AMER"], clearance=2), WITHHELD_Q)
    assert answer.refusal_reason == "no_access" and t.verdict == Verdict.MISCONFIGURATION
    assert t.stage("identity").status == StageStatus.FAIL and t.failed_stage == "identity"
    assert any("no Department" in p for p in t.caller_problems)


async def test_uncited_answer_is_a_generation_miss(traced: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_citations(**kw: Any) -> LlmResult:
        from rag_os.domain.answers import TokenUsage

        return LlmResult(text="Employees get some days off.", usage=TokenUsage(), provider="fake", model="fake",
                         stop_reason="end_turn")

    monkeypatch.setattr(traced.llm_answer, "complete", no_citations)
    answer, t = await ask(traced, hr_amer(traced), ANSWERED_Q)
    assert answer.refusal_reason == "uncited"
    assert t.verdict == Verdict.GENERATION_MISS and t.failed_stage == "grounding"
    assert t.stage("llm").data["stop_reason"] == "end_turn"


async def test_a_permitted_document_missing_from_results_is_a_retrieval_miss(
        traced: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates the filter and the index disagreeing: the probe finds a document the caller may read."""
    real = traced.retriever.retrieve

    async def lose_everything(**kw: Any) -> RetrievalResult:
        r = await real(**kw)
        return RetrievalResult(hits=[], usage=r.usage, timings_ms=r.timings_ms, thresholds=r.thresholds,
                               vector=r.vector)

    monkeypatch.setattr(traced.retriever, "retrieve", lose_everything)
    us = person(traced, departments=["HR"], regions=["US"], clearance=1)
    _, t = await ask(traced, us, WITHHELD_Q)
    assert t.verdict == Verdict.RETRIEVAL_MISS and t.failed_stage == "search"
    assert any(d.allowed and d.path == PTO for d in t.near_miss.docs)


async def test_probe_can_be_switched_off(settings: Settings) -> None:
    c = Container(settings.model_copy(update={"retrieval_min_score": BAR, "query_trace_near_miss": False}))
    await c.bootstrap()
    try:
        await ingest_sample(c)
        _, t = await ask(c, hr_amer(c), WITHHELD_Q)
        assert t.verdict == Verdict.UNVERIFIED and "disabled" in t.near_miss.skipped_reason
    finally:
        await c.aclose()


# --------------------------------------------------------------------------- admin API


@pytest.fixture()
def client(traced: Container) -> TestClient:  # type: ignore[misc]
    with TestClient(create_app(traced.settings, traced)) as tc:
        yield tc


def token(client: TestClient, pid: str) -> dict[str, str]:
    r = client.post("/api/dev/token", json={"principal_id": pid})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_every_question_is_traced_and_readable_by_admins_only(client: TestClient) -> None:
    hr, admin = token(client, "support-de"), token(client, "admin")
    r = client.post("/api/chat", headers={**hr, "X-Correlation-ID": "trace-me-1"}, json={"question": WITHHELD_Q})
    assert r.status_code == 200 and r.json()["refused"]

    assert client.get("/api/admin/traces", headers=hr).status_code == 403
    assert client.get("/api/admin/traces/trace-me-1", headers=hr).status_code == 403

    items = client.get("/api/admin/traces", headers=admin).json()["items"]
    assert items and items[0]["correlation_id"] == "trace-me-1" and items[0]["subject"] == "support-de"
    detail = client.get("/api/admin/traces/trace-me-1", headers=admin).json()
    assert detail["verdict"] == "withheld_by_policy" and detail["verdict_label"]
    assert detail["stages"][0]["name"] == "request" and detail["near_miss"]["docs"]
    assert client.get("/api/admin/traces/nope", headers=admin).status_code == 404

    meta = client.get("/api/admin/traces/meta", headers=admin).json()
    assert meta["stages"][0]["name"] == "request" and any(v["problem"] for v in meta["verdicts"])
    summary = client.get("/api/admin/traces/summary?minutes=60", headers=admin).json()
    assert summary["total"] >= 1 and {m["key"] for m in summary["metrics"]} >= {"problem_rate", "p95_ms"}
    problems = client.get("/api/admin/traces?problems=true", headers=admin).json()["items"]
    assert all(i["verdict"] in ("misconfiguration", "retrieval_miss", "generation_miss", "error") for i in problems)


def test_a_failure_is_traced_with_the_stage_that_failed(client: TestClient, traced: Container,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(_: Any) -> Any:
        raise DependencyUnavailable("search query failed", detail={"status": 503})

    monkeypatch.setattr(traced.index, "search", down)
    hr = token(client, "hr-emea")
    r = client.post("/api/chat", headers={**hr, "X-Correlation-ID": "trace-me-2"}, json={"question": WITHHELD_Q})
    assert r.status_code == 503
    detail = client.get("/api/admin/traces/trace-me-2", headers=token(client, "admin")).json()
    assert detail["outcome"] == "error" and detail["verdict"] == "error" and detail["failed_stage"] == "search"
    assert any("Search failed" in line for line in detail["diagnosis"])


def test_malformed_questions_are_not_traced(client: TestClient, traced: Container) -> None:
    hr = token(client, "hr-emea")
    r = client.post("/api/chat", headers={**hr, "X-Correlation-ID": "bad-1"},
                    json={"question": "x", "filters": {"nope": ["a"]}})
    assert r.status_code == 422
    assert traced.traces.get("bad-1") is None


def test_expectations_replay_and_judge(client: TestClient) -> None:
    hr, admin = token(client, "support-de"), token(client, "admin")
    client.post("/api/chat", headers={**hr, "X-Correlation-ID": "exp-1"}, json={"question": WITHHELD_Q})
    client.post("/api/chat", headers={**token(client, "hr-emea"), "X-Correlation-ID": "exp-2"},
                json={"question": "How much paid parental leave do UK employees get?"})

    # The refusal was correct for this person, and an expectation says so: replay passes.
    e1 = client.post("/api/admin/traces/exp-1/expectation", headers=admin, json={"expected": "no_answer"}).json()
    r = client.post(f"/api/admin/expectations/{e1['id']}/replay", headers=admin).json()
    assert r["expectation"]["last_result"] == "pass", r

    # Someone states it SHOULD be answered from the US PTO policy: the replay fails, and says why.
    e2 = client.post("/api/admin/traces/exp-1/expectation", headers=admin,
                     json={"expected": "answer", "required_doc_ids": ["whatever"]}).json()
    r = client.post(f"/api/admin/expectations/{e2['id']}/replay", headers=admin).json()
    assert r["expectation"]["last_result"] == "fail" and "expected an answer" in r["expectation"]["last_detail"]
    replay_trace = client.get(f"/api/admin/traces/{r['trace_id']}", headers=admin).json()
    assert replay_trace["replay_of"] == e2["id"] and replay_trace["attributes"]["region"] == ["DE"]

    # An answered question with the wrong required document fails on the citation, not the outcome.
    e3 = client.post("/api/admin/traces/exp-2/expectation", headers=admin,
                     json={"expected": "answer", "required_doc_ids": ["not-cited"]}).json()
    r = client.post(f"/api/admin/expectations/{e3['id']}/replay", headers=admin).json()
    assert r["expectation"]["last_result"] == "fail" and "did not cite not-cited" in r["expectation"]["last_detail"]

    listed = client.get("/api/admin/expectations", headers=admin).json()["items"]
    assert {e["id"] for e in listed} == {e1["id"], e2["id"], e3["id"]}
    summary = client.get("/api/admin/traces/summary", headers=admin).json()
    assert len(summary["failing_expectations"]) == 2 and summary["replays"] >= 3
    assert client.post("/api/admin/traces/exp-1/expectation", headers=admin,
                       json={"expected": "no_answer", "required_doc_ids": ["x"]}).status_code == 422
    assert client.delete(f"/api/admin/expectations/{e1['id']}", headers=admin).status_code == 200
    assert client.delete(f"/api/admin/expectations/{e1['id']}", headers=admin).status_code == 404
    assert client.post("/api/admin/expectations/nope/replay", headers=hr).status_code == 403


async def test_maintenance_purges_and_replays_due(traced: Container) -> None:
    from datetime import UTC, datetime, timedelta

    from rag_os.application.use_cases.expectations import maintenance_tick

    p = hr_amer(traced)
    _, t = await ask(traced, p, WITHHELD_Q)
    old = t.model_copy(update={"id": "0" * 32, "at": datetime.now(UTC) - timedelta(days=45)})
    traced.traces.save(old)
    traced.traces.save(t)
    traced.expectations.from_trace(t, expected="no_answer", required_doc_ids=[], note="", created_by="test")
    out = await maintenance_tick(traced.traces, traced.expectations, retention_days=30, replay_hours=24)
    assert out == {"purged": 1, "replayed": 1, "passed": 1, "failed": 0}
    # Run again immediately: nothing is due, nothing left to purge.
    assert await maintenance_tick(traced.traces, traced.expectations, retention_days=30, replay_hours=24) == {
        "purged": 0, "replayed": 0, "passed": 0, "failed": 0}


def test_trace_store_path(tmp_path: Path) -> None:
    """The container builds the trace store from the state database settings."""
    s = Settings(app_env="test", state_db_url=f"sqlite:///{(tmp_path / 'x.db').as_posix()}",
                 config_dir=str(Path(__file__).resolve().parents[2] / "config"), queue="in_memory",
                 search_backend="in_memory", embedding_profile="test-fake-256", otel_enabled=False,
                 _env_file=None)  # type: ignore[call-arg]
    assert Container(s).traces.engine.url.database.endswith("x.db")
