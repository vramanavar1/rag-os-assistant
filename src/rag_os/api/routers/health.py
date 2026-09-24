"""Liveness / readiness probes and public client configuration."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from rag_os.api.deps import get_container
from rag_os.api.schemas import PublicConfigResponse
from rag_os.composition import Container

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (DB, embedding profile guard, index)")
async def readyz(c: Container = Depends(get_container)) -> JSONResponse:
    checks: dict[str, Any] = {}
    ok = True
    try:
        def _ping() -> None:
            with c.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))

        await asyncio.wait_for(asyncio.to_thread(_ping), timeout=5)
        checks["state_db"] = "ok"
    except Exception as e:
        ok = False
        checks["state_db"] = f"error: {type(e).__name__}"
    try:
        st = await asyncio.wait_for(c.guard.check(c.index, {"query": c.embed_query}), timeout=20)
        checks["embedding_profile"] = {"ok": st.ok, "fingerprint": c.guard.fp, "index": c.index_name,
                                       "reasons": st.reasons}
        ok = ok and st.ok
    except Exception as e:
        ok = False
        checks["embedding_profile"] = {"ok": False, "reasons": [f"{type(e).__name__}: {e}"]}
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
