"""ClaimsMapper: validated JWT claims -> Principal, driven entirely by the access policy.

Which claim feeds which attribute is configured per issuer kind (entra / dev). A rule's ``value_map``
translates raw claim values into attribute values, so an Entra group object id can stand for "HR" without that
id ever appearing on a document. Roles are only honoured from issuers trusted for roles, so a local dev token
cannot make someone an admin in a deployment that does not trust the dev issuer.
"""

from __future__ import annotations

from typing import Any

from rag_os.domain.access import AccessPolicy, Principal
from rag_os.domain.errors import AuthenticationFailed


class ClaimsMapper:
    def __init__(self, policy: AccessPolicy) -> None:
        self.policy = policy

    def map(self, claims: dict[str, Any], issuer_kind: str) -> Principal:
        attrs: dict[str, list[str] | int] = {}
        for rule in self.policy.attributes:
            claim = rule.claims.get(issuer_kind)
            if not claim or claim not in claims:
                continue
            raw = claims[claim]
            if raw is None:
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
