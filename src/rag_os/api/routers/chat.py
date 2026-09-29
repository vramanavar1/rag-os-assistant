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


@router.get("/me/account", summary="Account Information: who the caller is and what it lets them read")
async def me_account(principal: Principal = Depends(get_principal),
                     c: Container = Depends(get_container)) -> dict[str, Any]:
    """Everything the Account Information panel shows. No role required - it describes only the caller.

    Roles are reported as the APPLICATION ROLES defined on the app registration, because that is the unit an
    administrator actually assigns. `held` comes from the token's own roles claim rather than from the mapped
    roles, since the mapping is many-to-many and cannot be inverted: rag.admin grants both admin and
    contributor, so deriving it backwards would claim an assignment nobody made.
    """
    policy = c.domain.policy
    claimed = set(principal.claimed_roles)
    app_roles = [
        {
            "value": r.value,
            "display_name": r.display_name or r.value,
            "description": r.description,
            "held": r.value in claimed,
            # What holding it means inside RAG-OS, from the same map the authoriser uses.
            "grants": sorted(name for name, accepted in policy.roles.items() if r.value in accepted),
        }
        for r in policy.app_roles
    ]
    return {
        "subject": principal.subject,
        "display_name": principal.display_name,
        "issuer_kind": principal.issuer_kind,
        "roles": sorted(principal.roles),
        "app_roles": app_roles,
        # Values the token carries that match no defined app role - a misspelled assignment, or one left over
        # from another application. Stated rather than silently dropped, which is what used to happen.
        "unrecognised_roles": sorted(claimed - {r.value for r in policy.app_roles}),
        **c.engine.account(principal),
    }
