"""TagResolver: layered, cheapest-first classification + ACL assignment.

Precedence (later wins per key): source defaults < path rules < sidecar < manifest.
The embedding/LLM classifier only fills facets that are still unset afterwards (see ProcessItem), and
SME-approved tags (review) always win on re-discovery (enforced by the state store).
"""

from __future__ import annotations

from rag_os.domain.classification import FacetSchema, PathRules
from rag_os.domain.documents import SourceItem, TagSet
from rag_os.domain.ingestion import SourceConfig


class TagResolver:
    def __init__(self, facets: FacetSchema, rules: PathRules) -> None:
        self.facets = facets
        self.rules = rules

    def _canonical(self, tags: TagSet) -> TagSet:
        facets: dict[str, list[str]] = {}
        for name, values in tags.facets.items():
            fd = self.facets.get(name)
            if fd is None:
                continue  # unknown facet -> dropped (controlled vocabulary)
            canon = [c for c in (fd.normalise(v) for v in values) if c]
            if canon:
                facets[name] = list(dict.fromkeys(canon)) if fd.multi else canon[:1]
        return TagSet(facets=facets, acl=dict(tags.acl), sources=dict(tags.sources))

    def resolve(self, source: SourceConfig, item: SourceItem, manifest: dict[str, TagSet]) -> TagSet:
        d_facets, d_acl = source.defaults.normalised()
        tags = TagSet().merged_with(TagSet(facets=d_facets, acl=d_acl), "source_default")
        for rule in self.rules.rules:
            if rule.matches(source.id, item.path):
                tags = tags.merged_with(TagSet(facets=rule.facets, acl=rule.acl), f"path_rule:{rule.glob}")
        if item.sidecar is not None:
            tags = tags.merged_with(item.sidecar, "sidecar")
        m = manifest.get(item.path.lower()) or manifest.get(item.item_id.lower())
        if m is not None:
            tags = tags.merged_with(m, "manifest")
        return self._canonical(tags)

    def unset_classifiable(self, tags: TagSet) -> list[str]:
        return [f.name for f in self.facets.facets if f.classify and not tags.facets.get(f.name)]
