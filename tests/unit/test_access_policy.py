"""AccessPolicyEngine: semantics, default-deny, injection safety, and OData <-> predicate agreement."""

from __future__ import annotations

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
