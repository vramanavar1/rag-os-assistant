"""Faceted classification schema (controlled vocabularies) and path rules."""

from __future__ import annotations

import fnmatch

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_os.domain.access import FIELD_NAME_RE


class FacetValue(BaseModel):
    # An unquoted comma inside a YAML flow mapping ({id: X, description: a, b}) ends the entry and turns the
    # rest into extra keys. Rejecting them makes that a config error instead of silently truncated text.
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str = ""
    parent: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    description: str = ""


class FacetDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    field: str
    label: str = ""
    hierarchical: bool = False
    multi: bool = True
    owner: str = ""
    closed: bool = True  # only listed values allowed
    classify: bool = False  # auto-classify when unset after rules
    values: list[FacetValue] = Field(default_factory=list)

    @field_validator("name", "field")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not FIELD_NAME_RE.match(v):
            raise ValueError(f"invalid identifier {v!r}")
        return v

    @model_validator(mode="after")
    def _tree(self) -> FacetDef:
        ids = [v.id for v in self.values]
        if len(ids) != len(set(ids)):
            raise ValueError(f"facet {self.name}: duplicate value ids")
        known = set(ids)
        for v in self.values:
            if v.parent is not None:
                if not self.hierarchical:
                    raise ValueError(f"facet {self.name}: parent set on a flat facet")
                if v.parent not in known:
                    raise ValueError(f"facet {self.name}: unknown parent {v.parent!r}")
        # cycle / depth check
        parents = {v.id: v.parent for v in self.values}
        for vid in ids:
            seen: set[str] = set()
            cur: str | None = vid
            depth = 0
            while cur is not None:
                if cur in seen:
                    raise ValueError(f"facet {self.name}: cycle at {vid!r}")
                seen.add(cur)
                cur = parents.get(cur)
                depth += 1
                if depth > 8:
                    raise ValueError(f"facet {self.name}: depth > 8 at {vid!r}")
        return self

    def ancestors(self, value_id: str) -> list[str]:
        """value itself followed by its ancestors (root last)."""
        parents = {v.id: v.parent for v in self.values}
        out: list[str] = []
        cur: str | None = value_id
        while cur is not None and cur not in out:
            out.append(cur)
            cur = parents.get(cur)
        return out

    def normalise(self, raw: str) -> str | None:
        """Map a raw value or synonym (case-insensitive) to a canonical id."""
        r = raw.strip().lower()
        for v in self.values:
            if v.id.lower() == r or v.label.lower() == r or r in (s.lower() for s in v.synonyms):
                return v.id
        return None if self.closed else raw.strip()


class FacetSchema(BaseModel):
    version: int = 1
    facets: list[FacetDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> FacetSchema:
        names = [f.name for f in self.facets]
        if len(names) != len(set(names)):
            raise ValueError("duplicate facet names")
        fields = [f.field for f in self.facets]
        if len(fields) != len(set(fields)):
            raise ValueError("duplicate facet fields")
        return self

    def get(self, name: str) -> FacetDef | None:
        return next((f for f in self.facets if f.name == name), None)

    def expand_for_index(self, facets: dict[str, list[str]]) -> dict[str, list[str]]:
        """Canonicalise values and include ancestors for hierarchical facets (so parent filters match)."""
        out: dict[str, list[str]] = {}
        for name, values in facets.items():
            fd = self.get(name)
            if fd is None:
                continue
            acc: list[str] = []
            for raw in values:
                canon = fd.normalise(raw)
                if canon is None:
                    continue
                chain = fd.ancestors(canon) if fd.hierarchical else [canon]
                for c in chain:
                    if c not in acc:
                        acc.append(c)
            if acc:
                out[name] = acc
        return out


class PathRule(BaseModel):
    glob: str
    facets: dict[str, list[str]] = Field(default_factory=dict)
    acl: dict[str, list[str] | int] = Field(default_factory=dict)
    sources: list[str] | None = None  # restrict to these source ids

    def matches(self, source_id: str, path: str) -> bool:
        if self.sources is not None and source_id not in self.sources:
            return False
        p = path.replace("\\", "/").lstrip("/").lower()
        return fnmatch.fnmatchcase(p, self.glob.lower())


class PathRules(BaseModel):
    version: int = 1
    rules: list[PathRule] = Field(default_factory=list)
