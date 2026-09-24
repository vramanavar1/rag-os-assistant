"""AccessPolicyEngine: one configurable policy -> (a) OData filter for Azure AI Search, (b) Python predicate.

Semantics (default-deny):

* For each attribute in ``combine.all_of`` the document must satisfy the attribute's match rule (AND).
* ``combine.grant_any_of`` attributes are an OR-override (explicit per-person/per-group shares).
  The result is ``(all_of...) or (grant) or (grant)``.
* A document with no ACL value for an attribute never satisfies it (so un-tagged docs are invisible).
* A principal missing a *required* attribute loses the whole ``all_of`` branch. That is DENY_ALL only when
  they also have no grant: an explicit share still reaches them.
* Admins (application role) bypass filtering; callers must audit-log that.

Both outputs are generated from the same code path and property-tested for agreement.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from rag_os.domain.access import AccessPolicy, AttributeRule, MatchKind, Principal
from rag_os.domain.classification import FacetSchema
from rag_os.domain.errors import AuthenticationFailed

DENY_ALL = "__deny_all__"
_DELIM = "|"


def odata_quote(value: str) -> str:
    """OData string literal escaping (single quotes doubled)."""
    return value.replace("'", "''")


@dataclass(frozen=True)
class AccessDecision:
    """Result of evaluating a principal against the policy."""

    odata: str | None  # None = unrestricted (admin); DENY_ALL = no access
    bypass: bool
    attributes_used: tuple[str, ...]

    @property
    def deny_all(self) -> bool:
        return self.odata == DENY_ALL


class AccessPolicyEngine:
    def __init__(self, policy: AccessPolicy, facets: FacetSchema | None = None) -> None:
        self.policy = policy
        self.facets = facets or FacetSchema()
        self._patterns = {a.name: re.compile(a.value_pattern) for a in policy.attributes}

    # ------------------------------------------------------------------ principal values

    def _principal_values(self, rule: AttributeRule, principal: Principal) -> list[str] | int | None:
        raw = principal.attributes.get(rule.name)
        if raw is None or raw == []:
            return None
        if rule.is_numeric:
            try:
                return int(raw) if not isinstance(raw, list) else int(raw[0])
            except (TypeError, ValueError, IndexError):
                raise AuthenticationFailed(f"attribute '{rule.name}' must be an integer") from None
        values = raw if isinstance(raw, list) else [raw]
        out: list[str] = []
        for v in values:
            s = str(v)
            if not self._patterns[rule.name].fullmatch(s) or _DELIM in s:
                # Reject rather than sanitise: a malformed claim is a broken/forged token.
                raise AuthenticationFailed(f"attribute '{rule.name}' has an invalid value")
            if s not in out:
                out.append(s)
        if rule.match == MatchKind.HIERARCHICAL:
            out = self._expand_ancestors(rule, out)
        return out

    def _expand_ancestors(self, rule: AttributeRule, values: list[str]) -> list[str]:
        expanded: list[str] = []
        fd = self.facets.get(rule.hierarchy_facet) if rule.hierarchy_facet else None
        for v in values:
            if fd is not None:
                canon = fd.normalise(v) or v
                chain = fd.ancestors(canon)
            else:
                parts = v.split("/")
                chain = ["/".join(parts[: i + 1]) for i in range(len(parts) - 1, -1, -1)]
            for c in chain:
                if c not in expanded:
                    expanded.append(c)
        return expanded

    def _match_values(self, rule: AttributeRule, values: list[str]) -> list[str]:
        vals = list(values)
        if rule.match != MatchKind.EXACT and rule.wildcard and rule.wildcard not in vals:
            vals.append(rule.wildcard)
        return vals

    # ------------------------------------------------------------------ decision

    def decide(self, principal: Principal) -> AccessDecision:
        if principal.is_admin:
            return AccessDecision(odata=None, bypass=True, attributes_used=())

        and_clauses: list[str] = []
        used: list[str] = []
        all_of_denied = False
        for name in self.policy.combine.all_of:
            rule = self.policy.attribute(name)
            pv = self._principal_values(rule, principal)
            if pv is None:
                if rule.required:
                    all_of_denied = True
                    break
                # Not required: principal only sees docs open to everyone for this attribute.
                if rule.is_numeric:
                    and_clauses.append(f"{rule.field} le 0")
                elif rule.wildcard and rule.match != MatchKind.EXACT:
                    and_clauses.append(f"{rule.field}/any(v: v eq '{odata_quote(rule.wildcard)}')")
                else:
                    all_of_denied = True
                    break
                used.append(name)
                continue
            used.append(name)
            and_clauses.append(self._clause(rule, pv))

        grant_clauses: list[str] = []
        for name in self.policy.combine.grant_any_of:
            rule = self.policy.attribute(name)
            pv = self._principal_values(rule, principal)
            if pv is None:
                continue
            used.append(name)
            grant_clauses.append(self._clause(rule, pv, allow_wildcard=False))

        parts: list[str] = []
        if not all_of_denied and and_clauses:
            parts.append("(" + " and ".join(and_clauses) + ")")
        parts.extend(f"({g})" for g in grant_clauses)
        if not parts:
            return AccessDecision(odata=DENY_ALL, bypass=False, attributes_used=tuple(used))
        return AccessDecision(odata=" or ".join(parts), bypass=False, attributes_used=tuple(used))

    def _clause(self, rule: AttributeRule, pv: list[str] | int, *, allow_wildcard: bool = True) -> str:
        if rule.is_numeric:
            assert isinstance(pv, int)
            return f"{rule.field} le {int(pv)}"
        assert isinstance(pv, list)
        vals = self._match_values(rule, pv) if allow_wildcard else list(pv)
        joined = odata_quote(_DELIM.join(vals))
        return f"{rule.field}/any(v: search.in(v, '{joined}', '{_DELIM}'))"

    # ------------------------------------------------------------------ predicate (same semantics)

    def allows(self, principal: Principal, doc_acl: Mapping[str, list[str] | int]) -> bool:
        if principal.is_admin:
            return True
        all_ok = True
        any_all_of = False
        for name in self.policy.combine.all_of:
            rule = self.policy.attribute(name)
            pv = self._principal_values(rule, principal)
            doc_val = doc_acl.get(name)
            if pv is None:
                if rule.required:
                    all_ok = False
                    break
                if rule.is_numeric:
                    pv = 0
                elif rule.wildcard and rule.match != MatchKind.EXACT:
                    pv = []  # only wildcard docs
                else:
                    all_ok = False
                    break
            any_all_of = True
            if not self._rule_ok(rule, pv, doc_val):
                all_ok = False
                break
        if any_all_of and all_ok:
            return True
        for name in self.policy.combine.grant_any_of:
            rule = self.policy.attribute(name)
            pv = self._principal_values(rule, principal)
            if pv is None:
                continue
            if self._rule_ok(rule, pv, doc_acl.get(name), allow_wildcard=False):
                return True
        return False

    def _rule_ok(
        self, rule: AttributeRule, pv: list[str] | int, doc_val: list[str] | int | None, *, allow_wildcard: bool = True
    ) -> bool:
        if doc_val is None:
            return False
        if rule.is_numeric:
            if isinstance(doc_val, list):
                return False
            return int(doc_val) <= int(pv)  # type: ignore[arg-type]
        if not isinstance(doc_val, list):
            return False
        assert isinstance(pv, list)
        vals = set(self._match_values(rule, pv) if allow_wildcard else pv)
        return any(v in vals for v in doc_val)

    # ------------------------------------------------------------------ helpers

    def validate_doc_acl(self, acl: Mapping[str, list[str] | int]) -> dict[str, list[str] | int]:
        """Drop unknown attributes / invalid values from document ACLs before indexing."""
        clean: dict[str, list[str] | int] = {}
        for rule in self.policy.attributes:
            if rule.name not in acl:
                continue
            v = acl[rule.name]
            if rule.is_numeric:
                try:
                    clean[rule.name] = int(v if not isinstance(v, list) else v[0])
                except (TypeError, ValueError, IndexError):
                    continue
            else:
                vals = v if isinstance(v, list) else [v]
                ok = [
                    str(x)
                    for x in vals
                    if str(x) == rule.wildcard or (self._patterns[rule.name].fullmatch(str(x)) and _DELIM not in str(x))
                ]
                if ok:
                    clean[rule.name] = ok
        return clean

    def explain(self, principal: Principal) -> dict[str, object]:
        d = self.decide(principal)
        return {
            "policy_version": self.policy.version,
            "bypass": d.bypass,
            "deny_all": d.deny_all,
            "filter": None if d.deny_all else d.odata,
            "attributes_used": list(d.attributes_used),
            "principal_attributes": principal.attributes,
        }
