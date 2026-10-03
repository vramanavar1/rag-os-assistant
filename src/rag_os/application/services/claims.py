"""ClaimsMapper: validated JWT claims -> Principal, driven entirely by the access policy.

Which claim feeds which attribute is configured per issuer kind (entra / dev). A rule's ``value_map``
translates raw claim values into attribute values, so an Entra group object id can stand for "HR" without that
id ever appearing on a document. Roles are only honoured from issuers trusted for roles, so a local dev token
cannot make someone an admin in a deployment that does not trust the dev issuer.
"""

from __future__ import annotations

import re
from typing import Any

from rag_os.domain.access import AccessPolicy, Principal
from rag_os.domain.errors import AuthenticationFailed

_MISSING = object()
# A directory extension is REGISTERED as extension_{appId-without-hyphens}_{name}, and Microsoft's
# optional-claims page says the JWT carries it as extn.{name} - then shows extension_{appid}_{name} in its own
# worked example a few paragraphs later. The page contradicts itself, and the long form embeds the tenant's own
# application id, so it can never be a checked-in default.
#
# Rather than make every operator guess, a configured `extn.<name>` also matches the long form. The symptom
# this avoids is the worst kind: everything configured correctly, no error anywhere, and no documents.
_EXTN_PREFIX = "extn."
_LONG_FORM = re.compile(r"^extension_[0-9a-fA-F]{32}_(?P<name>.+)$")


def _claim_value(claims: dict[str, Any], name: str) -> Any:
    """The claim's value, or _MISSING. Exact match wins; `extn.x` also accepts extension_<appid>_x."""
    if name in claims:
        return claims[name]
    if not name.startswith(_EXTN_PREFIX):
        return _MISSING
    suffix = name[len(_EXTN_PREFIX):].casefold()
    for key, value in claims.items():
        m = _LONG_FORM.match(key)
        # Case-insensitive on the suffix: Entra treats extension names as case-sensitive when reading them,
        # so a tenant that registered `Clearance` would otherwise never match a config saying `clearance`.
        if m and m.group("name").casefold() == suffix:
            return value
    return _MISSING


def claim_is_present(claims: dict[str, Any], name: str) -> bool:
    """Whether the token actually carries this claim, in either spelling.

    Shared with the directory fallback so "missing" means one thing. Deciding presence with `name in claims`
    there would re-read from Graph for every caller whose token carried the LONG form, which is a Graph call per
    request for users who never needed one.
    """
    return _claim_value(claims, name) is not _MISSING


class ClaimsMapper:
    def __init__(self, policy: AccessPolicy) -> None:
        self.policy = policy

    def map(self, claims: dict[str, Any], issuer_kind: str) -> Principal:
        attrs: dict[str, list[str] | int] = {}
        for rule in self.policy.attributes:
            claim = rule.claims.get(issuer_kind)
            if not claim:
                continue
            raw = _claim_value(claims, claim)
            if raw is _MISSING or raw is None:
                continue
            if rule.is_numeric:
                if isinstance(raw, list):
                    raw = raw[0] if raw else None
                if raw is None:
                    continue
                mapped = rule.map_value(str(raw))
                if mapped is None:
                    continue
                try:
                    attrs[rule.name] = int(mapped)
                except (TypeError, ValueError):
                    # e.g. an IdP that sends clearance: "Internal" without a value_map for it.
                    raise AuthenticationFailed(f"attribute '{rule.name}' must be an integer") from None
            else:
                vals = raw if isinstance(raw, list) else [raw]
                mapped_vals: list[str] = []
                for v in vals:
                    s = str(v)
                    if not s.strip():
                        continue
                    m = rule.map_value(s)
                    if m is not None:
                        mapped_vals.append(m)
                attrs[rule.name] = mapped_vals
        roles: set[str] = set()
        claimed: set[str] = set()
        rs = self.policy.role_sources
        if issuer_kind in rs.trusted_for_roles:
            claim_name = rs.role_claim.get(issuer_kind, "roles")
            raw_roles = claims.get(claim_name) or []
            raw_set = {str(r) for r in (raw_roles if isinstance(raw_roles, list) else [raw_roles])}
            # Kept, not just matched and dropped. The mapping below is many-to-many, so it cannot be inverted
            # afterwards to answer "which application role was I actually assigned?" - and a value that
            # matches nothing (a typo'd assignment) would otherwise be indistinguishable from no assignment.
            claimed = raw_set
            for app_role, accepted in self.policy.roles.items():
                if raw_set & set(accepted):
                    roles.add(app_role)
        return Principal(
            subject=str(claims.get("sub") or claims.get("oid") or "unknown"),
            issuer_kind=issuer_kind,
            display_name=str(claims.get("name") or claims.get("preferred_username") or claims.get("sub") or ""),
            attributes=attrs,
            roles=roles,
            claimed_roles=sorted(claimed),
            raw_claims=claims,
        )
