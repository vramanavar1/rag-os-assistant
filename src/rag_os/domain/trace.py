"""Query traces: what happened to one question, stage by stage, and whether the outcome was right.

A trace exists so an administrator can answer "why did this person get no answer?" from evidence instead of
from guesses. Two things make that possible:

* Every stage records its inputs, outputs and timing, including the parts the answer itself throws away (the
  access decision, hits removed by the relevance bar, the model's stop reason).
* A refused question is re-checked by a near-miss probe: the same search with ONLY the access clause removed.
  Because everything else is held constant, any difference between the two result sets is caused by access
  and nothing else. The verdict is computed from that comparison, never from a model's opinion.

What a trace cannot know is what the right answer *should* have been. That is stated by a person, as an
Expectation, and checked by replaying the question.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class StageStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


class Verdict(StrEnum):
    ANSWERED = "answered"
    """Answered, and every citation came from a passage this caller was allowed to retrieve."""
    NOT_IN_CORPUS = "not_in_corpus"
    """Correct no-answer: nothing cleared the relevance bar even with the access filter removed."""
    WITHHELD_BY_POLICY = "withheld_by_policy"
    """Correct no-answer: relevant documents exist, the policy withholds them, and nothing looks misconfigured."""
    MISCONFIGURATION = "misconfiguration"
    """Relevant documents are withheld, and a document's tags or the caller's attributes look wrong."""
    RETRIEVAL_MISS = "retrieval_miss"
    """A relevant document the caller IS allowed to read was not returned to them. Should have answered."""
    GENERATION_MISS = "generation_miss"
    """Passages were found and sent to the model, which refused or answered without citations."""
    ERROR = "error"
    """The pipeline failed or refused before answering (exception, index not ready, search unavailable)."""
    UNVERIFIED = "unverified"
    """No answer, and the near-miss probe did not run, so whether that was correct is unknown."""


PROBLEM_VERDICTS = frozenset({Verdict.MISCONFIGURATION, Verdict.RETRIEVAL_MISS, Verdict.GENERATION_MISS,
                              Verdict.ERROR})

VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.ANSWERED: "Answered with citations",
    Verdict.NOT_IN_CORPUS: "Correct no-answer: not in the documents",
    Verdict.WITHHELD_BY_POLICY: "Correct no-answer: withheld by access policy",
    Verdict.MISCONFIGURATION: "Suspected misconfiguration",
    Verdict.RETRIEVAL_MISS: "Should have answered: retrieval",
    Verdict.GENERATION_MISS: "Should have answered: generation",
    Verdict.ERROR: "Error",
    Verdict.UNVERIFIED: "No answer, not verified",
}

# The fixed order the pipeline runs in, and the labels the troubleshooting page shows.
STAGES: tuple[tuple[str, str], ...] = (
    ("request", "Request"),
    ("identity", "User context"),
    ("guard", "Index guard"),
    ("access", "Access filter"),
    ("query", "Query prep"),
    ("embed", "Embed query"),
    ("search", "Search"),
    ("relevance", "Relevance bar"),
    ("prompt", "Prompt"),
    ("llm", "Model"),
    ("grounding", "Citations"),
    ("outcome", "Outcome"),
)


class TraceStage(BaseModel):
    name: str
    label: str
    status: StageStatus = StageStatus.SKIPPED
    duration_ms: float | None = None
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class AttributeCheck(BaseModel):
    """One access attribute evaluated for one document and one caller, by the policy's own predicate."""

    passed: bool
    doc_values: list[str] | int | None = None
    caller_values: list[str] | int | None = None
    note: str = ""


class NearMissDoc(BaseModel):
    doc_id: str
    chunk_id: str
    title: str
    path: str
    page: int | None = None
    score: float
    reranker_score: float | None = None
    allowed: bool
    checks: dict[str, AttributeCheck] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)
    """Misconfiguration evidence on THIS document (missing required tag, facet and access tag disagree)."""


class NearMiss(BaseModel):
    ran: bool = False
    skipped_reason: str = ""
    relevance_bar: dict[str, float] = Field(default_factory=dict)
    """The thresholds applied - identical to the caller's own search, or the comparison would prove nothing."""
    docs: list[NearMissDoc] = Field(default_factory=list)
    below_bar: int = 0


class QueryTrace(BaseModel):
    id: str
    correlation_id: str | None = None
    at: datetime
    subject: str = ""
    display_name: str = ""
    issuer: str = ""
    roles: list[str] = Field(default_factory=list)
    attributes: dict[str, list[str] | int] = Field(default_factory=dict)
    """The caller's mapped attributes, as the filter saw them. What a replay runs as."""
    question: str = ""
    filters: dict[str, list[str]] = Field(default_factory=dict)
    history_turns: int = 0
    answer: str = ""
    outcome: str = "answered"  # answered | refused | error
    reason: str | None = None
    verdict: Verdict = Verdict.UNVERIFIED
    failed_stage: str | None = None
    duration_ms: float = 0.0
    tokens: int = 0
    provider: str = ""
    model: str = ""
    stages: list[TraceStage] = Field(default_factory=list)
    near_miss: NearMiss = Field(default_factory=NearMiss)
    caller_problems: list[str] = Field(default_factory=list)
    diagnosis: list[str] = Field(default_factory=list)
    replay_of: str | None = None
    """The expectation this trace was produced by, when it is a replay rather than a real user's question."""

    @property
    def is_problem(self) -> bool:
        return self.verdict in PROBLEM_VERDICTS

    def stage(self, name: str) -> TraceStage:
        return next(s for s in self.stages if s.name == name)


class TraceSummaryRow(BaseModel):
    """One line of the traces list - no stage data, which is the bulk of a trace."""

    id: str
    correlation_id: str | None = None
    at: datetime
    subject: str = ""
    display_name: str = ""
    question: str = ""
    outcome: str
    reason: str | None = None
    verdict: Verdict
    failed_stage: str | None = None
    duration_ms: float = 0.0
    tokens: int = 0
    replay_of: str | None = None


class Expectation(BaseModel):
    """What the right outcome for a question is, stated by a person - the one thing a trace cannot infer."""

    id: str
    question: str
    attributes: dict[str, list[str] | int] = Field(default_factory=dict)
    roles: list[str] = Field(default_factory=list)
    filters: dict[str, list[str]] = Field(default_factory=dict)
    expected: str  # "answer" | "no_answer"
    required_doc_ids: list[str] = Field(default_factory=list)
    """For an expected answer: every one of these documents must be cited, or the replay fails."""
    note: str = ""
    created_by: str = ""
    created_at: datetime
    from_trace_id: str | None = None
    last_result: str | None = None  # pass | fail | None (never run)
    last_detail: str = ""
    last_run_at: datetime | None = None
    last_trace_id: str | None = None


def judge(expectation: Expectation, trace: QueryTrace, cited_doc_ids: list[str]) -> tuple[bool, str]:
    """Did the replay produce the outcome the expectation states? Pure, so it is tested on its own."""
    answered = trace.outcome == "answered"
    if expectation.expected == "no_answer":
        if answered:
            return False, "expected no answer, but it answered"
        return True, f"no answer, as expected ({trace.reason or trace.outcome})"
    if not answered:
        return False, f"expected an answer, got {trace.reason or trace.outcome} ({VERDICT_LABELS[trace.verdict]})"
    missing = [d for d in expectation.required_doc_ids if d not in cited_doc_ids]
    if missing:
        return False, f"answered, but did not cite {', '.join(missing)}"
    return True, "answered" + (" and cited every required document" if expectation.required_doc_ids else "")
