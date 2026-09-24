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


class AttributeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo'd key here is a security bug, not a nicety

    name: str
    field: str
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
    raw_claims: dict[str, Any] = Field(default_factory=dict, exclude=True)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles
