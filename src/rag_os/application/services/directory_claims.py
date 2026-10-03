"""Attribute claims the token did not carry, read from the directory instead.

Entra does not emit directory extension claims for a Microsoft-account user. Microsoft's optional-claims
reference states it plainly: "If your application manifest requests a custom extension and an MSA user logs in to
your app, these extensions aren't returned." The values are really there - written on the user object in the
resource tenant, readable through Graph, visible on the admin page that set them - but the token service omits
them, so every such caller arrives with no department and no region and, since both are required, reads nothing.
Nothing in the deployment is misconfigured and no error is raised anywhere.

This fills that gap, and only that gap:

* A token that already carries the claim is used exactly as before. No Graph call, no added latency, and the
  signed value always wins - a directory read never overrides what the issuer asserted.
* A token missing them is enriched from the directory, keyed on the `oid` claim of that same signed token. The
  identity still comes from the issuer; only the attributes come from Graph.
* The values are injected into the CLAIMS, under the claim name the policy reads, rather than onto the principal.
  ClaimsMapper then applies value_map, drop_unmapped and the numeric coercion to them exactly as it would to a
  real claim. Building a Principal here instead would be a second implementation of those rules, and the one that
  disagreed would be whichever was read less often.

A failure to reach the directory leaves the claims untouched, which fails closed: the caller keeps whatever the
token carried, which for the accounts this exists for is nothing at all.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from rag_os.application.ports import DirectoryAdmin
from rag_os.application.services.claims import claim_is_present
from rag_os.domain.access import AccessPolicy

log = logging.getLogger(__name__)

_EXTN = "extn."
_DEFAULT_TTL_S = 300.0
_MAX_ENTRIES = 2048


class DirectoryAttributes:
    """Reads a caller's attributes from the directory when their token did not carry them."""

    def __init__(
        self,
        directory: DirectoryAdmin,
        policy: AccessPolicy,
        *,
        ttl_s: float = _DEFAULT_TTL_S,
        max_entries: int = _MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._directory = directory
        self._policy = policy
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}

    def _extension_claims(self) -> dict[str, str]:
        """Policy attribute name -> the Entra claim it reads, for attributes backed by a directory extension.

        Only these can be filled in from a user object. An attribute read from `groups` through a value_map has
        no extension behind it, and one read from `oid` is already in every token.
        """
        out = {}
        for rule in self._policy.attributes:
            claim = rule.claims.get("entra", "")
            if claim.startswith(_EXTN):
                out[rule.name] = claim
        return out

    def _cached(self, object_id: str) -> dict[str, str] | None:
        hit = self._cache.get(object_id)
        if hit is None:
            return None
        expires_at, attributes = hit
        if expires_at <= self._clock():
            self._cache.pop(object_id, None)
            return None
        return attributes

    def _store(self, object_id: str, attributes: dict[str, str]) -> None:
        if len(self._cache) >= self._max_entries:
            # Evict the entry closest to expiry rather than clearing everything: a full flush under load would
            # send every active caller back to Graph at once.
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
        self._cache[object_id] = (self._clock() + self._ttl_s, attributes)

    async def enrich(self, claims: dict[str, Any], issuer_kind: str) -> dict[str, Any]:
        """`claims` with any missing extension-backed attribute claims filled in from the directory."""
        if issuer_kind != "entra":
            return claims
        wanted = {
            name: claim for name, claim in self._extension_claims().items()
            if not claim_is_present(claims, claim)
        }
        if not wanted:
            return claims  # the common path: a work account whose token carries them. Nothing is read.
        object_id = str(claims.get("oid") or "")
        if not object_id:
            return claims
        attributes = self._cached(object_id)
        if attributes is None:
            try:
                attributes = dict((await self._directory.read_user(object_id)).attributes)
            except Exception:
                # Never turn a directory outage into a failed sign-in. The caller keeps the token's own claims,
                # so this can only ever grant less access than a successful read, never more.
                log.warning("could not read directory attributes for the caller", exc_info=True)
                return claims
            self._store(object_id, attributes)
        filled = dict(claims)
        for name, claim in wanted.items():
            value = attributes.get(name)
            if value is not None:
                filled[claim] = value
        return filled
