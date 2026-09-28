"""Bearer-token validation with a trusted-issuer table.

* ``entra`` - Microsoft Entra ID access tokens (RS256, JWKS discovery). The only production issuer.
            One tenant, but TWO legitimate spellings of `iss` and `aud`: see IssuerConfig.
* ``dev``   - local demo tokens minted by /api/dev/token (DEV_AUTH_ENABLED only), lifetime capped.

The algorithm is pinned per issuer (no alg=none, no HS/RS confusion); iss/aud/exp/nbf are required.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient

from rag_os.domain.errors import AuthenticationFailed

log = logging.getLogger(__name__)
LEEWAY_S = 30


@dataclass(frozen=True)
class IssuerConfig:
    """One trusted issuer. ``issuer`` and ``audience`` each take a single string or an allow-list of them.

    Entra needs the allow-list, for one tenant. The app registration's ``api.requestedAccessTokenVersion``
    decides the format of every access token issued for that API - and its default of ``null`` means 1:

        version 1   iss = https://sts.windows.net/<tid>/    aud = the api:// resource URI that was requested
        version 2   iss = .../<tid>/v2.0                    aud = the API's bare client-id GUID

    Nothing in this process chooses that; it is a directory setting. Accepting both spellings is therefore the
    only way a correct deployment cannot be broken by a manifest value, and it is not a widening: both carry the
    same <tid> and name the same application. Matching stays exact - an allow-list of two, never a prefix.
    """

    kind: str
    issuer: str | tuple[str, ...]
    audience: str | tuple[str, ...]
    algorithms: tuple[str, ...]
    key: str | None = None  # HS secret
    jwks_url: str | None = None
    max_lifetime_s: int | None = None
    required: tuple[str, ...] = ("exp", "iat", "iss", "aud", "sub")
    issuers: tuple[str, ...] = field(init=False, default=(), compare=False)    # normalised `issuer`
    audiences: tuple[str, ...] = field(init=False, default=(), compare=False)  # normalised `audience`

    def __post_init__(self) -> None:
        # The isinstance check is the point, not defensiveness: a str is iterable, so tuple("rag-os-dev") would
        # quietly become ('r', 'a', 'g', ...) and that issuer would then match nothing at all.
        for name, value in (("issuers", self.issuer), ("audiences", self.audience)):
            normalised = (value,) if isinstance(value, str) else tuple(value)
            if not normalised or not all(normalised):
                raise ValueError(f"issuer '{self.kind}': {name} needs at least one non-empty value")
            object.__setattr__(self, name, normalised)


class JwtValidator:
    def __init__(self, issuers: list[IssuerConfig]) -> None:
        self._issuers = list(issuers)
        self._by_iss: dict[str, IssuerConfig] = {}
        for cfg in issuers:
            for iss in cfg.issuers:
                clash = self._by_iss.get(iss)
                if clash is not None and clash.kind != cfg.kind:
                    raise ValueError(f"issuer url '{iss}' is claimed by both '{clash.kind}' and '{cfg.kind}'")
                self._by_iss[iss] = cfg
        self._jwks: dict[str, PyJWKClient] = {}

    @property
    def issuer_kinds(self) -> list[str]:
        # From the configs, not from _by_iss.values(): one config is now indexed under several urls.
        return sorted({i.kind for i in self._issuers})

    def validate(self, token: str) -> tuple[dict[str, Any], str]:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthenticationFailed("malformed token") from e
        token_iss = str(unverified.get("iss"))
        cfg = self._by_iss.get(token_iss)
        if cfg is None:
            # The exposed message is unchanged; the values go to the log. "untrusted issuer" with nothing
            # recorded is why a token-version mismatch costs a deploy-and-fail cycle to identify.
            log.warning("token rejected: issuer not in the allow-list", extra={
                "token_iss": token_iss, "token_ver": str(unverified.get("ver") or "1.0 (no ver claim)"),
                "trusted_iss": sorted(self._by_iss)})
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
                token, key=key, algorithms=list(cfg.algorithms),
                # audience= takes a list and passes when ANY member matches; a list with no match still raises
                # InvalidAudienceError, so this is an allow-list and not a way round the check. issuer= takes one
                # string, so the issuer check stays a single exact comparison - and token_iss is the key the
                # allow-list was just matched on, read from the same token bytes being verified here.
                audience=list(cfg.audiences), issuer=token_iss,
                leeway=LEEWAY_S, options={"require": list(cfg.required)},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthenticationFailed("token expired") from e
        except jwt.InvalidAudienceError as e:
            # Called out separately because PyJWT's own text is just "Audience doesn't match" - no seen, no
            # expected - and an audience mismatch here almost always means the directory's token version and
            # ENTRA_AUDIENCE disagree rather than that the caller did anything wrong.
            log.warning("token rejected: audience mismatch", extra={
                "issuer_kind": cfg.kind, "token_aud": str(unverified.get("aud")),
                "token_ver": str(unverified.get("ver") or "1.0 (no ver claim)"),
                "accepted_aud": list(cfg.audiences)})
            raise AuthenticationFailed("token audience not accepted") from e
        except jwt.PyJWTError as e:
            log.warning("token rejected", extra={"issuer_kind": cfg.kind, "reason": type(e).__name__,
                                                 "error": str(e), "token_iss": token_iss})
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
