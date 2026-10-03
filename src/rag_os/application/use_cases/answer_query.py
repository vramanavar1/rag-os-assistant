"""AnswerQuery: permission-aware, grounded answering with citations and per-query token accounting."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence

from rag_os.application.ports import LlmMessage, LlmProvider, RetrievalResult, Retriever
from rag_os.application.services.access_policy import AccessDecision, AccessPolicyEngine, odata_quote
from rag_os.application.services.index_schema import CURRENT_FILTER
from rag_os.application.services.prompts import (
    ANSWER_SYSTEM,
    CONDENSE_SYSTEM,
    NO_ACCESS_MESSAGE,
    NOT_FOUND_MESSAGE,
    build_condense_prompt,
    build_context,
    build_user_prompt,
    cited_indexes,
)
from rag_os.application.services.query_expansion import QueryExpander
from rag_os.application.services.query_trace import NearMissProbe, TraceRecorder, hit_row
from rag_os.domain.access import Principal
from rag_os.domain.answers import Answer, ChatTurn, Citation, SearchHit, TokenUsage
from rag_os.domain.classification import FacetDef, FacetSchema
from rag_os.domain.errors import ValidationFailed
from rag_os.domain.trace import StageStatus

log = logging.getLogger(__name__)
audit = logging.getLogger("rag_os.audit")

MAX_QUESTION_CHARS = 2000
_FILTER_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.@/&-]{0,127}$")
_NOT_FOUND_HINTS = ("could not find", "couldn't find", "not in the documents", "no information")
_WS = re.compile(r"\s+")


def _collapse_duplicates(hits: list[SearchHit]) -> tuple[list[SearchHit], dict[str, list[str]]]:
    """One passage, one context block - however many documents happen to hold it.

    The same file uploaded twice, crawled from two sources, or left behind by an ingest that failed between
    upserting the new version and sweeping the old one, all put byte-identical text in the index under
    different chunk ids. Retrieval then ranks them identically and adjacently, so with top_k of 8 a duplicate
    costs a slot; worse, the answer prompt says to prefer the most recent when blocks conflict, which makes
    two copies of one passage read as two sources agreeing.

    Every hit here has already passed the caller's access filter, so listing the other locations discloses
    nothing they could not already retrieve.
    """
    kept: list[SearchHit] = []
    first_by_text: dict[str, SearchHit] = {}
    also_at: dict[str, list[str]] = {}
    for hit in hits:
        key = _WS.sub(" ", hit.content).strip().casefold()
        winner = first_by_text.get(key)
        if winner is None:
            first_by_text[key] = hit
            kept.append(hit)
            continue
        # Ranked order, so the first occurrence is the best-scoring one; the rest become "also at".
        if hit.path and hit.path not in also_at.setdefault(winner.chunk_id, []):
            also_at[winner.chunk_id].append(hit.path)
    return kept, also_at


class AnswerQuery:
    def __init__(
        self,
        *,
        engine: AccessPolicyEngine,
        facets: FacetSchema,
        retriever: Retriever,
        llm: LlmProvider,
        utility_llm: LlmProvider,
        fallback_llm: LlmProvider | None = None,
        top_k: int = 8,
        history_turns: int = 6,
        max_output_tokens: int = 8000,
        probe: NearMissProbe | None = None,
    ) -> None:
        self.engine = engine
        self.facets = facets
        self.facet_fields = {f.name: f.field for f in facets.facets}
        self.expander = QueryExpander(facets)
        self.retriever = retriever
        self.llm = llm
        self.utility = utility_llm
        self.fallback = fallback_llm
        self.top_k = top_k
        self.history_turns = history_turns
        self.max_output_tokens = max_output_tokens
        self.probe = probe  # only ever run for a traced, refused question

    def facet_filter(self, filters: dict[str, list[str]] | None) -> str | None:
        clauses = []
        for name, values in (filters or {}).items():
            field = self.facet_fields.get(name)
            fd = self.facets.get(name)
            if field is None or fd is None:
                raise ValidationFailed(f"unknown facet filter '{name}'")
            canon = []
            for v in values[:20]:
                if not _FILTER_VALUE.match(str(v)):
                    raise ValidationFailed(f"invalid value for facet '{name}'")
                c = fd.normalise(str(v))
                if c:
                    canon.append(c)
            if canon:
                joined = odata_quote("|".join(canon))
                clauses.append(f"{field}/any(v: search.in(v, '{joined}', '|'))")
        return " and ".join(clauses) if clauses else None

    def _decide(self, principal: Principal) -> AccessDecision:
        d = self.engine.decide(principal)
        if d.bypass:
            audit.info("admin access bypass", extra={"subject": principal.subject, "issuer": principal.issuer_kind})
        return d

    def access_filter(self, principal: Principal) -> tuple[str | None, bool]:
        """(combined filter or None for unrestricted, deny_all)."""
        d = self._decide(principal)
        if d.deny_all:
            return None, True
        return d.odata, False

    @staticmethod
    def combine(*parts: str | None) -> str:
        return " and ".join(f"({p})" for p in parts if p)

    async def ask(
        self,
        principal: Principal,
        question: str,
        history: Sequence[ChatTurn] = (),
        filters: dict[str, list[str]] | None = None,
        correlation_id: str | None = None,
        trace: TraceRecorder | None = None,
    ) -> Answer:
        """Answer from the documents this principal may read.

        `trace`, when given, records every stage for the troubleshooting page. It observes only: the answer is the
        same with or without it, and the near-miss probe it may run is never reflected in the answer.
        """
        t0 = time.perf_counter()
        question = (question or "").strip()
        if not question:
            raise ValidationFailed("question is empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValidationFailed(f"question longer than {MAX_QUESTION_CHARS} characters")
        usage = TokenUsage()
        timings: dict[str, float] = {}

        decision = self._decide(principal)
        facet = self.facet_filter(filters)
        if trace is not None:
            self._trace_identity(trace, principal, decision)
        if decision.deny_all:
            if trace is not None:
                trace.set("access", StageStatus.FAIL, "No access: the account lacks a required attribute",
                          deny_all=True, attributes_used=list(decision.attributes_used))
                await self._near_miss(trace, principal, question, self.expander.expand(question),
                                      facet or "", None)
            return Answer(answer=NO_ACCESS_MESSAGE, refused=True, refusal_reason="no_access", usage=usage,
                          correlation_id=correlation_id, timings_ms={"total": (time.perf_counter() - t0) * 1000})
        odata = self.combine(CURRENT_FILTER, decision.odata, facet)
        if trace is not None:
            trace.set("access", StageStatus.OK,
                      "Administrator: no access filter" if decision.bypass
                      else f"Filter built from {', '.join(decision.attributes_used) or 'no attributes'}",
                      bypass=decision.bypass, access_filter=decision.odata, facet_filter=facet,
                      combined_filter=odata, attributes_used=list(decision.attributes_used))

        turns = list(history)[-self.history_turns:]
        search_q = question
        tq = time.perf_counter()
        if turns:
            tc = time.perf_counter()
            res = await self.utility.complete(
                system=CONDENSE_SYSTEM,
                messages=[LlmMessage("user", build_condense_prompt([(t.role, t.content) for t in turns], question))],
                max_tokens=300, purpose="condense")
            usage.add(res.usage, "condense")
            if res.text.strip() and not res.refused:
                search_q = res.text.strip()[:MAX_QUESTION_CHARS]
            timings["condense"] = (time.perf_counter() - tc) * 1000
        keyword_q = self.expander.expand(search_q)
        if trace is not None:
            trace.set("query", StageStatus.OK,
                      "Rewritten from the conversation" if search_q != question else "Question used as asked",
                      duration_ms=(time.perf_counter() - tq) * 1000, search_query=search_q,
                      keyword_query=keyword_q, condensed=search_q != question, history_turns=len(turns))

        try:
            retrieval = await self.retriever.retrieve(query=search_q, keyword_query=keyword_q,
                                                      odata_filter=odata, top=self.top_k)
        except Exception as e:
            if trace is not None:
                trace.current = _failing_retrieval_stage(e)
            raise
        usage.add(retrieval.usage, "embed_query")
        timings.update(retrieval.timings_ms)
        hits, also_at = _collapse_duplicates(retrieval.hits)
        if trace is not None:
            self._trace_retrieval(trace, retrieval, hits, odata, keyword_q)
        if not hits:
            timings["total"] = (time.perf_counter() - t0) * 1000
            if trace is not None:
                await self._near_miss(trace, principal, search_q, keyword_q, facet or "",
                                      retrieval.vector)
            return Answer(answer=NOT_FOUND_MESSAGE, refused=True, refusal_reason="no_relevant_context", usage=usage,
                          timings_ms=timings, correlation_id=correlation_id)

        messages = [LlmMessage(t.role, t.content[:4000]) for t in turns if t.role in ("user", "assistant")]
        user_prompt = build_user_prompt(question, build_context(hits))
        messages.append(LlmMessage("user", user_prompt))
        if trace is not None:
            trace.set("prompt", StageStatus.OK, f"{len(hits)} passages, {len(user_prompt):,} characters",
                      blocks=len(hits), chars=len(user_prompt), history_messages=len(messages) - 1,
                      passages=trace.hits(hits))
            trace.current = "llm"
        tl = time.perf_counter()
        result = await self.llm.complete(system=ANSWER_SYSTEM, messages=messages,
                                         max_tokens=self.max_output_tokens, purpose="answer")
        usage.add(result.usage, "answer")
        first_refused = result.refused
        if result.refused and self.fallback is not None:
            log.warning("answer model refused; using fallback provider", extra={"provider": result.provider})
            result = await self.fallback.complete(system=ANSWER_SYSTEM, messages=messages,
                                                  max_tokens=self.max_output_tokens, purpose="answer_fallback")
            usage.add(result.usage, "answer_fallback")
        timings["llm"] = (time.perf_counter() - tl) * 1000

        idx = cited_indexes(result.text, len(hits))
        citations = [
            Citation(index=i, doc_id=hits[i - 1].doc_id, chunk_id=hits[i - 1].chunk_id, title=hits[i - 1].title,
                     path=hits[i - 1].path, page=hits[i - 1].page, heading=hits[i - 1].heading,
                     score=hits[i - 1].reranker_score or hits[i - 1].score, snippet=hits[i - 1].content[:300],
                     also_at=also_at.get(hits[i - 1].chunk_id, []))
            for i in idx
        ]
        answer_text = result.text.strip()
        refused = result.refused
        reason = "model_refusal" if refused else None
        if not citations and not refused:
            # Grounding guard: an answer without citations is not trusted.
            refused = True
            reason = "uncited"
            if not any(h in answer_text.lower() for h in _NOT_FOUND_HINTS):
                answer_text = NOT_FOUND_MESSAGE
        timings["total"] = (time.perf_counter() - t0) * 1000
        if trace is not None:
            trace.current = None
            trace.set("llm", StageStatus.FAIL if result.refused else StageStatus.OK,
                      f"{result.provider} {result.model}".strip() + (" declined" if result.refused else ""),
                      duration_ms=timings["llm"], provider=result.provider, model=result.model,
                      stop_reason=result.stop_reason, refused=result.refused,
                      fallback_used=first_refused and self.fallback is not None,
                      output_chars=len(result.text), usage=usage.by_purpose.get("answer", {}))
            trace.set("grounding",
                      StageStatus.FAIL if reason in ("uncited", "model_refusal") else StageStatus.OK,
                      f"{len(citations)} citation(s)" if citations else "No citations - answer withheld",
                      guard=reason, cited=idx,
                      citations=[{"index": c.index, "doc_id": c.doc_id, "title": c.title, "path": c.path,
                                  "page": c.page, "score": c.score} for c in citations])
        return Answer(answer=answer_text, refused=refused, refusal_reason=reason, citations=citations, usage=usage,
                      provider=result.provider, model=result.model, timings_ms=timings,
                      correlation_id=correlation_id)

    # ------------------------------------------------------------------ tracing helpers

    def _trace_identity(self, trace: TraceRecorder, principal: Principal, decision: AccessDecision) -> None:
        account = self.engine.account(principal)
        problems = self.engine.caller_problems(principal)
        trace.trace.caller_problems = problems
        status = StageStatus.FAIL if decision.deny_all else StageStatus.WARN if problems else StageStatus.OK
        trace.set("identity", status,
                  "Administrator - reads everything" if decision.bypass else str(account["summary"]),
                  subject=principal.subject, display_name=principal.display_name, issuer=principal.issuer_kind,
                  roles=sorted(principal.roles), claimed_roles=list(principal.claimed_roles),
                  attributes=account["attributes"], problems=problems, bypass=decision.bypass)

    def _trace_retrieval(self, trace: TraceRecorder, retrieval: RetrievalResult, hits: list[SearchHit],
                         odata: str, keyword_q: str) -> None:
        trace.set("embed", StageStatus.OK, f"{len(retrieval.vector or [])} dimensions",
                  duration_ms=retrieval.timings_ms.get("embed_query"), dimensions=len(retrieval.vector or []),
                  tokens=retrieval.usage.embedding)
        found = len(retrieval.hits) + len(retrieval.dropped)
        trace.set("search", StageStatus.OK if found else StageStatus.WARN,
                  f"{found} passage(s) found inside the access filter",
                  duration_ms=retrieval.timings_ms.get("search"), filter=odata, keyword_query=keyword_q,
                  top_k=self.top_k,
                  hits=trace.hits(retrieval.hits, kept=True) + trace.hits(retrieval.dropped, kept=False))
        best = max(retrieval.dropped, key=lambda h: (h.reranker_score or 0.0, h.score)) if retrieval.dropped else None
        trace.set("relevance",
                  StageStatus.WARN if retrieval.dropped and not hits else StageStatus.OK,
                  f"{len(hits)} kept, {len(retrieval.dropped)} below the bar, "
                  f"{len(retrieval.hits) - len(hits)} duplicate(s) merged",
                  thresholds=retrieval.thresholds, kept=len(hits), dropped=trace.hits(retrieval.dropped),
                  duplicates=len(retrieval.hits) - len(hits), best_dropped=hit_row(best) if best else None)

    async def _near_miss(self, trace: TraceRecorder, principal: Principal, text: str, keyword_q: str,
                         base_filter: str, vector: list[float] | None) -> None:
        """Run the probe for a refused question. A probe failure never affects the answer.

        `base_filter` is the caller's facet filter only - no access clause and no is_current, so a relevant chunk
        that is wrongly marked not-current is found and reported instead of silently missing from both searches.
        """
        nm = trace.trace.near_miss
        if trace.bypass:
            nm.skipped_reason = "not needed: an administrator's search is already unfiltered"
            return
        if self.probe is None:
            nm.skipped_reason = "disabled (QUERY_TRACE_NEAR_MISS=false)"
            return
        try:
            trace.trace.near_miss = await self.probe.run(principal, text=text, keyword_query=keyword_q,
                                                         base_filter=base_filter, vector=vector)
        except Exception as e:  # diagnostics must never turn a refusal into an error
            log.warning("near-miss probe failed", extra={"error": f"{type(e).__name__}: {e}"})
            nm.skipped_reason = f"failed: {type(e).__name__}: {e}"[:300]

    def _vocabulary(self, fd: FacetDef) -> dict[str, object]:
        """The facet as CONFIGURED, independent of what is indexed.

        `values` below is an aggregation, so it is empty for a facet no visible document carries - which is
        exactly the state an upload picker has to offer choices in. The two answer different questions and
        both are needed: counts for filtering what exists, vocabulary for tagging what does not yet.
        """
        return {
            "label": fd.label or fd.name,
            "closed": fd.closed,
            "hierarchical": fd.hierarchical,
            "multi": fd.multi,
            "vocabulary": [{"id": v.id, "label": v.label or v.id, "parent": v.parent} for v in fd.values],
        }

    async def facet_counts(self, principal: Principal, index: object) -> dict[str, dict[str, object]]:
        access, deny_all = self.access_filter(principal)
        out: dict[str, dict[str, object]] = {}
        if deny_all:
            return {f.name: {**self._vocabulary(f), "values": []} for f in self.facets.facets}
        counts = await index.facets(self.combine(CURRENT_FILTER, access), list(self.facet_fields.values()))  # type: ignore[attr-defined]
        for fd in self.facets.facets:
            c = counts.get(fd.field, {})
            labels = {v.id: v.label or v.id for v in fd.values}
            out[fd.name] = {
                **self._vocabulary(fd),
                "values": [{"id": k, "label": labels.get(k, k), "count": n}
                           for k, n in sorted(c.items(), key=lambda kv: -kv[1])],
            }
        return out


def _failing_retrieval_stage(e: BaseException) -> str:
    """Retrieval embeds and then searches inside one call; the error says which half failed."""
    text = f"{type(e).__module__} {e}".lower()
    return "search" if ("search" in text or "index" in text) else "embed"
