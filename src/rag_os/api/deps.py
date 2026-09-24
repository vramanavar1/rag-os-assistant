"""FastAPI dependencies: container access, authentication and role checks."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request

from rag_os.composition import Container
from rag_os.domain.access import Principal
from rag_os.domain.errors import AccessDenied, AuthenticationFailed


def get_container(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]


def get_principal(request: Request, c: Container = Depends(get_container)) -> Principal:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationFailed("missing bearer token")
    claims, kind = c.jwt.validate(token.strip())
    principal = c.claims.map(claims, kind)
    request.state.principal = principal
    return principal


def require_role(*roles: str) -> Callable[..., Principal]:
    def dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not (principal.roles & set(roles)):
            raise AccessDenied(f"requires one of roles: {', '.join(roles)}")
        return principal

    return dep
