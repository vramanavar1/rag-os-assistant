"""Query traces: per-attribute access verdicts, misconfiguration checks, the verdict matrix, expectations and health.

The verdict is the part that must never guess, so every row of the matrix is pinned here with the exact
evidence that is supposed to produce it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rag_os.application.services.access_policy import AccessPolicyEngine
from rag_os.application.services.query_health import summarise
from rag_os.application.services.query_trace import TraceRecorder, build_diagnosis, decide_verdict
from rag_os.application.services.relevance import apply_relevance_bar
from rag_os.domain.access import AccessPolicy, AttributeRule, CombineRule, MatchKind, Principal
from rag_os.domain.answers import Answer, SearchHit
from rag_os.domain.classification import FacetDef, FacetSchema, FacetValue
from rag_os.domain.trace import (
    AttributeCheck,
    Expectation,
    NearMiss,
    NearMissDoc,
    QueryTrace,
    StageStatus,
    TraceSummaryRow,
    Verdict,
    judge,
)

REGION = FacetDef(name="region", field="f_region", hierarchical=True, values=[
    FacetValue(id="Global"), FacetValue(id="EMEA", parent="Global"), FacetValue(id="UK", parent="EMEA"),
    FacetValue(id="AMER", parent="Global"), FacetValue(id="US", parent="AMER"),
])
DEPARTMENT = FacetDef(name="department", field="f_department", values=[
    FacetValue(id="HR", synonyms=["human resources"]), FacetValue(id="IT"), FacetValue(id="Sales"),
])
FACETS = FacetSchema(facets=[REGION, DEPARTMENT])
POLICY = AccessPolicy(
    attributes=[
        AttributeRule(name="department", field="acl_department", required=True),
        AttributeRule(name="region", field="acl_region", match=MatchKind.HIERARCHICAL, hierarchy_facet="region",
                      required=True),
        AttributeRule(name="clearance", field="acl_clearance", match=MatchKind.MAX_LEVEL),
        AttributeRule(name="employee_id", field="acl_employee_id", match=MatchKind.EXACT),
    ],
    combine=CombineRule(all_of=["department", "region", "clearance"], grant_any_of=["employee_id"]),
    roles={"admin": ["rag.admin"]},
)
ENGINE = AccessPolicyEngine(POLICY, FACETS)


def person(**attrs: list[str] | int) -> Principal:
    return Principal(subject="u1", issuer_kind="entra", attributes=dict(attrs))


# The person from the bug report: HR / AMER / clearance 2.
HR_AMER = person(department=["HR"], region=["AMER"], clearance=2, employee_id=["E9"])


# --------------------------------------------------------------------------- per-attribute verdicts


def test_explain_document_names_the_attribute_that_blocks() -> None:
    checks = ENGINE.explain_document(HR_AMER, {"department": ["HR"], "region": ["US"], "clearance": 1})
    assert checks["department"].passed and checks["clearance"].passed
    # AMER reaches AMER and Global, not the US below it - hierarchy runs upward only.
    assert not checks["region"].passed
    assert "US" in checks["region"].note and "AMER" in checks["region"].note
    assert not checks["employee_id"].passed


def test_explain_document_agrees_with_allows() -> None:
    cases = [
        {"department": ["HR"], "region": ["AMER"], "clearance": 2},
        {"department": ["HR"], "region": ["Global"], "clearance": 3},
        {"department": ["IT"], "region": ["Global"], "clearance": 1},
        {"department": ["*"], "region": ["*"], "clearance": 0},
        {"department": ["Sales"], "region": ["US"], "clearance": 0, "employee_id": ["E9"]},
        {"department": ["HR"], "clearance": 1},
    ]
    for acl in cases:
        checks = ENGINE.explain_document(HR_AMER, acl)  # type: ignore[arg-type]
        all_of_ok = all(checks[n].passed for n in POLICY.combine.all_of)
        grant_ok = any(checks[n].passed for n in POLICY.combine.grant_any_of)
        assert (all_of_ok or grant_ok) == ENGINE.allows(HR_AMER, acl), acl  # type: ignore[arg-type]


def test_clearance_note_states_both_levels() -> None:
    checks = ENGINE.explain_document(HR_AMER, {"department": ["HR"], "region": ["AMER"], "clearance": 3})
    assert not checks["clearance"].passed and "level 3" in checks["clearance"].note


def test_missing_document_tag_is_named() -> None:
    checks = ENGINE.explain_document(HR_AMER, {"department": ["HR"], "region": None, "clearance": 1})
    assert "no Region tag" in checks["region"].note


# --------------------------------------------------------------------------- misconfiguration checks


def test_caller_problems_missing_required_attribute() -> None:
    probs = ENGINE.caller_problems(person(region=["AMER"], clearance=2))
    assert any("no Department" in p for p in probs)


def test_caller_problems_spelling_differs_from_vocabulary() -> None:
    probs = ENGINE.caller_problems(person(department=["hr"], region=["AMER"], clearance=2))
    assert any("'hr'" in p and "'HR'" in p for p in probs)


def test_caller_problems_unknown_value() -> None:
    assert any("Finance" in p for p in ENGINE.caller_problems(person(department=["Finance"], region=["AMER"])))


def test_caller_problems_hierarchical_synonyms_are_fine() -> None:
    # Region values are normalised before matching, so a lower-case region is not a problem.
    assert ENGINE.caller_problems(person(department=["HR"], region=["amer"], clearance=1)) == []


def test_caller_problems_clean_and_admin() -> None:
    assert ENGINE.caller_problems(HR_AMER) == []
    admin = Principal(subject="a", issuer_kind="entra", roles={"admin"})
    assert ENGINE.caller_problems(admin) == []


def test_document_problems_upload_inherited_uploader_tags() -> None:
    # Classified HR by its folder, but the access tag came from an IT uploader.
    probs = ENGINE.document_problems({"department": ["IT"], "region": ["Global"], "clearance": 3},
                                     {"department": ["HR"]})
    assert any("classified Department HR" in p and "says IT" in p for p in probs)


def test_document_problems_missing_tag_and_wildcard_is_fine() -> None:
    probs = ENGINE.document_problems({"department": ["HR"], "region": None, "clearance": 1}, {"department": ["HR"]})
    assert any("no Region access tag" in p for p in probs)
    assert ENGINE.document_problems({"department": ["*"], "region": ["Global"], "clearance": 0},
                                    {"department": ["IT"]}) == []


# --------------------------------------------------------------------------- relevance bar


def _hit(cid: str, score: float, rr: float | None = None) -> SearchHit:
    return SearchHit(chunk_id=cid, doc_id=cid, title=cid, heading="", content="x", path=cid, page=None,
                     source_id="s", score=score, reranker_score=rr)


def test_relevance_bar_keeps_order_and_reports_drops() -> None:
    kept, dropped = apply_relevance_bar([_hit("a", 0.03, 2.0), _hit("b", 0.02, 0.9), _hit("c", 0.01, None)],
                                        1.2, 0.0)
    assert [h.chunk_id for h in kept] == ["a", "c"]  # no reranker score: not judged by it
    assert [h.chunk_id for h in dropped] == ["b"]
    assert apply_relevance_bar([_hit("a", 0.01)], 0.0, 0.0) == ([_hit("a", 0.01)], [])


# --------------------------------------------------------------------------- verdict matrix


def _trace(outcome: str = "refused", reason: str | None = "no_relevant_context", *, docs: list[NearMissDoc] | None = None,
           ran: bool = True, caller_problems: list[str] | None = None) -> QueryTrace:
    rec = TraceRecorder(principal=HR_AMER, question="Please tell me more about 401 (K)")
    t = rec.trace
    t.outcome, t.reason = outcome, reason
    t.near_miss = NearMiss(ran=ran, relevance_bar={"min_reranker_score": 1.2, "min_score": 0.0}, docs=docs or [])
    t.caller_problems = caller_problems or []
    return t


def _doc(*, allowed: bool, problems: list[str] | None = None) -> NearMissDoc:
    return NearMissDoc(doc_id="benefits", chunk_id="c1", title="Benefits", path="HR/Benefits.pdf", page=3, score=0.03,
                       reranker_score=2.4, allowed=allowed, problems=problems or [],
                       checks={"department": AttributeCheck(passed=allowed, note="document is Department IT")})


def test_verdict_matrix() -> None:
    assert decide_verdict(_trace("answered", None)) == Verdict.ANSWERED
    assert decide_verdict(_trace("error", "search_unavailable")) == Verdict.ERROR
    assert decide_verdict(_trace(reason="uncited")) == Verdict.GENERATION_MISS
    assert decide_verdict(_trace(reason="model_refusal")) == Verdict.GENERATION_MISS
    # Nothing above the bar even unfiltered: the documents do not cover it.
    assert decide_verdict(_trace(docs=[])) == Verdict.NOT_IN_CORPUS
    # Relevant documents exist and the policy withholds them, with nothing suspicious: correct.
    assert decide_verdict(_trace(docs=[_doc(allowed=False)])) == Verdict.WITHHELD_BY_POLICY
    # ... unless the document's tags look wrong, or the caller's attributes do.
    assert decide_verdict(_trace(docs=[_doc(allowed=False, problems=["tag mismatch"])])) == Verdict.MISCONFIGURATION
    assert decide_verdict(_trace(docs=[_doc(allowed=False)], caller_problems=["no dept"])) == Verdict.MISCONFIGURATION
    # A relevant document the caller MAY read, missing from their results: should have answered.
    assert decide_verdict(_trace(docs=[_doc(allowed=True)])) == Verdict.RETRIEVAL_MISS
    # The probe did not run: we do not know, and say so.
    assert decide_verdict(_trace(ran=False)) == Verdict.UNVERIFIED
    # An administrator's search is unfiltered already, so an empty result is conclusive.
    assert decide_verdict(_trace(ran=False), bypass=True) == Verdict.NOT_IN_CORPUS
    assert decide_verdict(_trace(reason="no_access", caller_problems=["no dept"])) == Verdict.MISCONFIGURATION


def test_finish_marks_the_blamed_stage_red_and_explains() -> None:
    rec = TraceRecorder(principal=HR_AMER, question="401k?")
    rec.trace.near_miss = NearMiss(ran=True, docs=[_doc(allowed=False, problems=["Classified HR but tagged IT."])])
    t = rec.finish(Answer(answer="not found", refused=True, refusal_reason="no_relevant_context"))
    assert t.verdict == Verdict.MISCONFIGURATION and t.is_problem
    assert t.failed_stage == "access"
    assert t.stage("outcome").status == StageStatus.FAIL
    text = "\n".join(t.diagnosis)
    assert "Benefits" in text and "withheld" in text and "Classified HR but tagged IT." in text


def test_correct_refusal_is_not_red() -> None:
    rec = TraceRecorder(principal=HR_AMER, question="weather on mars?")
    rec.trace.near_miss = NearMiss(ran=True, relevance_bar={"min_reranker_score": 1.2})
    t = rec.finish(Answer(answer="not found", refused=True, refusal_reason="no_relevant_context"))
    assert t.verdict == Verdict.NOT_IN_CORPUS and not t.is_problem and t.failed_stage is None
    assert t.stage("outcome").status == StageStatus.OK
    assert any("never ingested" in d for d in t.diagnosis)


def test_no_relevance_bar_is_disclosed() -> None:
    t = _trace(docs=[_doc(allowed=False)])
    t.near_miss.relevance_bar = {"min_reranker_score": 0.0, "min_score": 0.0}
    t.verdict = decide_verdict(t)
    assert any("no relevance bar" in d for d in build_diagnosis(t))


def test_timed_stage_failure_is_recorded_and_reraised() -> None:
    rec = TraceRecorder(principal=HR_AMER, question="q")
    try:
        with rec.timed("search"):
            raise RuntimeError("search query failed")
    except RuntimeError:
        pass
    assert rec.trace.stage("search").status == StageStatus.FAIL
    assert rec.trace.failed_stage == "search"
    rec.fail(RuntimeError("search query failed"))
    t = rec.finish(None)
    assert t.outcome == "error" and t.verdict == Verdict.ERROR


# --------------------------------------------------------------------------- expectations


def _exp(expected: str, required: list[str] | None = None) -> Expectation:
    return Expectation(id="e1", question="401k?", expected=expected, required_doc_ids=required or [],
                       created_at=datetime.now(UTC))


def test_judge() -> None:
    answered = _trace("answered", None)
    refused = _trace(docs=[])
    refused.verdict = Verdict.NOT_IN_CORPUS
    assert judge(_exp("answer"), answered, ["benefits"])[0]
    assert judge(_exp("answer", ["benefits"]), answered, ["benefits", "x"])[0]
    ok, detail = judge(_exp("answer", ["benefits"]), answered, ["other"])
    assert not ok and "benefits" in detail
    ok, detail = judge(_exp("answer"), refused, [])
    assert not ok and "no_relevant_context" in detail
    assert judge(_exp("no_answer"), refused, [])[0]
    assert not judge(_exp("no_answer"), answered, ["x"])[0]


# --------------------------------------------------------------------------- health


def _row(i: int, verdict: Verdict, *, outcome: str = "refused", subject: str = "u1", ms: float = 100.0,
         replay: str | None = None) -> TraceSummaryRow:
    return TraceSummaryRow(id=f"t{i}", at=datetime.now(UTC) - timedelta(minutes=i), subject=subject, outcome=outcome,
                           verdict=verdict, duration_ms=ms, replay_of=replay)


def test_summary_counts_problems_not_refusals() -> None:
    rows = ([_row(i, Verdict.NOT_IN_CORPUS) for i in range(8)]
            + [_row(8, Verdict.MISCONFIGURATION), _row(9, Verdict.ANSWERED, outcome="answered", ms=900)]
            + [_row(10, Verdict.ERROR, outcome="error", replay="e1")])  # replays are not users
    s = summarise(rows, since=datetime.now(UTC) - timedelta(hours=1), minutes=60, expectations=[],
                  problem_rate=0.10, error_rate=0.05, p95_ms=15000)
    assert s["total"] == 10 and s["replays"] == 1
    metric = {m["key"]: m for m in s["metrics"]}
    assert metric["problem_rate"]["value"] == 0.1 and metric["problem_rate"]["status"] == "warn"
    assert metric["error_rate"]["value"] == 0.0 and metric["error_rate"]["status"] == "ok"
    assert s["repeated_refusals"][0]["subject"] == "u1" and s["repeated_refusals"][0]["problems"] == 1


def test_summary_flags_failing_expectations() -> None:
    e = _exp("answer").model_copy(update={"last_result": "fail", "last_detail": "did not cite benefits"})
    s = summarise([], since=datetime.now(UTC), minutes=60, expectations=[e], problem_rate=0.1, error_rate=0.05,
                  p95_ms=15000)
    assert s["status"] == "fail" and s["failing_expectations"][0]["detail"] == "did not cite benefits"


# --------------------------------------------------------------------------- per-attribute clauses (index-side verdicts)


def test_attribute_clauses_compose_into_the_real_filter() -> None:
    """Joined the way decide() joins them, the per-attribute clauses ARE the filter - so the index judging them one
    at a time cannot disagree with the query that ran."""
    for p in (HR_AMER, person(department=["HR"], region=["UK"]), person(region=["AMER"], employee_id=["E1"]),
              person(department=["HR"], region=["AMER"], employee_id=["E9"])):
        clauses = ENGINE.attribute_clauses(p)
        all_of = [clauses[n] for n in POLICY.combine.all_of]
        grants = [clauses[n] for n in POLICY.combine.grant_any_of if clauses[n] is not None]
        parts = ([f"({' and '.join(all_of)})"] if all(c is not None for c in all_of) else []) + [f"({g})" for g in grants]
        assert (" or ".join(parts) or "__deny_all__") == ENGINE.decide(p).odata, p.attributes


def test_attribute_clauses_agree_with_the_predicate_per_attribute() -> None:
    from rag_os.infrastructure.search.odata_eval import compile_filter

    doc = {"acl_department": ["HR"], "acl_region": ["US"], "acl_clearance": 1, "acl_employee_id": ["E9"]}
    acl = {"department": ["HR"], "region": ["US"], "clearance": 1, "employee_id": ["E9"]}
    local = ENGINE.explain_document(HR_AMER, acl)
    for name, clause in ENGINE.attribute_clauses(HR_AMER).items():
        index_says = clause is not None and compile_filter(clause)(doc)
        assert index_says == local[name].passed, name
    verdicts = {n: c is not None and compile_filter(c)(doc) for n, c in ENGINE.attribute_clauses(HR_AMER).items()}
    assert ENGINE.combine_verdicts(verdicts) == ENGINE.allows(HR_AMER, acl)  # the employee_id share lets them in


def test_attribute_clauses_admin_and_missing_values() -> None:
    admin = Principal(subject="a", issuer_kind="entra", roles={"admin"})
    assert set(ENGINE.attribute_clauses(admin).values()) == {"true"}
    no_dept = ENGINE.attribute_clauses(person(region=["AMER"]))
    assert no_dept["department"] is None and no_dept["clearance"] == "acl_clearance le 0"
    assert no_dept["employee_id"] is None
