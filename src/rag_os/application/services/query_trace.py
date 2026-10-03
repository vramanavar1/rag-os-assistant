"""Recording what happened to one question, and judging whether the outcome was right.

`TraceRecorder` collects stage data while AnswerQuery runs. `NearMissProbe` re-runs a refused search with the
access clause removed and nothing else changed. `decide_verdict` and `build_diagnosis` turn the evidence into
a verdict and plain sentences. None of this changes the answer a caller receives: tracing is observation only,
and the probe's results are never returned to the caller.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from rag_os.application.ports import EmbeddingProvider, SearchIndex, SearchRequest
from rag_os.application.services.access_policy import AccessPolicyEngine
from rag_os.application.services.index_schema import BASE_SELECT
from rag_os.application.services.prompts import GUARD_REFUSALS
from rag_os.application.services.relevance import apply_relevance_bar
from rag_os.domain.access import Principal
from rag_os.domain.answers import Answer, SearchHit
from rag_os.domain.classification import FacetSchema
from rag_os.domain.errors import RagOsError
from rag_os.domain.trace import (
    STAGES,
    VERDICT_LABELS,
    NearMiss,
    NearMissDoc,
    QueryTrace,
    StageStatus,
    TraceStage,
    Verdict,
)

MAX_ANSWER_CHARS = 4000


def _error_text(e: BaseException) -> str:
    status = getattr(e, "status_code", None)
    code = getattr(e, "code", None) if isinstance(e, RagOsError) else None
    head = type(e).__name__ + (f" {status}" if status else "") + (f" ({code})" if code else "")
    return f"{head}: {e}"[:1000]


def hit_row(h: SearchHit, **extra: Any) -> dict[str, Any]:
    return {"doc_id": h.doc_id, "chunk_id": h.chunk_id, "title": h.title, "path": h.path, "page": h.page,
            "score": round(h.score, 5), "reranker_score": h.reranker_score, **extra}


class TraceRecorder:
    def __init__(self, *, principal: Principal, question: str, filters: dict[str, list[str]] | None = None,
                 history_turns: int = 0, correlation_id: str | None = None, replay_of: str | None = None,
                 max_hits: int = 15) -> None:
        self._t0 = time.perf_counter()
        self.max_hits = max_hits
        self.current: str | None = None
        self.bypass = principal.is_admin
        self.trace = QueryTrace(
            id=uuid.uuid4().hex, correlation_id=correlation_id, at=datetime.now(UTC),
            subject=principal.subject, display_name=principal.display_name, issuer=principal.issuer_kind,
            roles=sorted(principal.roles), attributes=dict(principal.attributes), question=question[:2000],
            filters=dict(filters or {}), history_turns=history_turns, replay_of=replay_of,
            stages=[TraceStage(name=n, label=label) for n, label in STAGES],
        )
        self.set("request", StageStatus.OK, f"{len(question)} characters, {history_turns} earlier turns",
                 question=question[:2000], filters=dict(filters or {}), history_turns=history_turns,
                 correlation_id=correlation_id, replay_of=replay_of)

    # ------------------------------------------------------------------ recording

    def set(self, name: str, status: StageStatus, summary: str = "", duration_ms: float | None = None,
            **data: Any) -> TraceStage:
        s = self.trace.stage(name)
        s.status = status
        if summary:
            s.summary = summary
        if duration_ms is not None:
            s.duration_ms = round(duration_ms, 2)
        s.data.update(data)
        if status == StageStatus.FAIL and self.trace.failed_stage is None:
            self.trace.failed_stage = name
        return s

    @contextmanager
    def timed(self, name: str) -> Iterator[TraceStage]:
        """Time a stage. An exception inside marks it failed (and is re-raised); otherwise it is OK unless set."""
        s = self.trace.stage(name)
        self.current = name
        t = time.perf_counter()
        try:
            yield s
        except BaseException as e:
            self.set(name, StageStatus.FAIL, _error_text(e), error=_error_text(e))
            raise
        finally:
            s.duration_ms = round((time.perf_counter() - t) * 1000, 2)
            if s.status == StageStatus.SKIPPED:
                s.status = StageStatus.OK
            self.current = None

    def hits(self, hits: list[SearchHit], **extra: Any) -> list[dict[str, Any]]:
        return [hit_row(h, **extra) for h in hits[: self.max_hits]]

    # ------------------------------------------------------------------ closing

    def fail(self, e: BaseException) -> None:
        """An exception that escaped the pipeline. Attributed to the stage that was running, if any."""
        name = self.current or self.trace.failed_stage or "request"
        if self.trace.stage(name).status != StageStatus.FAIL:
            self.set(name, StageStatus.FAIL, _error_text(e), error=_error_text(e))
        self.trace.failed_stage = self.trace.failed_stage or name
        self.trace.outcome = "error"
        self.trace.reason = getattr(e, "code", None) or type(e).__name__

    def guard_refused(self, reason: str) -> None:
        self.set("guard", StageStatus.FAIL, GUARD_REFUSALS.get(reason, reason), reason=reason)

    def finish(self, answer: Answer | None) -> QueryTrace:
        t = self.trace
        t.duration_ms = round((time.perf_counter() - self._t0) * 1000, 2)
        if answer is not None:
            t.answer = answer.answer[:MAX_ANSWER_CHARS]
            t.reason = answer.refusal_reason
            t.outcome = "refused" if answer.refused else "answered"
            if answer.refusal_reason in GUARD_REFUSALS:
                t.outcome = "error"
            t.tokens = answer.usage.total + answer.usage.embedding
            t.provider, t.model = answer.provider, answer.model
        t.verdict = decide_verdict(t, bypass=self.bypass)
        t.diagnosis = build_diagnosis(t)
        problem = t.is_problem
        if problem and t.failed_stage is None:
            # No stage raised, but the verdict says something is wrong: colour the stage the evidence points at.
            blamed = self.trace.stage(_blame(t))
            self.set(blamed.name, StageStatus.FAIL,
                     f"{VERDICT_LABELS[t.verdict]}: the evidence points at this stage - see the diagnosis"
                     + (" and the near-miss check" if t.near_miss.ran else "") + ".",
                     verdict=t.verdict.value, ran_ok=blamed.summary)
        status = StageStatus.FAIL if problem else (StageStatus.WARN if t.verdict == Verdict.UNVERIFIED
                                                   else StageStatus.OK)
        self.set("outcome", status, VERDICT_LABELS[t.verdict], duration_ms=t.duration_ms,
                 outcome=t.outcome, reason=t.reason, verdict=t.verdict.value, answer=t.answer)
        return t


def _blame(t: QueryTrace) -> str:
    """Which stage the picture should colour red when no stage failed outright."""
    if t.verdict == Verdict.GENERATION_MISS:
        return "grounding"
    if t.verdict == Verdict.RETRIEVAL_MISS:
        return "search"
    if t.verdict == Verdict.MISCONFIGURATION:
        return "identity" if t.caller_problems else "access"
    return "outcome"


# --------------------------------------------------------------------------- near-miss probe


class NearMissProbe:
    """The caller's search again, with ONLY the access clause removed.

    Same query text, same vector, same facet filters, same candidate count, same semantic setting and the same
    relevance bar - so a document that shows up here and not in the caller's results is missing because of
    access, and for no other reason. Each such document is then evaluated attribute by attribute with the
    policy's own predicate.

    The results describe documents the caller may not read. They go into an administrator-only trace and
    nowhere else.
    """

    def __init__(self, *, index: SearchIndex, embedder: EmbeddingProvider, engine: AccessPolicyEngine,
                 facets: FacetSchema, thresholds: dict[str, float], top: int = 8, candidates: int = 50,
                 semantic: bool = True) -> None:
        self.index = index
        self.embedder = embedder
        self.engine = engine
        self.thresholds = thresholds
        self.top = top
        self.candidates = candidates
        self.semantic = semantic
        policy = engine.policy
        self._acl = {a.name: (a.field, a.is_numeric) for a in policy.attributes}
        # A facet that shares its name with an access attribute (department, region) is what lets a document's
        # classification be compared with its access tag.
        self._facet_fields = {f.name: f.field for f in facets.facets if f.name in self._acl}
        self.select = list(dict.fromkeys(
            [*BASE_SELECT, *(f for f, _ in self._acl.values()), *self._facet_fields.values()]))

    async def run(self, principal: Principal, *, text: str, keyword_query: str, base_filter: str | None,
                  vector: list[float] | None) -> NearMiss:
        if vector is None:
            vector, _ = await self.embedder.embed_query(text)
        hits = await self.index.search(SearchRequest(
            text=keyword_query, vector=vector, odata_filter=base_filter or None, top=self.top,
            candidates=self.candidates, semantic=self.semantic, select=self.select))
        kept, dropped = apply_relevance_bar(hits, self.thresholds.get("min_reranker_score", 0.0),
                                            self.thresholds.get("min_score", 0.0))
        docs: dict[str, NearMissDoc] = {}
        for h in kept:
            if h.doc_id in docs:  # one row per document; ranked order, so the first chunk is the best one
                continue
            acl = self._doc_acl(h)
            facets = {name: list(h.fields.get(field) or []) for name, field in self._facet_fields.items()}
            allowed = self.engine.allows(principal, {k: v for k, v in acl.items() if v is not None})
            docs[h.doc_id] = NearMissDoc(
                doc_id=h.doc_id, chunk_id=h.chunk_id, title=h.title, path=h.path, page=h.page, score=h.score,
                reranker_score=h.reranker_score, allowed=allowed,
                checks=self.engine.explain_document(principal, acl),
                problems=[] if allowed else self.engine.document_problems(acl, facets))
        return NearMiss(ran=True, relevance_bar=dict(self.thresholds), docs=list(docs.values()),
                        below_bar=len(dropped))

    def _doc_acl(self, h: SearchHit) -> dict[str, list[str] | int | None]:
        acl: dict[str, list[str] | int | None] = {}
        for name, (field, numeric) in self._acl.items():
            v = h.fields.get(field)
            if numeric:
                acl[name] = int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None
            else:
                acl[name] = [str(x) for x in v] if isinstance(v, list) and v else None
        return acl


# --------------------------------------------------------------------------- verdict and diagnosis


def decide_verdict(t: QueryTrace, *, bypass: bool = False) -> Verdict:
    """The verdict follows from recorded evidence only. See Verdict for what each one means."""
    if t.outcome == "error":
        return Verdict.ERROR
    if t.outcome == "answered":
        return Verdict.ANSWERED
    if t.reason in ("model_refusal", "uncited"):
        return Verdict.GENERATION_MISS
    nm = t.near_miss
    if t.reason == "no_access":
        # DENY_ALL happens only when a required attribute is missing and nothing is shared individually.
        return Verdict.MISCONFIGURATION if t.caller_problems else Verdict.WITHHELD_BY_POLICY
    if t.reason == "no_relevant_context":
        if bypass:
            # No access filter applied at all, so the caller's own search WAS the unfiltered search.
            return Verdict.NOT_IN_CORPUS
        if not nm.ran:
            return Verdict.UNVERIFIED
        if not nm.docs:
            return Verdict.NOT_IN_CORPUS
        if any(d.allowed for d in nm.docs):
            return Verdict.RETRIEVAL_MISS
        if t.caller_problems or any(d.problems for d in nm.docs):
            return Verdict.MISCONFIGURATION
        return Verdict.WITHHELD_BY_POLICY
    return Verdict.UNVERIFIED


def _bar_text(bar: dict[str, float], best: dict[str, Any]) -> str:
    """"best reranker 0.91, needs 1.2" - only the parts of the bar that are actually switched on."""
    parts = []
    if bar.get("min_reranker_score", 0) > 0:
        parts.append(f"best reranker score {best.get('reranker_score', '—')}, needs {bar['min_reranker_score']}")
    if bar.get("min_score", 0) > 0:
        parts.append(f"best score {best.get('score', '—')}, needs {bar['min_score']}")
    return "; ".join(parts) or "no bar configured"


def build_diagnosis(t: QueryTrace) -> list[str]:
    out: list[str] = [VERDICT_LABELS[t.verdict] + "."]
    if t.verdict == Verdict.ERROR:
        st = t.failed_stage and t.stage(t.failed_stage)
        if st:
            out.append(f"{st.label} failed: {st.summary}")
        return out
    out.extend(t.caller_problems)
    if t.verdict == Verdict.ANSWERED:
        return out

    llm = t.stage("llm").data
    if t.reason == "uncited":
        out.append(f"The model was given {t.stage('prompt').data.get('blocks', 0)} passages and answered without "
                   f"citing any, so the grounding guard withheld the answer.")
    elif t.reason == "model_refusal":
        out.append(f"The model declined to answer (stop reason: {llm.get('stop_reason') or 'refusal'}"
                   + (", and the fallback model also declined" if llm.get("fallback_used") else "") + ").")

    rel = t.stage("relevance").data
    if rel.get("kept") == 0 and rel.get("dropped"):
        best = rel.get("best_dropped") or {}
        out.append(f"{len(rel['dropped'])} passage(s) this person may read were found, but none cleared the "
                   f"relevance bar ({_bar_text(rel.get('thresholds') or {}, best)}).")

    nm = t.near_miss
    if nm.ran:
        bar = nm.relevance_bar
        if not any(v > 0 for v in bar.values()):
            out.append("This search backend applies no relevance bar, so the documents below are the nearest "
                       "matches, not necessarily relevant ones - read them before concluding.")
        if not nm.docs:
            out.append("Even with the access filter removed, nothing cleared the relevance bar: the documents do "
                       "not cover this, or the file that does was never ingested (check it under Documents).")
        for d in nm.docs[:5]:
            failing = [f"{k} ({c.note})" for k, c in d.checks.items() if not c.passed and c.note]
            where = f"'{d.title or d.path}'" + (f" page {d.page}" if d.page else "")
            if d.allowed:
                out.append(f"{where} is relevant AND this person may read it, yet their own search did not return "
                           f"it. That points at an inconsistency between the access filter and the index - report "
                           f"it with this trace id.")
            else:
                grants = [k for k, c in d.checks.items() if c.passed]
                out.append(f"{where} is relevant but withheld: " + "; ".join(failing or ["no attribute matched"])
                           + (f" (passes: {', '.join(grants)})" if grants else "") + ".")
                out.extend(f"  {p}" for p in d.problems)
    elif nm.skipped_reason and t.verdict == Verdict.UNVERIFIED:
        out.append(f"The near-miss check did not run: {nm.skipped_reason}")
    return out
