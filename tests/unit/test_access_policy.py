"""AccessPolicyEngine: semantics, default-deny, injection safety, and OData <-> predicate agreement."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from rag_os.application.services.access_policy import AccessDecision, AccessPolicyEngine
from rag_os.domain.access import AccessPolicy, AttributeRule, CombineRule, MatchKind, Principal
from rag_os.domain.classification import FacetDef, FacetSchema, FacetValue
from rag_os.domain.errors import AuthenticationFailed
from rag_os.infrastructure.search.odata_eval import compile_filter
from rag_os.infrastructure.storage.config_repo import FileConfigRepository

REPO = Path(__file__).resolve().parents[2]

REGION = FacetDef(name="region", field="f_region", hierarchical=True, values=[
    FacetValue(id="Global"), FacetValue(id="EMEA", parent="Global"), FacetValue(id="UK", parent="EMEA"),
    FacetValue(id="AMER", parent="Global"), FacetValue(id="US", parent="AMER"),
])
FACETS = FacetSchema(facets=[REGION])

POLICY = AccessPolicy(
    attributes=[
        AttributeRule(name="department", field="acl_department", required=True),
        AttributeRule(name="region", field="acl_region", match=MatchKind.HIERARCHICAL, hierarchy_facet="region",
                      required=True),
        AttributeRule(name="clearance", field="acl_clearance", match=MatchKind.MAX_LEVEL),
        AttributeRule(name="employee_id", field="acl_employee_id", match=MatchKind.EXACT),
    ],
    combine=CombineRule(all_of=["department", "region", "clearance"], grant_any_of=["employee_id"]),
    roles={"admin": ["rag.admin"]},
)
ENGINE = AccessPolicyEngine(POLICY, FACETS)


def P(**attrs: object) -> Principal:
    return Principal(subject="u", issuer_kind="entra", attributes=attrs)  # type: ignore[arg-type]


def doc(dept: list[str] | None = None, region: list[str] | None = None, level: int | None = None,
        emp: list[str] | None = None) -> dict[str, object]:
    """Index document as Azure sees it (field names) + the ACL dict the predicate sees (attribute names)."""
    return {"acl_department": dept or [], "acl_region": region or [], "acl_clearance": level,
            "acl_employee_id": emp or []}


def acl_of(d: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    if d["acl_department"]:
        out["department"] = d["acl_department"]
    if d["acl_region"]:
        out["region"] = d["acl_region"]
    if d["acl_clearance"] is not None:
        out["clearance"] = d["acl_clearance"]
    if d["acl_employee_id"]:
        out["employee_id"] = d["acl_employee_id"]
    return out


def visible(principal: Principal, d: dict[str, object]) -> bool:
    decision = ENGINE.decide(principal)
    if decision.deny_all:
        return False
    return compile_filter(decision.odata)(d)


def test_department_and_hierarchical_region() -> None:
    hr_uk = P(department=["HR"], region=["UK"], clearance=1)
    assert visible(hr_uk, doc(["HR"], ["UK"], 1))
    assert visible(hr_uk, doc(["HR"], ["EMEA"], 1))  # parent region visible to child
    assert visible(hr_uk, doc(["HR"], ["Global"], 0))
    assert not visible(hr_uk, doc(["HR"], ["US"], 1))
    assert not visible(hr_uk, doc(["Sales"], ["UK"], 1))
    assert not visible(P(department=["HR"], region=["EMEA"], clearance=1), doc(["HR"], ["UK"], 1))  # child not up


def test_wildcard_and_clearance() -> None:
    p = P(department=["Sales"], region=["US"], clearance=1)
    assert visible(p, doc(["*"], ["*"], 0))
    assert not visible(p, doc(["*"], ["*"], 2))  # above clearance
    assert not visible(p, doc(["*"], ["*"], None))  # no level -> deny


def test_default_deny_untagged_and_missing_required() -> None:
    assert not visible(P(department=["HR"], region=["UK"], clearance=3), doc())
    d = ENGINE.decide(P(region=["UK"]))  # department required
    assert d.deny_all


def test_grant_override() -> None:
    p = P(department=["HR"], region=["UK"], clearance=1, employee_id=["E1"])
    assert visible(p, doc(["Legal"], ["US"], 3, emp=["E1"]))
    assert not visible(p, doc(["Legal"], ["US"], 3, emp=["E2"]))
    # exact match: the wildcard is never honoured for grants
    assert not visible(P(department=["X"], region=["US"], employee_id=["E9"]), doc(["Legal"], ["US"], 0, emp=["*"]))


def test_a_grant_survives_a_missing_required_attribute() -> None:
    """`required` removes the all_of branch, not the grant branch — the two are ORed, not ANDed.

    This is what lets one document be shared with one person without widening its department tag, and it is
    the case the hypothesis strategy never generates (it pairs a missing attribute with a missing grant).
    """
    shared = doc(["Legal"], ["US"], 3, emp=["E1"])
    # no department and no region at all: the all_of branch cannot be built
    only_grant = P(employee_id=["E1"])
    assert ENGINE.decide(only_grant).odata == "(acl_employee_id/any(v: search.in(v, 'E1', '|')))"
    assert visible(only_grant, shared)
    assert not visible(only_grant, doc(["Legal"], ["US"], 3))  # nothing shared with them
    # the same caller without the grant is denied outright
    assert ENGINE.decide(P()).deny_all


def test_minimal_policy_template_loads_and_behaves_as_documented() -> None:
    """docs/examples/access-policy.minimal.yaml is a copy-paste starting point; keep it honest."""
    doc = yaml.safe_load(Path("docs/examples/access-policy.minimal.yaml").read_text(encoding="utf-8"))
    policy = AccessPolicy.model_validate(doc)
    assert [a.name for a in policy.attributes] == ["department"]
    assert policy.version == 1 and policy.default_decision == "deny" and policy.roles == {}
    rule = policy.attributes[0]
    assert rule.match == MatchKind.ANY_OF and rule.wildcard == "*" and rule.required is False

    engine = AccessPolicyEngine(policy)
    has = Principal(subject="u", issuer_kind="entra", attributes={"department": ["HR"]})
    none = Principal(subject="u", issuer_kind="entra", attributes={})
    assert engine.decide(has).odata == "(acl_department/any(v: search.in(v, 'HR|*', '|')))"
    # required defaults to false, so a caller with no department sees public documents rather than nothing
    assert engine.decide(none).odata == "(acl_department/any(v: v eq '*'))"


def test_required_false_is_meaningless_without_a_way_to_say_everyone() -> None:
    """`required: false` reads as optional, but only a numeric or a wildcarded non-exact attribute can honour it."""
    def decide_without_value(**rule_kwargs: object) -> AccessDecision:
        pol = AccessPolicy(
            attributes=[AttributeRule(name="x", field="acl_x", **rule_kwargs)],  # type: ignore[arg-type]
            combine=CombineRule(all_of=["x"]),
        )
        return AccessPolicyEngine(pol).decide(Principal(subject="u", issuer_kind="entra", attributes={}))

    assert decide_without_value(required=False).odata == "(acl_x/any(v: v eq '*'))"
    assert decide_without_value(required=False, match=MatchKind.MAX_LEVEL).odata == "(acl_x le 0)"
    # no way to express "open to everyone" -> denied exactly as if required
    assert decide_without_value(required=False, wildcard=None).deny_all
    assert decide_without_value(required=False, match=MatchKind.EXACT).deny_all


@pytest.mark.parametrize("doc", [
    {"attributes": [{"name": "a", "field": "acl_a", "require": True}], "combine": {"all_of": ["a"]}},
    {"attributes": [{"name": "a", "field": "acl_a", "wildcards": "*"}], "combine": {"all_of": ["a"]}},
    {"attributes": [{"name": "a", "field": "acl_a"}], "combine": {"all_of": ["a"], "grant_anyof": ["a"]}},
    {"attributes": [{"name": "a", "field": "acl_a", "value_pattern": "["}], "combine": {"all_of": ["a"]}},
])
def test_a_typo_in_the_policy_is_an_error_not_a_silent_no_op(doc: dict[str, object]) -> None:
    """`require:` instead of `required:` used to load fine and leave the attribute optional — a security bug.

    A bad `value_pattern` regex used to raise re.PatternError, which is not a ValueError, so pydantic never
    wrapped it and the config loader crashed instead of returning a clean validation error.
    """
    with pytest.raises(ValidationError):
        AccessPolicy.model_validate(doc)


def test_admin_bypass() -> None:
    d = ENGINE.decide(Principal(subject="a", issuer_kind="entra", roles={"admin"}))
    assert d.bypass and d.odata is None


@pytest.mark.parametrize("bad", ["HR' or true or '", "HR|Sales", "a)", "", "x" * 200, "HR\nSales"])
def test_injection_values_rejected(bad: str) -> None:
    with pytest.raises(AuthenticationFailed):
        ENGINE.decide(P(department=[bad], region=["UK"]))


def test_quotes_are_escaped_when_pattern_allows_them() -> None:
    pol = POLICY.model_copy(deep=True)
    pol.attributes[0].value_pattern = r"^[A-Za-z' ]{1,20}$"
    eng = AccessPolicyEngine(pol, FACETS)
    d = eng.decide(P(department=["O'Brien"], region=["UK"], clearance=0))
    assert "O''Brien" in (d.odata or "")
    assert compile_filter(d.odata)(doc(["O'Brien"], ["UK"], 0))


def test_new_attribute_is_pure_configuration() -> None:
    pol = POLICY.model_copy(deep=True)
    pol.attributes.append(AttributeRule(name="cost_center", field="acl_cost_center", required=True))
    pol.combine.all_of.append("cost_center")
    eng = AccessPolicyEngine(pol, FACETS)
    d = eng.decide(P(department=["HR"], region=["UK"], clearance=1, cost_center=["CC-1"]))
    assert "acl_cost_center/any" in (d.odata or "")
    assert eng.decide(P(department=["HR"], region=["UK"], clearance=1)).deny_all


# ------------------------------------------------------------------ property: OData == predicate

VALS = st.sampled_from(["HR", "Sales", "Legal", "*"])
REGIONS = st.sampled_from(["Global", "EMEA", "UK", "AMER", "US", "*"])
EMPS = st.sampled_from(["E1", "E2", "*"])


@st.composite
def principals(draw: st.DrawFn) -> Principal:
    attrs: dict[str, object] = {}
    if draw(st.booleans()):
        attrs["department"] = draw(st.lists(st.sampled_from(["HR", "Sales", "Legal"]), min_size=1, max_size=2))
    if draw(st.booleans()):
        attrs["region"] = draw(st.lists(st.sampled_from(["UK", "EMEA", "US", "Global"]), min_size=1, max_size=2))
    if draw(st.booleans()):
        attrs["clearance"] = draw(st.integers(0, 3))
    if draw(st.booleans()):
        attrs["employee_id"] = [draw(st.sampled_from(["E1", "E2"]))]
    return P(**attrs)


@st.composite
def docs(draw: st.DrawFn) -> dict[str, object]:
    return doc(
        draw(st.lists(VALS, max_size=2)),
        draw(st.lists(REGIONS, max_size=2)),
        draw(st.one_of(st.none(), st.integers(0, 3))),
        draw(st.lists(EMPS, max_size=1)),
    )


@settings(max_examples=400, deadline=None)
@given(principals(), docs())
def test_odata_and_predicate_agree(p: Principal, d: dict[str, object]) -> None:
    assert visible(p, d) == ENGINE.allows(p, acl_of(d))  # type: ignore[arg-type]


# ---------------------------------------------------------------- policy metadata that the UI renders
# The clearance ladder and the application-role catalogue became configuration so Account Information could
# show a caller what their numbers and roles mean. Both are security-adjacent, so both are validated.


def _policy(**over: object) -> dict:
    base = {
        "attributes": [
            {"name": "department", "field": "acl_department", "match": "any_of", "required": True,
             "claims": {"dev": "departments"}},
            {"name": "clearance", "field": "acl_clearance", "match": "max_level", "claims": {"dev": "clearance"}},
        ],
        "combine": {"all_of": ["department", "clearance"]},
    }
    return {**base, **over}


def test_a_level_ladder_is_only_meaningful_for_max_level() -> None:
    """Rungs describe "document level <= mine". On an any_of attribute they would be decoration that reads
    like policy, which is the worst kind of configuration."""
    from rag_os.domain.access import AccessPolicy

    bad = _policy(attributes=[
        {"name": "department", "field": "acl_department", "match": "any_of", "claims": {"dev": "departments"},
         "levels": [{"value": 0, "label": "Public"}]},
    ], combine={"all_of": ["department"]})
    with pytest.raises(ValidationError, match="max_level"):
        AccessPolicy.model_validate(bad)


def test_duplicate_level_values_are_rejected() -> None:
    from rag_os.domain.access import AccessPolicy

    bad = _policy(attributes=[
        {"name": "clearance", "field": "acl_clearance", "match": "max_level", "claims": {"dev": "clearance"},
         "levels": [{"value": 1, "label": "Internal"}, {"value": 1, "label": "Confidential"}]},
    ], combine={"all_of": ["clearance"]})
    with pytest.raises(ValidationError, match="duplicate level"):
        AccessPolicy.model_validate(bad)


def test_a_role_cannot_be_granted_by_an_app_role_nobody_defined() -> None:
    """The catalogue and the mapping describe one app registration. A value in the mapping with no entry in
    the catalogue is invisible on the account page and impossible to assign - it would silently grant nothing
    while looking configured."""
    from rag_os.domain.access import AccessPolicy

    bad = _policy(roles={"admin": ["rag.admin"], "contributor": ["rag.typo"]},
                  app_roles=[{"value": "rag.admin", "display_name": "Admin"}])
    with pytest.raises(ValidationError, match=re.escape("rag.typo")):
        AccessPolicy.model_validate(bad)


def test_the_shipped_policy_describes_every_role_it_grants() -> None:
    repo = FileConfigRepository(config_dir="./config")
    policy = repo.load_access_policy()
    defined = {r.value for r in policy.app_roles}
    assert defined, "the catalogue is what Account Information lists"
    for accepted in policy.roles.values():
        assert set(accepted) <= defined
    for role in policy.app_roles:
        assert role.display_name and role.description, f"{role.value} needs a name and a sentence for the UI"


def test_the_app_roles_agree_with_the_script_that_creates_them() -> None:
    """Two copies, in two languages, of one app registration's roles.

    infra/scripts/common.ps1 creates them in Entra through Graph; access-policy.yaml is served to a browser.
    Neither can read the other, so this pins them together - the same cross-artefact idiom used for the Entra
    redirect path and the upload size cap.
    """
    ps = (REPO / "infra" / "scripts" / "common.ps1").read_text(encoding="utf-8")
    block = re.search(r"\$script:RagOsEntraAppRoles\s*=\s*@\((.*?)\n\)", ps, re.S)
    assert block, "the PowerShell role catalogue moved; re-point this test"
    from_ps = dict(re.findall(r"Value\s*=\s*'([^']+)';\s*DisplayName\s*=\s*'([^']+)'", block.group(1)))
    assert from_ps, "no roles parsed out of the PowerShell catalogue"

    policy = FileConfigRepository(config_dir="./config").load_access_policy()
    from_yaml = {r.value: r.display_name for r in policy.app_roles}
    assert from_yaml == from_ps, (
        "access-policy.yaml app_roles and $script:RagOsEntraAppRoles disagree. They provision and describe the "
        "same four roles; change one and you must change the other.")


# ---------------------------------------------------------------- the master list of grantable values
# Settings (Security) writes department/region/clearance onto an Entra user. What it may write is
# `allowed_values` here, never whatever the browser posted, so these validations are the boundary of that
# feature rather than convenience for a dropdown.


def test_allowed_values_are_rejected_on_a_max_level_attribute() -> None:
    """A ladder already has a master list - `levels` - and it carries the number each rung means. Two lists on
    one attribute would disagree eventually, and the one the UI happened to read would decide who reads what."""
    bad = _policy(attributes=[
        {"name": "clearance", "field": "acl_clearance", "match": "max_level", "claims": {"dev": "clearance"},
         "allowed_values": [{"value": "1"}]},
    ], combine={"all_of": ["clearance"]})
    with pytest.raises(ValidationError, match="levels"):
        AccessPolicy.model_validate(bad)


def test_allowed_values_reject_duplicates_differing_only_in_case() -> None:
    """`any_of` matches a caller's value against document tags case-sensitively, so HR and hr are two different
    grants and at most one of them matches anything. Offering both in a picker guarantees someone picks the
    one that silently grants nothing."""
    bad = _policy(attributes=[
        {"name": "department", "field": "acl_department", "match": "any_of", "claims": {"dev": "departments"},
         "allowed_values": [{"value": "HR"}, {"value": "hr"}]},
    ], combine={"all_of": ["department"]})
    with pytest.raises(ValidationError, match="duplicate allowed_values"):
        AccessPolicy.model_validate(bad)


def test_an_allowed_value_containing_the_filter_delimiter_is_rejected() -> None:
    """The search filter joins a caller's values on "|", so AccessPolicyEngine refuses any attribute value
    containing one - see _principal_values. Writing such a value onto a person does not narrow their access;
    it makes every query they run fail authentication, permanently, until someone edits the directory."""
    bad = _policy(attributes=[
        {"name": "department", "field": "acl_department", "match": "any_of", "claims": {"dev": "departments"},
         "value_pattern": r"^[A-Za-z|]+$", "allowed_values": [{"value": "HR|Finance"}]},
    ], combine={"all_of": ["department"]})
    with pytest.raises(ValidationError, match="delimiter"):
        AccessPolicy.model_validate(bad)


def test_an_allowed_value_must_not_violate_the_value_pattern() -> None:
    bad = _policy(attributes=[
        {"name": "department", "field": "acl_department", "match": "any_of", "claims": {"dev": "departments"},
         "allowed_values": [{"value": "!!nope!!"}]},
    ], combine={"all_of": ["department"]})
    with pytest.raises(ValidationError, match="value_pattern"):
        AccessPolicy.model_validate(bad)


def test_an_allowed_value_must_survive_the_claims_mapper_unchanged() -> None:
    """The check that makes writing attributes safe at all.

    A deployment may point `department` at Entra's `groups` claim and map group object ids to names, which the
    shipped policy documents as the way to grant a whole department. Under that configuration map_value("HR")
    is None: the claim is discarded on the way in. Settings (Security) would PATCH the directory extension,
    Graph would return 204, the page would report success - and the value would never reach a token. Nothing
    downstream can detect that, so it has to be refused here.
    """
    bad = _policy(attributes=[
        {"name": "department", "field": "acl_department", "match": "any_of", "claims": {"entra": "groups"},
         "value_map": {"7c9f1b3e-2d4a-4a1c-9f6b-0b2d6e21a001": "HR"}, "drop_unmapped": True,
         "allowed_values": [{"value": "HR"}]},
    ], combine={"all_of": ["department"]})
    with pytest.raises(ValidationError, match="value_map"):
        AccessPolicy.model_validate(bad)


def test_allowed_values_pass_when_the_mapper_leaves_them_alone() -> None:
    """The same shape without drop_unmapped: "HR" passes through, so granting it is honest."""
    ok = _policy(attributes=[
        {"name": "department", "field": "acl_department", "match": "any_of", "claims": {"dev": "departments"},
         "allowed_values": [{"value": "HR", "label": "Human Resources", "description": "People and payroll."}]},
    ], combine={"all_of": ["department"]})
    policy = AccessPolicy.model_validate(ok)
    assert [v.value for v in policy.attribute("department").allowed_values] == ["HR"]
    assert policy.attribute("department").allowed_values[0].label == "Human Resources"


# ---------------------------------------------------------------- the caller's own directory object id


def test_a_principal_knows_its_directory_object_id() -> None:
    """`subject` is NOT the object id. ClaimsMapper prefers `sub`, which for Entra is a pairwise
    per-application identifier that exists nowhere in the directory. Anything comparing a caller against a
    Graph object - "you may not edit yourself" - must use this property, because comparing `subject` would
    never match and would therefore fail open.
    """
    p = Principal(subject="pairwise-sub-value", issuer_kind="entra",
                  raw_claims={"sub": "pairwise-sub-value", "oid": "11111111-2222-3333-4444-555555555555"})
    assert p.directory_object_id == "11111111-2222-3333-4444-555555555555"
    assert p.directory_object_id != p.subject, (
        "if these are ever equal this test is not proving anything - ClaimsMapper prefers `sub` over `oid`")


def test_a_dev_token_has_no_directory_object_id() -> None:
    """The dev issuer asserts whatever the developer typed, including roles. It cannot identify a real person,
    so a caller holding one must be refused rather than silently skipping the self-edit check."""
    p = Principal(subject="alice", issuer_kind="dev", raw_claims={"sub": "alice", "oid": "not-a-real-oid"})
    assert p.directory_object_id is None


def test_an_entra_token_without_an_oid_has_no_directory_object_id() -> None:
    p = Principal(subject="s", issuer_kind="entra", raw_claims={"sub": "s"})
    assert p.directory_object_id is None


def test_every_attribute_the_page_can_write_has_a_master_list() -> None:
    """An attribute fed by a directory extension is one Settings (Security) writes. Without a master list the
    page has nothing to offer, and the only alternative is a free-text box writing unvalidated values straight
    into the directory - which is how somebody ends up with a department no document is tagged with."""
    policy = FileConfigRepository(config_dir="./config").load_access_policy()
    writable = [a for a in policy.attributes if a.claims.get("entra", "").startswith("extn.")]
    assert writable, "no attribute reads a directory extension; re-point this test"
    for rule in writable:
        assert rule.allowed_values or rule.levels, (
            f"attribute {rule.name!r} is written by Settings (Security) but has neither allowed_values nor "
            f"levels, so there is nothing for an administrator to choose from")


def test_the_region_master_list_matches_the_hierarchy_facet() -> None:
    """region is `hierarchical`, so a caller's value is expanded to its ancestors through the facet tree. A
    value absent from that tree does not fail - _expand_ancestors falls through to [value], so a caller
    assigned it reaches UK and NOT EMEA or Global. Silently narrower access than the administrator granted,
    with nothing anywhere to explain it.
    """
    repo = FileConfigRepository(config_dir="./config")
    policy, facets = repo.load_access_policy(), repo.load_facets()
    region = policy.attribute("region")
    assert region.hierarchy_facet, "region stopped being hierarchical; re-point this test"
    tree = {v.id for f in facets.facets if f.name == region.hierarchy_facet for v in f.values}
    assert tree, f"facet {region.hierarchy_facet!r} has no values"
    orphans = sorted(v.value for v in region.allowed_values if v.value not in tree)
    assert not orphans, (
        f"allowed_values for region names {orphans}, which are not in the {region.hierarchy_facet} facet tree. "
        f"A caller assigned one reaches only that value, not its ancestors.")
