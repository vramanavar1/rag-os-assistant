"""Settings (Security): assigning a person's access attributes and application roles in the identity provider.

Thin on purpose. Every rule about what may be written, to whom and by whom lives in
application/services/directory_admin.py, which is the only layer mypy checks - a request handler is the wrong
place for a security boundary.

The write is a PUT of the desired state guarded by If-Match, matching how configuration documents are written
here (see admin_config.py). It needs no DELETE or PATCH, so the CORS allow-list in api/app.py and the browser's
API client stay as they are.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from rag_os.api.deps import get_container, require_role
from rag_os.api.schemas import DirectoryWriteRequest
from rag_os.application.services.directory_admin import DirectoryWrite
from rag_os.composition import Container
from rag_os.domain.access import Principal

router = APIRouter(prefix="/api/admin", tags=["admin: security"])
admin = require_role("admin")


@router.get("/directory", summary="What this deployment can assign: master lists, roles and caveats")
async def directory_capability(
    _: Principal = Depends(admin), c: Container = Depends(get_container)
) -> dict[str, Any]:
    """The values an administrator may assign, and the application roles they may grant.

    Answered even when no directory is configured, with `enabled: false`, so the page can explain what to switch
    on instead of showing a bare error.
    """
    return c.directory_admin.capability()


@router.get("/directory/users/{email}", summary="One person's current attributes and role assignments")
async def read_directory_user(
    email: str, _: Principal = Depends(admin), c: Container = Depends(get_container)
) -> JSONResponse:
    state = await c.directory_admin.describe(email)
    # The ETag covers the attributes AND the role assignments, so a page loaded before somebody else's change
    # cannot write its stale idea of them back.
    return JSONResponse(state, headers={"ETag": f'"{state["etag"]}"'})


@router.put("/directory/users/{email}", summary="Assign attributes and roles (desired state, idempotent)")
async def write_directory_user(
    email: str,
    body: DirectoryWriteRequest,
    if_match: str | None = Header(default=None),
    principal: Principal = Depends(admin),
    c: Container = Depends(get_container),
) -> JSONResponse:
    """Apply the desired state: revokes first, then attributes, then grants, aborting on the first failure.

    A partial write returns 200 with `ok: false` and the list of what already landed. That is deliberate and
    unlike the rest of this API: an error response carries a message and nothing else, and the one thing an
    administrator needs after a half-applied write is which steps reached the directory.
    """
    result = await c.directory_admin.apply(
        principal,
        email,
        DirectoryWrite(
            attributes=dict(body.attributes),
            roles=list(body.roles) if body.roles is not None else None,
            revoke_sessions=body.revoke_sessions,
            confirm=body.confirm,
        ),
        if_match or "",
    )
    payload = {"ok": result.ok, "applied": result.applied, "failed": result.failed, **result.state}
    return JSONResponse(payload, headers={"ETag": f'"{result.etag}"'})
