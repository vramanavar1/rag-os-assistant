"""Recording what happened to one question, and judging whether the outcome was right.

`TraceRecorder` collects stage data while AnswerQuery runs. `NearMissProbe` re-runs a refused search with the
access clause removed and nothing else changed. `decide_verdict` and `build_diagnosis` turn the evidence into
a verdict and plain sentences. None of this changes the answer a caller receives: tracing is observation only,
and the probe's results are never returned to the caller.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from rag_os.application.ports import EmbeddingProvider, SearchIndex, SearchRequest
from rag_os.application.services.access_policy import AccessPolicyEngine, odata_quote
from rag_os.application.services.index_schema import BASE_SELECT
from rag_os.application.services.prompts import GUARD_REFUSALS
from rag_os.application.services.relevance import apply_relevance_bar
from rag_os.domain.access import Principal
from rag_os.domain.answers import Answer, SearchHit
from rag_os.domain.classification import FacetSchema
from rag_os.domain.documents import PRIVATE_SOURCE_KEY, TagSet
from rag_os.domain.errors import RagOsError
from rag_os.domain.trace import (
    STAGES,
    VERDICT_LABELS,
    AttributeCheck,
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
    """The caller's search again, with ONLY the access clause removed - and then the index asked, attribute by
    attribute, whether each relevant document passes.

    Same query text, same vector, same facet filters, same candidate count, same semantic setting and the same
    relevance bar - so a document that shows up here and not in the caller's results is missing because of
    access (or because it is not current, which is checked too), and for no other reason.

    Access tags are not retrievable from the index (by design), so they are never selected. The verdict for
    each attribute comes from the index evaluating that attribute's own clause - the same OData the real query
    uses, so it cannot disagree with it. The tag VALUES shown next to a verdict, and the misconfiguration checks,
    come from the tags recorded in the state store; if those disagree with the index, that is reported too.

    The results describe documents the caller may not read. They go into an administrator-only trace and
    nowhere else.
    """

    def __init__(self, *, index: SearchIndex, embedder: EmbeddingProvider, engine: AccessPolicyEngine,
                 facets: FacetSchema, thresholds: dict[str, float], retrievable: set[str] | None = None,
                 doc_tags: Callable[[list[str]], dict[str, TagSet]] | None = None, top: int = 8,
                 candidates: int = 50, semantic: bool = True) -> None:
        self.index = index
        self.embedder = embedder
        self.engine = engine
        self.facets = facets
        self.thresholds = thresholds
        self.doc_tags = doc_tags
        self.top = top
        self.candidates = candidates
        self.semantic = semantic
        acl_names = {a.name for a in engine.policy.attributes}
        # A facet that shares its name with an access attribute (department, region) is what lets a document's
        # classification be compared with its access tag.
        self._facet_fields = {f.name: f.field for f in facets.facets if f.name in acl_names}
        wanted = [*BASE_SELECT, "is_current", *self._facet_fields.values()]
        # Never a non-retrievable field: Azure rejects the whole query (400) if one is named in $select.
        self.select = [f for f in dict.fromkeys(wanted) if retrievable is None or f in retrievable]

    async def run(self, principal: Principal, *, text: str, keyword_query: str, base_filter: str | None,
                  vector: list[float] | None) -> NearMiss:
        if vector is None:
            vector, _ = await self.embedder.embed_query(text)
        hits = await self.index.search(SearchRequest(
            text=keyword_query, vector=vector, odata_filter=base_filter or None, top=self.top,
            candidates=self.candidates, semantic=self.semantic, select=self.select))
        kept, dropped = apply_relevance_bar(hits, self.thresholds.get("min_reranker_score", 0.0),
                                            self.thresholds.get("min_score", 0.0))
        best: dict[str, SearchHit] = {}
        for h in kept:  # one row per document; ranked order, so the first chunk is the best one
            best.setdefault(h.doc_id, h)
        if not best:
            return NearMiss(ran=True, relevance_bar=dict(self.thresholds), below_bar=len(dropped))

        passed = await self._index_verdicts(principal, list(best.values()), vector)
        recorded = self.doc_tags(list(best)) if self.doc_tags else {}
        docs = [self._row(principal, h, passed, recorded.get(doc_id)) for doc_id, h in best.items()]
        return NearMiss(ran=True, relevance_bar=dict(self.thresholds), docs=docs, below_bar=len(dropped))

    async def _index_verdicts(self, principal: Principal, hits: list[SearchHit],
                              vector: list[float]) -> dict[str, set[str]]:
        """{attribute: doc_ids whose chunk passes that attribute's clause}, as judged by the index."""
        ids = " or ".join(f"chunk_id eq '{odata_quote(h.chunk_id)}'" for h in hits)
        out: dict[str, set[str]] = {}
        for name, clause in self.engine.attribute_clauses(principal).items():
            if clause is None:
                out[name] = set()
                continue
            res = await self.index.search(SearchRequest(
                text=None, vector=vector, odata_filter=f"({ids}) and ({clause})", top=len(hits),
                candidates=max(self.candidates, len(hits)), semantic=False, select=["chunk_id", "doc_id"]))
            out[name] = {r.doc_id for r in res}
        return out

    def _row(self, principal: Principal, h: SearchHit, passed: dict[str, set[str]],
             tags: TagSet | None) -> NearMissDoc:
        acl: dict[str, list[str] | int | None] = {}
        facets: dict[str, list[str]] = {}
        private = False
        if tags is not None:
            clean = self.engine.validate_doc_acl(tags.acl)
            acl = {a.name: clean.get(a.name) for a in self.engine.policy.attributes}
            facets = self.facets.expand_for_index(tags.facets)
            private = tags.sources.get(PRIVATE_SOURCE_KEY) == "private"
        else:  # not in the state store: classification can still be read from the index
            facets = {name: list(h.fields.get(field) or []) for name, field in self._facet_fields.items()}
        local = self.engine.explain_document(principal, acl)
        verdicts = {name: h.doc_id in docs for name, docs in passed.items()}
        checks: dict[str, AttributeCheck] = {}
        for name, ok in verdicts.items():
            base = local.get(name) or AttributeCheck(passed=ok)
            note = base.note if not ok else ""
            if tags is None:
                note = note or ("" if ok else "the index says no (tags not recorded in the state store)")
            elif ok != base.passed:
                note = (f"the index says {'yes' if ok else 'no'}, the recorded tags say "
                        f"{'yes' if base.passed else 'no'}")
            checks[name] = base.model_copy(update={"passed": ok, "note": note})
        current = h.fields.get("is_current")
        if current is not True and "is_current" in self.select:
            checks["is_current"] = AttributeCheck(passed=False, doc_values=None, caller_values=None,
                                                  note="this chunk is not marked current, so every search skips it")
        allowed = self.engine.combine_verdicts(verdicts) and checks.get("is_current", AttributeCheck(passed=True)).passed

        problems: list[str] = []
        if "is_current" in checks:
            problems.append("The document's chunks are not marked current (is_current is not true), so every search "
                            "skips them. Re-ingest it.")
        if tags is not None and not allowed:
            if any(c.note.startswith("the index says") for c in checks.values()):
                problems.append("The access tags in the index differ from the tags recorded for this document. "
                                "Re-tag it (Edit access tags) or re-ingest it.")
            if not private:
                problems.extend(self.engine.document_problems(acl, facets))
        return NearMissDoc(doc_id=h.doc_id, chunk_id=h.chunk_id, title=h.title, path=h.path, page=h.page,
                           score=h.score, reranker_score=h.reranker_score, allowed=allowed, private=private,
                           checks=checks, problems=problems)


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
            if d.private:
                out.append(f"{where} is relevant but private to its uploader (Only me was ticked at upload) - "
                           f"withheld on purpose.")
            elif d.allowed:
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
