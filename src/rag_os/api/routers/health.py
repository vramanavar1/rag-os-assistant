"""Liveness / readiness probes and public client configuration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from rag_os.api.deps import get_container
from rag_os.api.schemas import PublicConfigResponse
from rag_os.composition import Container

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])

# The database check has to outlast the connect attempt underneath it, or it can only ever report TimeoutError.
# db.py allows 10s to open a PostgreSQL connection (_PG_CONNECT_TIMEOUT_S), so a 5s budget here guaranteed the
# least informative answer for every slow-but-working database: asyncio.wait_for cancels the await but cannot
# cancel the worker thread, so the real error arrived after nobody was listening for it.
_DB_TIMEOUT_S = 12
_GUARD_TIMEOUT_S = 20
# Worst case is therefore 32s, sequential. Every client that polls /api/readyz must allow more than that -
# test_every_readyz_client_timeout_exceeds_readyz_own_budget enforces it, because the relationship spans two
# languages and three files and is invisible from any one of them.


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (DB, embedding profile guard, index)")
async def readyz(c: Container = Depends(get_container), fresh: bool = False) -> JSONResponse:
    """Why a dependency is unusable. Not a probe - no container probe points here, deliberately.

    `?fresh=1` re-checks Search and the embedder instead of accepting the guard's cached verdict, which is up to
    `recheck_seconds` (60s) old. The default stays cached because step 08 polls this every 15 seconds and this
    route is reachable unauthenticated through the chat UI; a stale answer is fine for waiting, but it is
    confusing when someone is re-running this by hand to watch a dependency come back.

    The body deliberately names exception TYPES and not their messages. An Azure or SQLAlchemy error string
    carries endpoints, usernames and identity details, and this response is public. The full exception goes to
    the log, which is not.
    """
    checks: dict[str, Any] = {}
    ok = True
    try:
        def _ping() -> None:
            with c.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))

        await asyncio.wait_for(asyncio.to_thread(_ping), timeout=_DB_TIMEOUT_S)
        checks["state_db"] = "ok"
    except Exception as e:
        ok = False
        log.error("readyz: state database check failed", exc_info=True)
        checks["state_db"] = f"error: {type(e).__name__}"
    try:
        # Both pools, because the index is only trustworthy if the vectors in it and the vectors a query is
        # embedded into came from the same model. The ingestion pool is advisory: it scales to zero when idle,
        # so "not running" is its resting state - but if it ANSWERS and reports a different model, that is a
        # failure, because those vectors would land in a different space to the queries searching them.
        st = await asyncio.wait_for(
            c.guard.check(c.index, {"query": c.embed_query, "ingest": c.embed_ingest}, force=fresh,
                          advisory_pools=frozenset({"ingest"})),
            timeout=_GUARD_TIMEOUT_S)
        checks["embedding_profile"] = {"ok": st.ok, "fingerprint": c.guard.fp, "index": c.index_name,
                                       "reasons": st.reasons, "notes": st.notes}
        ok = ok and st.ok
    except Exception as e:
        ok = False
        log.error("readyz: embedding profile guard check failed", exc_info=True)
        # The guard now reports a Search failure as a reason of its own, so reaching here means the guard call
        # itself failed or timed out. Name the index anyway - without it the caller cannot tell which index the
        # verdict was about.
        checks["embedding_profile"] = {"ok": False, "fingerprint": c.guard.fp, "index": c.index_name,
                                       "reasons": [f"{type(e).__name__}"]}
    checks["llm_answer"] = c.settings.llm_answer
    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)


@router.get("/public-config", response_model=PublicConfigResponse, summary="Non-secret settings for the chat UI")
async def public_config(c: Container = Depends(get_container)) -> PublicConfigResponse:
    s = c.settings
    return PublicConfigResponse(
        app_name="RAG-OS Knowledge Assistant",
        auth_mode=s.auth_mode,
        embed_origins=s.embed_origin_list,
        dev_auth_enabled=s.dev_auth_enabled,
        entra_tenant_id=s.entra_tenant_id,
        entra_client_id=s.entra_client_id,
        entra_api_scope=s.entra_api_scope,
    )
