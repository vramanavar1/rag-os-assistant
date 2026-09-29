"""TagResolver: layered, cheapest-first classification + ACL assignment.

Precedence (later wins per key): source defaults < path rules < sidecar < manifest.
For a browser upload the path is untrusted, so `facets_from_path` applies the rules' FACET half only - see
its docstring.
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

    def facets_from_path(self, source_id: str, path: str) -> TagSet:
        """Path rules applied to an UNTRUSTED path, yielding facets only - never ACL.

        This exists for browser uploads, where the folder structure is supplied by the client. A path rule
        carries both halves (`hr/**` sets facets.department AND acl.department), and honouring the ACL half of
        a client-supplied string would let any contributor grant themselves another department's access tags -
        `it/**` grants `acl: { department: ["*"] }`, i.e. everyone.

        `rule.acl` is deliberately never read here rather than read and then filtered: the guarantee is a
        property of this function, not of a caller remembering to strip something afterwards.
        """
        tags = TagSet()
        for rule in self.rules.rules:
            if rule.matches(source_id, path):
                tags = tags.merged_with(TagSet(facets=rule.facets), f"path_rule:{rule.glob}")
        return self._canonical(tags)

    def canonical_facets(self, facets: dict[str, list[str]]) -> tuple[TagSet, list[str]]:
        """Canonicalise caller-supplied facets, reporting what the controlled vocabulary refused.

        `_canonical` drops unknown facets and out-of-vocabulary values silently, which is right for a crawl
        (one bad manifest cell should not stop a backfill) and wrong for an interactive upload, where the
        person is owed an error naming what they got wrong.
        """
        rejected: list[str] = []
        for name, values in facets.items():
            fd = self.facets.get(name)
            if fd is None:
                rejected.append(f"unknown facet '{name}'")
                continue
            for v in values:
                if not fd.normalise(v):
                    rejected.append(f"'{v}' is not a value of facet '{name}'")
        return self._canonical(TagSet(facets=facets)), rejected

    def unset_classifiable(self, tags: TagSet) -> list[str]:
        return [f.name for f in self.facets.facets if f.classify and not tags.facets.get(f.name)]
