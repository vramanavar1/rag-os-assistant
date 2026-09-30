"""Configurable attribute-based access control model.

The policy is data (YAML), not code: add an attribute (e.g. cost_center) by editing the policy file.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
DEFAULT_VALUE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.@/-]{0,127}$"


class MatchKind(StrEnum):
    ANY_OF = "any_of"  # principal has any value the document allows (wildcard honoured)
    EXACT = "exact"  # like any_of but the document wildcard is NOT honoured
    HIERARCHICAL = "hierarchical"  # principal values expanded to ancestors, then any_of
    MAX_LEVEL = "max_level"  # document level <= principal level (integers)


class AttributeLevel(BaseModel):
    """One rung of a max_level ladder, e.g. 2 = Confidential.

    The ladder is configuration rather than code because `max_level` is generic: a deployment may run 0-5 and
    name the rungs whatever its handbook names them. Until this existed the four familiar names lived only in a
    YAML comment, so a caller was shown `clearance=2` with nothing anywhere that could turn 2 into a word.
    """

    model_config = ConfigDict(extra="forbid")

    value: int
    label: str
    description: str = ""


class AllowedValue(BaseModel):
    """One value an administrator may assign for an attribute, e.g. department HR.

    The master list for the attributes Settings (Security) writes onto a person. It lives here, in the
    admin-only policy file, rather than pointing at a facet vocabulary: facets.yaml is editable by a
    taxonomy_editor (see EDITABLE in api/routers/admin_config.py), so a pointer would put the set of grantable
    security values - and, through facet synonyms, what an existing value canonicalises to - inside a weaker
    permission than the one needed to grant it.

    Not used for a max_level attribute: `levels` is already that attribute's master list, and it carries the
    number each rung means.
    """

    model_config = ConfigDict(extra="forbid")

    value: str
    label: str = ""  # falls back to the value itself
    description: str = ""  # what assigning it means, shown beside the picker


class AppRole(BaseModel):
    """An application role as defined on the Entra app registration, which is where permission is granted.

    Mirrors the catalogue that provisions them (`$script:RagOsEntraAppRoles` in infra/scripts/common.ps1); a
    test keeps the two in step. Two copies exist because one provisions Entra from PowerShell and the other is
    served to a browser, and neither can read the other.
    """

    model_config = ConfigDict(extra="forbid")

    value: str  # the value that appears in the token's roles claim, e.g. rag.admin
    display_name: str = ""
    description: str = ""


class AttributeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo'd key here is a security bug, not a nicety

    name: str
    field: str
    label: str = ""  # for display; falls back to a title-cased name
    match: MatchKind = MatchKind.ANY_OF
    wildcard: str | None = "*"
    required: bool = False
    value_pattern: str = DEFAULT_VALUE_PATTERN
    claims: dict[str, str] = Field(default_factory=dict)  # issuer kind -> claim name
    # Raw claim value -> attribute value, e.g. an Entra group object id -> "HR", or "Internal" -> "1".
    # Matching is case-insensitive; unmapped values pass through unless drop_unmapped is set.
    value_map: dict[str, str] = Field(default_factory=dict)
    drop_unmapped: bool = False  # for claims like Entra "groups" that also carry values meaning nothing here
    hierarchy_facet: str | None = None  # facet whose tree defines ancestors (else "/"-separated paths)
    description: str = ""
    levels: list[AttributeLevel] = Field(default_factory=list)  # max_level only: what each rung means
    # The values an administrator may assign for this attribute. Empty means this attribute is not assignable
    # from Settings (Security) - not that anything goes.
    allowed_values: list[AllowedValue] = Field(default_factory=list)

    _value_lookup: dict[str, str] = PrivateAttr(default_factory=dict)  # lower-cased keys of value_map

    @field_validator("name", "field")
    @classmethod
    def _valid_identifier(cls, v: str) -> str:
        if not FIELD_NAME_RE.match(v):
            raise ValueError(f"invalid identifier {v!r}")
        return v

    @field_validator("value_pattern")
    @classmethod
    def _valid_regex(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:  # not a ValueError, so pydantic would not wrap it into a clean 422
            raise ValueError(f"invalid value_pattern regex: {e}") from None
        return v

    @model_validator(mode="after")
    def _check_value_map(self) -> AttributeRule:
        """Reject an unusable map at load time rather than as a 401 on someone's first query."""
        if self.drop_unmapped and not self.value_map:
            raise ValueError(f"attribute {self.name!r}: drop_unmapped needs a non-empty value_map")
        pattern = re.compile(self.value_pattern)
        lookup: dict[str, str] = {}
        for raw, mapped in self.value_map.items():
            key = str(raw).strip().lower()
            if not key:
                raise ValueError(f"attribute {self.name!r}: value_map has an empty key")
            if key in lookup:
                raise ValueError(f"attribute {self.name!r}: duplicate value_map key {raw!r}")
            if self.is_numeric:
                try:
                    int(mapped)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"attribute {self.name!r}: value_map maps {raw!r} to {mapped!r}, which is not an integer"
                    ) from None
            elif not pattern.fullmatch(mapped):
                raise ValueError(
                    f"attribute {self.name!r}: value_map maps {raw!r} to {mapped!r}, "
                    f"which does not match value_pattern"
                )
            lookup[key] = mapped
        self._value_lookup = lookup
        return self

    @model_validator(mode="after")
    def _check_allowed_values(self) -> AttributeRule:
        """The master list is what an administrator may write onto a person, so every entry must be a value
        that actually works. Runs after _check_value_map because the round trip below needs the lookup.
        """
        if not self.allowed_values:
            return self
        pattern = re.compile(self.value_pattern)
        seen: dict[str, str] = {}
        for entry in self.allowed_values:
            v = entry.value
            key = v.casefold()
            if key in seen:
                raise ValueError(
                    f"attribute {self.name!r}: duplicate allowed_values entry {v!r} (already have {seen[key]!r}); "
                    f"document tags are matched case-sensitively, so at most one of them grants anything"
                )
            seen[key] = v
            if self.wildcard is not None and v == self.wildcard:
                raise ValueError(
                    f"attribute {self.name!r}: the wildcard {v!r} cannot be an allowed_values entry - assigning it "
                    f"to a person would match every document, which is the admin role's job and not a picker option"
                )
            # Duplicated from _DELIM in application/services/access_policy.py, which the domain must not import.
            # A value containing it does not narrow access; it makes every query that caller runs fail.
            if "|" in v:
                raise ValueError(
                    f"attribute {self.name!r}: allowed_values entry {v!r} contains the filter delimiter '|'"
                )
            if not pattern.fullmatch(v):
                raise ValueError(
                    f"attribute {self.name!r}: allowed_values entry {v!r} does not match value_pattern"
                )
            # The round trip. Under a value_map + drop_unmapped configuration (the documented way to drive
            # `department` from Entra groups) a name like "HR" is discarded on the way IN, so writing it to the
            # directory would succeed, report success, and never reach a token. Nothing downstream can see that.
            if self.map_value(v) != v:
                raise ValueError(
                    f"attribute {self.name!r}: allowed_values entry {v!r} does not survive value_map - this claim "
                    f"is mapped on the way in, so assigning {v!r} directly would never reach a token"
                )
        return self

    def map_value(self, raw: str) -> str | None:
        """Raw claim value -> attribute value. None means 'discard this value'."""
        if not self._value_lookup:
            return raw
        mapped = self._value_lookup.get(raw.strip().lower())
        if mapped is not None:
            return mapped
        return None if self.drop_unmapped else raw

    @property
    def is_numeric(self) -> bool:
        return self.match == MatchKind.MAX_LEVEL


class CombineRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_of: list[str] = Field(default_factory=list)
    grant_any_of: list[str] = Field(default_factory=list)


class IssuerRoleConfig(BaseModel):
    """Which issuers may assert roles (only an issuer you control should ever grant admin)."""

    model_config = ConfigDict(extra="forbid")

    trusted_for_roles: list[str] = Field(default_factory=lambda: ["entra", "dev"])
    role_claim: dict[str, str] = Field(default_factory=lambda: {"entra": "roles", "dev": "roles"})


class AccessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    default_decision: str = "deny"
    attributes: list[AttributeRule]
    combine: CombineRule
    roles: dict[str, list[str]] = Field(default_factory=dict)  # app role -> accepted role claim values
    app_roles: list[AppRole] = Field(default_factory=list)  # as defined on the Entra app registration
    role_sources: IssuerRoleConfig = Field(default_factory=IssuerRoleConfig)

    @model_validator(mode="after")
    def _check_refs(self) -> AccessPolicy:
        names = [a.name for a in self.attributes]
        if len(names) != len(set(names)):
            raise ValueError("duplicate attribute names")
        fields = [a.field for a in self.attributes]
        if len(fields) != len(set(fields)):
            raise ValueError("duplicate index field names")
        unknown = (set(self.combine.all_of) | set(self.combine.grant_any_of)) - set(names)
        if unknown:
            raise ValueError(f"combine references unknown attributes: {sorted(unknown)}")
        if self.default_decision != "deny":
            raise ValueError("only default_decision: deny is supported")
        if not self.combine.all_of and not self.combine.grant_any_of:
            raise ValueError("combine must reference at least one attribute")
        # A ladder only means something where the match is "document level <= mine".
        for a in self.attributes:
            if a.levels and a.match != MatchKind.MAX_LEVEL:
                raise ValueError(f"attribute '{a.name}' has levels but match is {a.match}, not max_level")
            values = [lvl.value for lvl in a.levels]
            if len(values) != len(set(values)):
                raise ValueError(f"attribute '{a.name}' has duplicate level values")
            # The mirror image: a ladder IS the master list for max_level, and it carries each rung's number.
            # Two lists on one attribute would drift, and whichever the UI read would decide who reads what.
            if a.allowed_values and a.match == MatchKind.MAX_LEVEL:
                raise ValueError(
                    f"attribute '{a.name}' has allowed_values but match is max_level; use levels, which is "
                    f"already its master list"
                )
        # The catalogue and the mapping describe the same app registration, so they must not drift: a role
        # granted by a value nobody defined would be invisible on the account page and impossible to assign.
        if self.app_roles:
            defined = {r.value for r in self.app_roles}
            granted = {v for accepted in self.roles.values() for v in accepted}
            missing = granted - defined
            if missing:
                raise ValueError(f"roles reference app role values not in app_roles: {sorted(missing)}")
        return self

    def attribute(self, name: str) -> AttributeRule:
        for a in self.attributes:
            if a.name == name:
                return a
        raise KeyError(name)

    def fingerprint_fields(self) -> list[tuple[str, str]]:
        """Schema-relevant part of the policy (field name + type). Used for index compatibility checks."""
        return sorted((a.field, "int" if a.is_numeric else "strings") for a in self.attributes)


