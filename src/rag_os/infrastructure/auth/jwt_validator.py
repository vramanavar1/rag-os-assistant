"""Bearer-token validation with a trusted-issuer table.

* ``entra`` - Microsoft Entra ID access tokens (RS256, JWKS discovery). The only production issuer.
* ``dev``   - local demo tokens minted by /api/dev/token (DEV_AUTH_ENABLED only), lifetime capped.

The algorithm is pinned per issuer (no alg=none, no HS/RS confusion); iss/aud/exp/nbf are required.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

from rag_os.domain.errors import AuthenticationFailed

log = logging.getLogger(__name__)
LEEWAY_S = 30


@dataclass(frozen=True)
class IssuerConfig:
    kind: str
    issuer: str
    audience: str
    algorithms: tuple[str, ...]
    key: str | None = None  # HS secret
    jwks_url: str | None = None
    max_lifetime_s: int | None = None
    required: tuple[str, ...] = ("exp", "iat", "iss", "aud", "sub")


class JwtValidator:
    def __init__(self, issuers: list[IssuerConfig]) -> None:
        self._by_iss = {i.issuer: i for i in issuers}
        self._jwks: dict[str, PyJWKClient] = {}

    @property
    def issuer_kinds(self) -> list[str]:
        return sorted({i.kind for i in self._by_iss.values()})

    def validate(self, token: str) -> tuple[dict[str, Any], str]:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthenticationFailed("malformed token") from e
        cfg = self._by_iss.get(str(unverified.get("iss")))
        if cfg is None:
            raise AuthenticationFailed("untrusted issuer")
        alg = header.get("alg")
        if alg not in cfg.algorithms:
            raise AuthenticationFailed("algorithm not allowed for issuer")
        try:
            if cfg.jwks_url:
                client = self._jwks.setdefault(cfg.jwks_url, PyJWKClient(cfg.jwks_url, cache_keys=True, lifespan=3600))
                key: Any = client.get_signing_key_from_jwt(token).key
            else:
                if not cfg.key:
                    raise AuthenticationFailed(f"issuer '{cfg.kind}' has no key configured")
                key = cfg.key
            claims = jwt.decode(
                token, key=key, algorithms=list(cfg.algorithms), audience=cfg.audience, issuer=cfg.issuer,
                leeway=LEEWAY_S, options={"require": list(cfg.required)},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthenticationFailed("token expired") from e
        except jwt.PyJWTError as e:
            raise AuthenticationFailed(f"invalid token ({type(e).__name__})") from e
        if cfg.max_lifetime_s is not None:
            lifetime = int(claims["exp"]) - int(claims["iat"])
            if lifetime > cfg.max_lifetime_s + LEEWAY_S:
                raise AuthenticationFailed("token lifetime exceeds the allowed maximum")
        return claims, cfg.kind


def mint_dev_token(key: str, issuer: str, audience: str, subject: str, claims: dict[str, Any],
                   ttl_s: int = 3600) -> str:
    now = int(time.time())
    payload = {"iss": issuer, "aud": audience, "sub": subject, "iat": now, "nbf": now, "exp": now + ttl_s,
               "jti": uuid.uuid4().hex, **claims}
    return jwt.encode(payload, key, algorithm="HS256")
