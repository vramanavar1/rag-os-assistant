"""Query expansion from the SME taxonomy (facet labels + synonyms) for the KEYWORD half of hybrid search.

The text sent to the embedder is never modified (it must match the embedding profile contract)."""

from __future__ import annotations

import re

from rag_os.domain.classification import FacetSchema

_WORD = re.compile(r"\w+", re.UNICODE)


class QueryExpander:
    def __init__(self, facets: FacetSchema) -> None:
        self._index: dict[str, set[str]] = {}
        for fd in facets.facets:
            for v in fd.values:
                terms = {v.id, v.label, *v.synonyms} - {""}
                lowered = {t.lower() for t in terms}
                for t in lowered:
                    self._index.setdefault(t, set()).update(lowered)

    def expand(self, query: str) -> str:
        q = query.lower()
        words = set(_WORD.findall(q))
        extra: list[str] = []
        for term, related in self._index.items():
            hit = term in words if " " not in term else term in q
            if hit:
                extra += [r for r in related if r != term and r not in q and r not in extra]
        return query if not extra else f"{query} {' '.join(extra[:12])}"
