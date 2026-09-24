"""JWT validation (pinned algorithms, audience/issuer/lifetime) and policy-driven claim mapping."""

from __future__ import annotations

import time

import jwt
import pytest
from pydantic import ValidationError

from rag_os.application.services.access_policy import AccessPolicyEngine
from rag_os.application.services.claims import ClaimsMapper
from rag_os.domain.access import AccessPolicy, AttributeRule, CombineRule, IssuerRoleConfig, MatchKind
from rag_os.domain.errors import AuthenticationFailed
from rag_os.infrastructure.auth.jwt_validator import IssuerConfig, JwtValidator

KEY = "dev-test-key-0123456789abcdef0123456789abcdef"
DEV_ISS = "rag-os-dev"
# The dev issuer is the only HS256 one left; Entra (RS256/JWKS) cannot be exercised without a tenant.
V = JwtValidator([IssuerConfig("dev", DEV_ISS, "rag-os", ("HS256",), key=KEY, max_lifetime_s=3600)])


def token(**over: object) -> str:
    now = int(time.time())
    claims = {"iss": DEV_ISS, "aud": "rag-os", "sub": "u1", "iat": now, "exp": now + 600,
              "departments": ["HR"], **over}
    return jwt.encode(claims, KEY, algorithm="HS256")


def test_valid_token() -> None:
    claims, kind = V.validate(token())
    assert kind == "dev" and claims["sub"] == "u1"


@pytest.mark.parametrize("over", [
    {"aud": "someone-else"},
    {"iss": "evil"},
    {"iss": "https://login.microsoftonline.com/tid/v2.0"},  # a real issuer, but not one we trust here
    {"exp": int(time.time()) - 3600},
    {"exp": int(time.time()) + 7200},  # lifetime beyond the cap for this issuer
])
def test_rejected_claims(over: dict[str, object]) -> None:
    with pytest.raises(AuthenticationFailed):
        V.validate(token(**over))


def test_alg_none_and_wrong_key_rejected() -> None:
    now = int(time.time())
    unsigned = jwt.encode({"iss": DEV_ISS, "aud": "rag-os", "sub": "x", "iat": now, "exp": now + 60},
                          key=None, algorithm="none")
    with pytest.raises(AuthenticationFailed):
        V.validate(unsigned)
    forged = jwt.encode({"iss": DEV_ISS, "aud": "rag-os", "sub": "x", "iat": now, "exp": now + 60},
                        "another-key-0123456789abcdef0123456789abcdef", algorithm="HS256")
    with pytest.raises(AuthenticationFailed):
        V.validate(forged)


def test_malformed() -> None:
    with pytest.raises(AuthenticationFailed):
        V.validate("not-a-jwt")


POLICY = AccessPolicy(
    attributes=[
        AttributeRule(name="department", field="acl_department", claims={"dev": "departments", "entra": "dept"}),
        AttributeRule(name="clearance", field="acl_clearance", match=MatchKind.MAX_LEVEL, claims={"dev": "lvl"}),
    ],
    combine=CombineRule(all_of=["department"]),
    roles={"admin": ["rag.admin"]},
    # Only Entra may assert roles here, so the dev issuer stands in for "an issuer we do not trust for roles".
    role_sources=IssuerRoleConfig(trusted_for_roles=["entra"]),
)


def test_claims_mapping_per_issuer_and_role_trust() -> None:
    m = ClaimsMapper(POLICY)
    p = m.map({"sub": "u", "departments": ["HR"], "lvl": 2, "roles": ["rag.admin"]}, "dev")
    assert p.attributes == {"department": ["HR"], "clearance": 2}
    assert p.roles == set()  # an issuer outside trusted_for_roles can grant attributes, never privileges
    e = m.map({"oid": "o1", "dept": "Finance", "roles": ["rag.admin"]}, "entra")
    assert e.attributes == {"department": ["Finance"]} and e.is_admin


# --------------------------------------------------------------------- value_map (Entra groups -> values)

HR_GROUP = "7c9f1b3e-2d4a-4a1c-9f6b-0b2d6e21a001"
SALES_GROUP = "8ad02c4f-3e5b-4b2d-a07c-1c3e7f32b002"
UNRELATED_GROUP = "00000000-1111-2222-3333-444444444444"


def _group_policy(*, drop_unmapped: bool) -> AccessPolicy:
    return AccessPolicy(
        attributes=[
            AttributeRule(
                name="department",
                field="acl_department",
                claims={"entra": "groups"},
                value_map={HR_GROUP: "HR", SALES_GROUP: "Sales"},
                drop_unmapped=drop_unmapped,
            ),
        ],
        combine=CombineRule(all_of=["department"]),
    )


def test_value_map_translates_entra_group_ids_into_the_filter() -> None:
    """One group grants a whole department: the GUID never reaches a document or the search filter."""
    policy = _group_policy(drop_unmapped=True)
    p = ClaimsMapper(policy).map({"oid": "o1", "groups": [HR_GROUP, UNRELATED_GROUP]}, "entra")
    assert p.attributes == {"department": ["HR"]}  # the group that means nothing here is dropped
    decision = AccessPolicyEngine(policy).decide(p)
    assert decision.odata == "(acl_department/any(v: search.in(v, 'HR|*', '|')))"


def test_value_map_is_case_insensitive_and_passes_unmapped_values_through() -> None:
    p = ClaimsMapper(_group_policy(drop_unmapped=False)).map(
        {"oid": "o1", "groups": [HR_GROUP.upper(), "Legal"]}, "entra"
    )
    assert p.attributes == {"department": ["HR", "Legal"]}


def test_value_map_turns_a_label_into_a_level() -> None:
    policy = AccessPolicy(
        attributes=[
            AttributeRule(name="department", field="acl_department", claims={"dev": "departments"}),
            AttributeRule(
                name="clearance",
                field="acl_clearance",
                match=MatchKind.MAX_LEVEL,
                claims={"dev": "lvl"},
                value_map={"Public": "0", "Internal": "1", "Confidential": "2"},
            ),
        ],
        combine=CombineRule(all_of=["department", "clearance"]),
    )
    p = ClaimsMapper(policy).map({"sub": "u", "departments": ["HR"], "lvl": "Internal"}, "dev")
    assert p.attributes["clearance"] == 1
    assert "acl_clearance le 1" in (AccessPolicyEngine(policy).decide(p).odata or "")


def test_unmapped_non_integer_level_is_a_clean_401() -> None:
    """An IdP sending clearance: "Internal" with no map gets an authentication error, not a 500."""
    with pytest.raises(AuthenticationFailed, match="must be an integer"):
        ClaimsMapper(POLICY).map({"sub": "u", "departments": ["HR"], "lvl": "Internal"}, "dev")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value_map": {HR_GROUP: "HR|Sales"}},  # mapped value fails value_pattern
        {"value_map": {HR_GROUP: ""}},
        {"value_map": {"": "HR"}},  # empty key
        {"drop_unmapped": True},  # no map to drop against: would silently deny everyone
    ],
)
def test_unusable_value_map_is_rejected_at_load(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AttributeRule(name="department", field="acl_department", **kwargs)  # type: ignore[arg-type]


def test_value_map_on_a_level_must_map_to_integers() -> None:
    with pytest.raises(ValidationError):
        AttributeRule(
            name="clearance",
            field="acl_clearance",
            match=MatchKind.MAX_LEVEL,
            value_map={"Internal": "medium"},
        )