class Principal(BaseModel):
    """The authenticated caller. Attributes are generic, mapped from token claims by policy."""

    subject: str
    issuer_kind: str  # entra | dev
    display_name: str = ""
    attributes: dict[str, list[str] | int] = Field(default_factory=dict)
    roles: set[str] = Field(default_factory=set)  # application roles (admin, taxonomy_editor, ...)
    # The role values the token actually carried, kept because the mapping above is many-to-many and so
    # cannot be inverted: rag.admin grants BOTH admin and contributor, so working backwards from the mapped
    # roles would report an app role nobody assigned. Only set from an issuer trusted to assert roles.
    claimed_roles: list[str] = Field(default_factory=list)
    raw_claims: dict[str, Any] = Field(default_factory=dict, exclude=True)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def directory_object_id(self) -> str | None:
        """This caller's Entra object id, or None when the token cannot identify a directory object.

        NOT `subject`. ClaimsMapper prefers the `sub` claim, and for Entra `sub` is a pairwise identifier
        scoped to one application - it exists nowhere in the directory. So any check that compares this caller
        against a Graph object ("you may not edit your own clearance") must use this and must refuse when it is
        None: comparing `subject` would never match, and the check would pass for everyone.

        None for the dev issuer by construction. A dev token asserts whatever a developer typed, roles
        included, so it must not be able to reach the directory at all.
        """
        if self.issuer_kind != "entra":
            return None
        oid = self.raw_claims.get("oid")
        return str(oid) if oid else None
