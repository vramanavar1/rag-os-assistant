"""End-user endpoints: chat, facets, identity."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends

from rag_os.api.deps import get_container, get_principal
from rag_os.api.schemas import ChatRequest, MeResponse
from rag_os.application.services.prompts import GUARD_REFUSALS
from rag_os.composition import Container
from rag_os.domain.access import Principal
from rag_os.domain.answers import Answer
from rag_os.infrastructure.telemetry import correlation_id_var, record_tokens, span

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=Answer, summary="Ask a question (answers only from documents you can access)")
async def chat(req: ChatRequest, principal: Principal = Depends(get_principal),
               c: Container = Depends(get_container)) -> Answer:
    # Refuse, politely, when there is genuinely nothing to answer from - an index that was never bootstrapped,
    # or one built with a different embedding model. Previously neither was checked here: a missing index
    # surfaced as DependencyUnavailable("search query failed") and a mismatch was not detected at all, so
    # queries ran against the wrong vector space and returned plausible but wrong answers.
    # An index that exists but is empty is NOT refused here - the guard is satisfied, the query runs, and
    # AnswerQuery answers with NOT_FOUND_MESSAGE. That path is unchanged.
    try:
        guard_reason = await asyncio.wait_for(c.guard.refusal_reason(c.index, {"query": c.embed_query}), timeout=20)
    except Exception as e:  # the guard itself is a dependency; never let it turn a question into a 500
        log.warning("profile guard unavailable; refusing the query", extra={"error": f"{type(e).__name__}: {e}"})
        guard_reason = "search_unavailable"
    if guard_reason:
        return Answer(answer=GUARD_REFUSALS[guard_reason], refused=True, refusal_reason=guard_reason,
                      correlation_id=correlation_id_var.get())
    with span("rag.chat", issuer=principal.issuer_kind):
        answer = await c.answer.ask(principal, req.question, req.history, req.filters,
                                    correlation_id=correlation_id_var.get())
    record_tokens(answer.usage, answer.provider or c.settings.llm_answer, answer.model or "", "answer")
    return answer


@router.get("/facets", summary="Facet values and counts visible to the caller")
async def facets(principal: Principal = Depends(get_principal), c: Container = Depends(get_container)) -> dict[str, Any]:
    return {"facets": await c.answer.facet_counts(principal, c.index)}


@router.get("/me", response_model=MeResponse, summary="The caller's identity as seen by the access policy")
async def me(principal: Principal = Depends(get_principal)) -> MeResponse:
    return MeResponse(subject=principal.subject, issuer_kind=principal.issuer_kind,
                      display_name=principal.display_name, attributes=principal.attributes,
                      roles=sorted(principal.roles))
