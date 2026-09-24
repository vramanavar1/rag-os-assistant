"""Development-only token minting (DEV_AUTH_ENABLED=true). Demo principals live in config/dev/principals.yaml
and carry raw claims in the same shape a real token would, so the claim->attribute mapping is exercised."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from rag_os.api.deps import get_container
from rag_os.api.schemas import DevTokenRequest
from rag_os.composition import DEV_ISSUER, Container
from rag_os.domain.errors import NotFound
from rag_os.infrastructure.auth.jwt_validator import mint_dev_token
from rag_os.infrastructure.storage.config_repo import DevPrincipals, validate_yaml

router = APIRouter(prefix="/api/dev", tags=["dev"])


def _principals(c: Container) -> DevPrincipals:
    text, _ = c.config.read_raw("dev-principals")
    return validate_yaml("dev-principals", text) if text else DevPrincipals()  # type: ignore[return-value]


@router.get("/principals", summary="Demo principals (dev only)")
async def principals(c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    out = []
    for p in _principals(c).principals:
        mapped = c.claims.map({**p.claims, "sub": p.id, "roles": p.roles}, "dev")
        out.append({"id": p.id, "display_name": p.display_name, "attributes": mapped.attributes,
                    "roles": sorted(mapped.roles)})
    return out


def _mint(c: Container, principal_id: str) -> dict[str, Any]:
    p = next((x for x in _principals(c).principals if x.id == principal_id), None)
    if p is None:
        raise NotFound("unknown dev principal")
    ttl = 3600
    token = mint_dev_token(c.settings.dev_jwt_key or "", DEV_ISSUER, c.settings.dev_jwt_audience, p.id,
                           {**p.claims, "name": p.display_name, "roles": p.roles}, ttl)
    return {"token": token, "expires_in": ttl}


@router.post("/token", summary="Mint a dev token for a demo principal")
async def token(req: DevTokenRequest, c: Container = Depends(get_container)) -> dict[str, Any]:
    return _mint(c, req.principal_id)


@router.get("/token", summary="Mint a dev token (GET variant used by the dev embed-host loader)")
async def token_get(principal_id: str, c: Container = Depends(get_container)) -> dict[str, Any]:
    return _mint(c, principal_id)
