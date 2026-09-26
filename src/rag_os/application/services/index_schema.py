"""Index schema generated from configuration (embedding profile + access policy + facet schema).

The same mapper is used by every SearchIndex adapter so Azure AI Search and the local backend
see identical field names and values.
"""

from __future__ import annotations

from typing import Any

from rag_os.application.ports import IndexField, IndexSchema
from rag_os.domain.access import AccessPolicy
from rag_os.domain.classification import FacetSchema
from rag_os.domain.documents import IndexedChunk
from rag_os.domain.embedding import EmbeddingProfile

BASE_SELECT = ["chunk_id", "doc_id", "title", "heading", "content", "path", "page", "source_id"]
CURRENT_FILTER = "is_current eq true"


def build_schema(
    index_name: str, profile: EmbeddingProfile, policy: AccessPolicy, facets: FacetSchema, compression: str
) -> IndexSchema:
    fields = [
        IndexField("chunk_id", "string", key=True, filterable=True),
        IndexField("doc_id", "string", filterable=True),
        IndexField("doc_version", "string", filterable=True),
        IndexField("ordinal", "int", sortable=True),
        IndexField("title", "string", searchable=True),
        IndexField("heading", "string", searchable=True),
        IndexField("content", "string", searchable=True),
        IndexField("path", "string", searchable=True, filterable=True),
        IndexField("page", "int", filterable=True),
        IndexField("source_id", "string", filterable=True, facetable=True),
        IndexField("content_type", "string", filterable=True, facetable=True),
        IndexField("embedding_fp", "string", filterable=True),
        # What the embedding pool REPORTED about itself when this chunk was written, as "model@revision".
        # embedding_fp above records what was CONFIGURED, so the two agreeing is the evidence that the
        # vectors here and the vectors a query is embedded into came from the same model. Filterable so a
        # suspect batch can be found: $filter=embedded_by ne '<expected>'.
        IndexField("embedded_by", "string", filterable=True),
        IndexField("is_current", "bool", filterable=True),
        IndexField("effective_date", "string", filterable=True, sortable=True),
        IndexField("vector", "vector", retrievable=False, dimensions=profile.dimensions),
    ]
    for rule in policy.attributes:
        if rule.is_numeric:
            fields.append(IndexField(rule.field, "int", filterable=True, retrievable=False))
        else:
            fields.append(IndexField(rule.field, "strings", filterable=True, facetable=True, retrievable=False))
    for fd in facets.facets:
        fields.append(IndexField(fd.field, "strings", filterable=True, facetable=True))
    names = [f.name for f in fields]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"index field name collision between policy/facets/base: {sorted(dupes)}")
    return IndexSchema(name=index_name, fields=fields, compression=compression)


class IndexDocumentMapper:
    """IndexedChunk -> flat index document using configured field names."""

    def __init__(self, policy: AccessPolicy, facets: FacetSchema) -> None:
        self._acl_fields = {a.name: (a.field, a.is_numeric) for a in policy.attributes}
        self._facet_fields = {f.name: f.field for f in facets.facets}
        self._facets = facets

    @property
    def facet_fields(self) -> dict[str, str]:
        return dict(self._facet_fields)

    def to_document(self, c: IndexedChunk) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "chunk_id": c.chunk_id,
            "doc_id": c.doc_id,
            "doc_version": c.doc_version,
            "ordinal": c.ordinal,
            "title": c.title,
            "heading": c.heading,
            "content": c.content,
            "path": c.path,
            "page": c.page,
            "source_id": c.source_id,
            "content_type": c.content_type,
            "embedding_fp": c.embedding_fp,
            "embedded_by": c.embedded_by,
            "is_current": c.is_current,
            "effective_date": c.effective_date,
            "vector": c.vector,
        }
        for name, (field, numeric) in self._acl_fields.items():
            v = c.acl.get(name)
            if numeric:
                doc[field] = int(v) if isinstance(v, int) else None
            else:
                doc[field] = list(v) if isinstance(v, list) else []
        expanded = self._facets.expand_for_index(c.facets)
        for name, field in self._facet_fields.items():
            doc[field] = expanded.get(name, [])
        return doc

    def facets_from_document(self, doc: dict[str, Any]) -> dict[str, list[str]]:
        return {name: list(doc.get(field) or []) for name, field in self._facet_fields.items() if doc.get(field)}
