"""State store change detection/transitions/reporting, tag precedence, profile fingerprints, OData evaluator."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from rag_os.application.ports import DocumentQuery
from rag_os.application.services.tagging import TagResolver
from rag_os.domain.classification import FacetDef, FacetSchema, FacetValue, PathRule, PathRules
from rag_os.domain.documents import DocumentRecord, DocumentStatus, SourceItem, TagSet
from rag_os.domain.embedding import EmbeddingProfile
from rag_os.domain.errors import Conflict
from rag_os.domain.ingestion import SourceConfig, SourceDefaults
from rag_os.infrastructure.search.odata_eval import compile_filter
from rag_os.infrastructure.state.sql_store import SqlStateStore
from rag_os.infrastructure.storage.config_repo import FileConfigRepository


def rec(doc_id: str, version: str, facets: dict[str, list[str]] | None = None) -> DocumentRecord:
    return DocumentRecord(doc_id=doc_id, source_id="s1", item_id=doc_id, path=f"hr/{doc_id}.md", version_key=version,
                          tags=TagSet(facets=facets or {"department": ["HR"]}))


@pytest.fixture()
def store(tmp_path: Path) -> SqlStateStore:
    return SqlStateStore(f"sqlite:///{(tmp_path / 's.db').as_posix()}")


def test_discovery_delta(store: SqlStateStore) -> None:
    d = store.upsert_discovered([rec("a", "v1"), rec("b", "v1")], "run1")
    assert [r.doc_id for r in d.full] == ["a", "b"]
    for doc_id in ("a", "b"):
        for s in (DocumentStatus.QUEUED, DocumentStatus.PARSING, DocumentStatus.CHUNKED, DocumentStatus.EMBEDDED,
                  DocumentStatus.CLASSIFIED):
            store.transition(doc_id, s)
        store.transition(doc_id, DocumentStatus.INDEXED, indexed_version="v1")
    d2 = store.upsert_discovered([rec("a", "v1"), rec("b", "v2"), rec("c", "v1")], "run2")
    assert d2.unchanged == 1 and {r.doc_id for r in d2.full} == {"b", "c"}
    d3 = store.upsert_discovered([rec("a", "v1", {"department": ["Finance"]})], "run3")
    assert [r.doc_id for r in d3.retag] == ["a"]
    assert store.mark_unseen_deleted("s1", "run3") == ["b", "c"]


def test_illegal_transition_and_failed_not_auto_retried(store: SqlStateStore) -> None:
    store.upsert_discovered([rec("a", "v1")], "r")
    with pytest.raises(Conflict):
        store.transition("a", DocumentStatus.INDEXED)
    store.transition("a", DocumentStatus.FAILED, error_type="ParseError", error_message="bad", stage="parse")
    d = store.upsert_discovered([rec("a", "v1")], "r2")
    assert d.skipped_failed == 1 and not d.full
    assert store.error_breakdown(None)[0]["error_type"] == "ParseError"


def test_keyset_paging_and_facet_summary(store: SqlStateStore) -> None:
    store.upsert_discovered([rec(f"d{i:03d}", "v1", {"department": ["HR" if i % 2 else "Sales"]})
                             for i in range(25)], "r")
    page1, nxt = store.query(DocumentQuery(limit=10))
    page2, _ = store.query(DocumentQuery(limit=10, after=nxt))
    assert len(page1) == 10 and page1[-1].doc_id < page2[0].doc_id
    hr, _ = store.query(DocumentQuery(facet=("department", "HR"), limit=100))
    assert len(hr) == 12
    rows = store.summary("department")
    assert {r["key"] for r in rows if r["kind"] == "facet"} == {"HR", "Sales"}


def test_tag_precedence() -> None:
    facets = FacetSchema(facets=[FacetDef(name="department", field="f_department", values=[
        FacetValue(id="HR", synonyms=["people"]), FacetValue(id="Legal"), FacetValue(id="Finance")])])
    rules = PathRules(rules=[PathRule(glob="hr/**", facets={"department": ["people"]}, acl={"department": ["HR"]})])
    tr = TagResolver(facets, rules)
    cfg = SourceConfig(id="s1", type="local_folder", defaults=SourceDefaults(facets={"department": "Finance"}))
    item = SourceItem(source_id="s1", item_id="hr/x.pdf", path="hr/x.pdf",
                      sidecar=TagSet(facets={"department": ["Legal"]}))
    tags = tr.resolve(cfg, item, {})
    assert tags.facets["department"] == ["Legal"] and tags.sources["facet:department"] == "sidecar"
    assert tags.acl["department"] == ["HR"]
    tags2 = tr.resolve(cfg, item, {"hr/x.pdf": TagSet(facets={"department": ["HR"]})})
    assert tags2.sources["facet:department"] == "manifest"


def test_stray_comma_in_a_facet_value_is_rejected() -> None:
    """An unquoted comma in a YAML flow mapping used to truncate the text silently and drop the remainder.

    `{ id: A, description: Prices, fees or rates }` parses as description="Prices" plus an extra null-valued
    key "fees or rates". Forbidding extra keys turns that into a config error instead of a worse classifier.
    """
    doc = yaml.safe_load(
        "version: 1\nfacets:\n  - name: t\n    field: f_t\n    values:\n"
        "      - { id: A, description: Prices, fees or rates for products }\n"
    )
    with pytest.raises(ValidationError):
        FacetSchema.model_validate(doc)
    quoted = yaml.safe_load(
        "version: 1\nfacets:\n  - name: t\n    field: f_t\n    values:\n"
        '      - { id: A, description: "Prices, fees or rates for products" }\n'
    )
    schema = FacetSchema.model_validate(quoted)
    assert schema.facets[0].values[0].description == "Prices, fees or rates for products"


def test_shipped_facets_and_path_rules_agree() -> None:
    """Every value of a required, rule-driven facet needs a path rule, or documents there become invisible."""
    repo = FileConfigRepository(config_dir="./config")
    facets, rules = repo.load_facets(), repo.load_path_rules()
    region = facets.get("region")
    assert region is not None
    globs = " ".join(r.glob.lower() for r in rules.rules)
    missing = [v.id for v in region.values if v.id != "Global" and f"/{v.id.lower()}/" not in globs]
    assert not missing, f"region values with no path rule (documents there get no acl_region): {missing}"


def test_documentation_templates_parse_and_resolve() -> None:
    """docs/examples/* are copy-paste templates: if they stop parsing, the documentation is lying."""
    from rag_os.infrastructure.sources.metadata_files import parse_manifest, parse_sidecar

    repo = FileConfigRepository(config_dir="./config")
    tagger = TagResolver(repo.load_facets(), repo.load_path_rules())
    cfg = next(s for s in repo.load_sources().sources if s.id == "sample-corpus")

    with open("docs/examples/manifest.csv", "rb") as fh:
        manifest = parse_manifest(fh)
    assert len(manifest) == 6, "every row must have a usable `path` (the header is case-sensitive)"

    sidecar = parse_sidecar(Path("docs/examples/contract.docx.meta.json").read_bytes())
    assert sidecar is not None, "the template sidecar must be valid UTF-8 JSON under 256 KiB"
    assert sidecar.acl["clearance"] == 2, "a bare int must stay an int under `acl`"

    # A blank manifest cell inherits from the folder rule rather than clearing it.
    path = "hr/uk/policies/parental-leave-policy.md"
    tags = tagger.resolve(cfg, SourceItem(source_id=cfg.id, item_id=path, path=path), manifest)
    assert tags.acl["department"] == ["HR"] and tags.sources["acl:department"] == "path_rule:hr/**"
    assert tags.facets["topic"] == ["Leave"] and tags.sources["facet:topic"] == "manifest"

    # The manifest outranks the sidecar for the same key; the sidecar fills what it leaves blank.
    path = "sales/emea/contracts/msa-fabrikam.docx"
    item = SourceItem(source_id=cfg.id, item_id=path, path=path, sidecar=sidecar)
    tags = tagger.resolve(cfg, item, manifest)
    assert tags.sources["facet:doc_type"] == "manifest"
    assert tags.facets["topic"] == ["Pricing"] and tags.sources["facet:topic"] == "sidecar"
    assert tags.acl["department"] == ["Sales", "Legal"], "';' separates multiple values"


def test_profile_fingerprint_changes_with_contract() -> None:
    p = EmbeddingProfile(name="a", provider="tei", model="m", dimensions=1024, query_prefix="q:")
    assert p.fingerprint() == p.model_copy(update={"name": "renamed"}).fingerprint()
    assert p.fingerprint() != p.model_copy(update={"query_prefix": "other:"}).fingerprint()
    assert p.index_name("kb", "Enterprise") == f"kb-enterprise-{p.fingerprint()}"


def test_odata_eval_subset() -> None:
    d = {"a": ["x", "y"], "n": 2, "b": True, "s": "q'z"}
    assert compile_filter("a/any(v: search.in(v, 'x|k', '|')) and n le 2 and b eq true")(d)
    assert not compile_filter("a/any(v: v eq 'z') or (n gt 5)")(d)
    assert compile_filter("s eq 'q''z' and not (n eq 3)")(d)
    assert not compile_filter("missing le 5")(d)
