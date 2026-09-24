"""AnswerQuery: permission-aware, grounded answering with citations and per-query token accounting."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence

from rag_os.application.ports import LlmMessage, LlmProvider, Retriever
from rag_os.application.services.access_policy import AccessPolicyEngine, odata_quote
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
from rag_os.domain.access import Principal
from rag_os.domain.answers import Answer, ChatTurn, Citation, TokenUsage
from rag_os.domain.classification import FacetSchema
from rag_os.domain.errors import ValidationFailed

log = logging.getLogger(__name__)
audit = logging.getLogger("rag_os.audit")

MAX_QUESTION_CHARS = 2000
_FILTER_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.@/&-]{0,127}$")
_NOT_FOUND_HINTS = ("could not find", "couldn't find", "not in the documents", "no information")


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

    def access_filter(self, principal: Principal) -> tuple[str | None, bool]:
        """(combined filter or None for unrestricted, deny_all)."""
        d = self.engine.decide(principal)
        if d.bypass:
            audit.info("admin access bypass", extra={"subject": principal.subject, "issuer": principal.issuer_kind})
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
    ) -> Answer:
        t0 = time.perf_counter()
        question = (question or "").strip()
        if not question:
            raise ValidationFailed("question is empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValidationFailed(f"question longer than {MAX_QUESTION_CHARS} characters")
        usage = TokenUsage()
        timings: dict[str, float] = {}

        access, deny_all = self.access_filter(principal)
        if deny_all:
            return Answer(answer=NO_ACCESS_MESSAGE, refused=True, refusal_reason="no_access", usage=usage,
                          correlation_id=correlation_id, timings_ms={"total": (time.perf_counter() - t0) * 1000})
        odata = self.combine(CURRENT_FILTER, access, self.facet_filter(filters))

        turns = list(history)[-self.history_turns:]
        search_q = question
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

        retrieval = await self.retriever.retrieve(query=search_q, keyword_query=self.expander.expand(search_q),
                                                  odata_filter=odata, top=self.top_k)
        usage.add(retrieval.usage, "embed_query")
        timings.update(retrieval.timings_ms)
        hits = retrieval.hits
        if not hits:
            timings["total"] = (time.perf_counter() - t0) * 1000
            return Answer(answer=NOT_FOUND_MESSAGE, refused=True, refusal_reason="no_relevant_context", usage=usage,
                          timings_ms=timings, correlation_id=correlation_id)

        messages = [LlmMessage(t.role, t.content[:4000]) for t in turns if t.role in ("user", "assistant")]
        messages.append(LlmMessage("user", build_user_prompt(question, build_context(hits))))
        tl = time.perf_counter()
        result = await self.llm.complete(system=ANSWER_SYSTEM, messages=messages,
                                         max_tokens=self.max_output_tokens, purpose="answer")
        usage.add(result.usage, "answer")
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
                     score=hits[i - 1].reranker_score or hits[i - 1].score, snippet=hits[i - 1].content[:300])
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
        return Answer(answer=answer_text, refused=refused, refusal_reason=reason, citations=citations, usage=usage,
                      provider=result.provider, model=result.model, timings_ms=timings,
                      correlation_id=correlation_id)

    async def facet_counts(self, principal: Principal, index: object) -> dict[str, dict[str, object]]:
        access, deny_all = self.access_filter(principal)
        out: dict[str, dict[str, object]] = {}
        if deny_all:
            return {f.name: {"label": f.label or f.name, "values": []} for f in self.facets.facets}
        counts = await index.facets(self.combine(CURRENT_FILTER, access), list(self.facet_fields.values()))  # type: ignore[attr-defined]
        for fd in self.facets.facets:
            c = counts.get(fd.field, {})
            labels = {v.id: v.label or v.id for v in fd.values}
            out[fd.name] = {
                "label": fd.label or fd.name,
                "values": [{"id": k, "label": labels.get(k, k), "count": n}
                           for k, n in sorted(c.items(), key=lambda kv: -kv[1])],
            }
        return out
