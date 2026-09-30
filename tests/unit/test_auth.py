"""JWT validation (pinned algorithms, audience/issuer/lifetime) and policy-driven claim mapping."""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from rag_os.application.services.access_policy import AccessPolicyEngine
from rag_os.application.services.claims import ClaimsMapper
from rag_os.composition import Container
from rag_os.domain.access import AccessPolicy, AttributeRule, CombineRule, IssuerRoleConfig, MatchKind
from rag_os.domain.errors import AuthenticationFailed
from rag_os.infrastructure.auth.jwt_validator import IssuerConfig, JwtValidator
from rag_os.infrastructure.settings import Settings
from rag_os.infrastructure.storage.config_repo import FileConfigRepository

KEY = "dev-test-key-0123456789abcdef0123456789abcdef"
DEV_ISS = "rag-os-dev"
# The dev issuer is the only HS256 one. Entra (RS256/JWKS) is exercised at the bottom of this file with an
# in-test keypair and a stubbed JWKS client - it needs no tenant, which is the assumption that let a real
# Entra failure ship unnoticed.
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


# ======================================================================================================== Entra
# These used to be impossible here - "Entra (RS256/JWKS) cannot be exercised without a tenant" - which is why the
# failure that motivated them shipped. It is not true: the only things Entra supplies are an RSA keypair and a
# JWKS endpoint, and a test can own both. What it could not catch, and now does:
#
#   sign-in returned AADSTS65005 (a missing scope, fixed in the directory). Behind it sat a second 401 nobody
#   could see: the app registration's api.requestedAccessTokenVersion defaults to null, null means 1, and a v1
#   token says iss=https://sts.windows.net/<tid>/ where the API trusted only .../<tid>/v2.0. Setting the manifest
#   to 2 does not fix it either - that moves `aud` from api://<app-id> to the bare app id. Whichever single pair
#   the code picked, some correct directory configuration was rejected.
#
# So the point of the parametrised case below is the WHOLE matrix passing, not any one row.

TID = "c2ff8ba6-8824-4dbb-85d6-b12c6fc80d0c"
OTHER_TID = "11111111-2222-3333-4444-555555555555"
APP = "72f70e5a-291a-4c27-a6c9-1a7d1fbe7f9e"
OTHER_APP = "99999999-8888-7777-6666-555555555555"
V2_ISS = f"https://login.microsoftonline.com/{TID}/v2.0"
V1_ISS = f"https://sts.windows.net/{TID}/"
JWKS = f"https://login.microsoftonline.com/{TID}/discovery/v2.0/keys"

# Generated once: 2048-bit keygen is ~0.1 s and every test below can share it.
_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_RSA_PEM = _RSA.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())


class _StubJwks:
    """The only part of PyJWKClient the validator touches. Keeps every test off the network."""

    def __init__(self, key: object) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(key=self._key)


def entra_validator(public_key: object = None) -> JwtValidator:
    v = JwtValidator([IssuerConfig("entra", (V2_ISS, V1_ISS), (f"api://{APP}", APP), ("RS256",),
                                   jwks_url=JWKS, required=("exp", "iat", "iss", "aud"))])
    v._jwks[JWKS] = _StubJwks(public_key or _RSA.public_key())  # type: ignore[assignment]
    return v


def entra_token(iss: str = V2_ISS, aud: str = APP, *, key: object = None, alg: str = "RS256",
                **over: object) -> str:
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": "entra-user", "oid": "o1", "tid": TID,
              "iat": now, "exp": now + 600, **over}
    # None removes a claim rather than emitting a null one: PyJWT rejects "sub": null as InvalidSubjectError,
    # which is not the same thing as the claim being absent and would test the wrong branch.
    return jwt.encode({k: v for k, v in claims.items() if v is not None},
                      key or _RSA_PEM, algorithm=alg)  # type: ignore[arg-type]


@pytest.mark.parametrize("iss,aud,label", [
    (V1_ISS, f"api://{APP}", "v1 token: the exact shape that returned 401 untrusted issuer"),
    (V2_ISS, APP, "v2 token: what requestedAccessTokenVersion = 2 stamps"),
    (V1_ISS, APP, "v1 with the bare app id, which v1 also permits"),
    (V2_ISS, f"api://{APP}", "belt and braces - accepted rather than reasoned about"),
])
def test_every_issuer_and_audience_pairing_entra_may_stamp_is_accepted(iss: str, aud: str, label: str) -> None:
    claims, kind = entra_validator().validate(entra_token(iss=iss, aud=aud))
    assert kind == "entra" and claims["sub"] == "entra-user", label


@pytest.mark.parametrize("iss", [
    f"https://sts.windows.net/{OTHER_TID}/",
    f"https://login.microsoftonline.com/{OTHER_TID}/v2.0",
])
def test_another_tenant_is_still_rejected(iss: str) -> None:
    """Accepting two spellings must not have become "accept any Microsoft issuer". Same allow-list, two entries."""
    with pytest.raises(AuthenticationFailed):
        entra_validator().validate(entra_token(iss=iss))


@pytest.mark.parametrize("iss", [
    f"https://login.microsoftonline.com/{TID}",  # no /v2.0
    f"https://sts.windows.net/{TID}",            # no trailing slash - the slash is part of the real claim
    f"https://login.microsoftonline.com/{TID}/v2.0/",
    f"https://login.microsoftonline.com/{TID}/v2.0.evil.example",
])
def test_the_issuer_match_stays_exact(iss: str) -> None:
    """Guards against anyone "simplifying" the allow-list into a startswith or a regex later."""
    with pytest.raises(AuthenticationFailed):
        entra_validator().validate(entra_token(iss=iss))


@pytest.mark.parametrize("aud", [OTHER_APP, f"api://{OTHER_APP}", "api://", ""])
def test_an_audience_naming_another_application_is_rejected(aud: str) -> None:
    with pytest.raises(AuthenticationFailed):
        entra_validator().validate(entra_token(aud=aud))


def test_an_entra_token_signed_hs256_is_rejected() -> None:
    """The algorithm stays pinned per issuer: a token claiming our issuer but symmetrically signed is refused
    before any key is fetched, so a leaked JWKS document could not be turned into a signing key."""
    with pytest.raises(AuthenticationFailed):
        entra_validator().validate(entra_token(key=KEY, alg="HS256"))


def test_a_token_signed_by_a_different_key_is_rejected() -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    with pytest.raises(AuthenticationFailed):
        entra_validator().validate(entra_token(key=other_pem))


def test_entra_does_not_require_sub_but_still_requires_aud() -> None:
    """composition.py drops `sub` from the Entra required set on purpose; dropping it must not drop the rest."""
    assert entra_validator().validate(entra_token(sub=None))[1] == "entra"
    with pytest.raises(AuthenticationFailed):
        entra_validator().validate(jwt.encode(
            {"iss": V2_ISS, "iat": int(time.time()), "exp": int(time.time()) + 600},
            _RSA_PEM, algorithm="RS256"))  # type: ignore[arg-type]


def test_a_single_issuer_config_still_takes_plain_strings() -> None:
    """The regression guard for the trap in normalising these: a str is iterable, so tuple("rag-os-dev") would
    silently become ('r', 'a', 'g', ...) and the dev issuer would match nothing. Every other test here would
    still pass while dev sign-in was completely broken."""
    cfg = IssuerConfig("dev", DEV_ISS, "rag-os", ("HS256",), key=KEY)
    assert cfg.issuers == (DEV_ISS,) and cfg.audiences == ("rag-os",)
    assert JwtValidator([cfg]).validate(token())[1] == "dev"


def test_an_issuer_config_needs_at_least_one_non_empty_value() -> None:
    for bad in ({"issuer": ()}, {"issuer": ("",)}, {"audience": ()}, {"audience": ("api://a", "")}):
        with pytest.raises(ValueError):
            IssuerConfig("x", bad.get("issuer", "i"), bad.get("audience", "a"), ("HS256",),  # type: ignore[arg-type]
                         key=KEY)


def test_two_issuer_kinds_cannot_claim_the_same_issuer_url() -> None:
    """Silently letting one shadow the other would route a token to the wrong key and audience."""
    with pytest.raises(ValueError, match="claimed by both"):
        JwtValidator([IssuerConfig("dev", DEV_ISS, "rag-os", ("HS256",), key=KEY),
                      IssuerConfig("entra", (DEV_ISS, V2_ISS), APP, ("RS256",), jwks_url=JWKS)])


def test_issuer_kinds_lists_each_kind_once() -> None:
    v = JwtValidator([IssuerConfig("dev", DEV_ISS, "rag-os", ("HS256",), key=KEY),
                      IssuerConfig("entra", (V2_ISS, V1_ISS), APP, ("RS256",), jwks_url=JWKS)])
    assert v.issuer_kinds == ["dev", "entra"]


# ---------------------------------------------------------------- the two audience spellings come from one setting
@pytest.mark.parametrize("configured,expected", [
    (f"api://{APP}", (f"api://{APP}", APP)),
    (APP, (APP, f"api://{APP}")),
    (f"api://{TID}/{APP}", (f"api://{TID}/{APP}", APP, f"api://{APP}")),
    # Not an app-id URI, so there is no bare form to derive - taken literally rather than guessed at.
    ("https://contoso.com/productsapi", ("https://contoso.com/productsapi",)),
    (None, ()),
    ("", ()),
    ("   ", ()),
])
def test_both_audience_spellings_are_derived_from_one_setting(configured: str | None,
                                                             expected: tuple[str, ...]) -> None:
    s = Settings(_env_file=None, entra_audience=configured)  # type: ignore[call-arg]
    assert s.entra_audiences == expected


def test_the_client_id_is_not_silently_accepted_as_an_audience() -> None:
    """Where the SPA and the API are separate registrations the client id is a different application; accepting
    it would accept a token minted for something else entirely."""
    s = Settings(_env_file=None, entra_audience=f"api://{APP}",  # type: ignore[call-arg]
                 entra_client_id=OTHER_APP)
    assert OTHER_APP not in s.entra_audiences


def test_composition_trusts_both_issuer_urls_for_the_configured_tenant() -> None:
    validator = Container(Settings(  # type: ignore[call-arg]
        _env_file=None, dev_auth_enabled=False, entra_tenant_id=TID,
        entra_audience=f"api://{APP}", entra_client_id=APP,
        entra_api_scope=f"api://{APP}/access_as_user")).jwt
    assert validator.issuer_kinds == ["entra"]
    assert set(validator._by_iss) == {V2_ISS, V1_ISS}
    cfg = validator._by_iss[V1_ISS]
    assert cfg.audiences == (f"api://{APP}", APP) and cfg.algorithms == ("RS256",)
    assert "sub" not in cfg.required  # Entra tokens for an app+user flow need not carry it


# ---------------------------------------------------------------- directory-extension claim names
# Microsoft's optional-claims page states that a directory extension appears in a JWT as `extn.{name}`, and
# then shows `extension_{appid}_{name}` in its own worked example further down. The page contradicts itself,
# and the long form embeds the tenant's application id, so it cannot be a checked-in default. Both are
# accepted, because the failure mode otherwise is everything-configured-correctly-and-no-documents.

APP_ID = "9c1b3e4d5f6a7b8c9d0e1f2a3b4c5d6e"  # an appId with its hyphens stripped, as Entra writes it


def _extn_policy() -> AccessPolicy:
    return AccessPolicy(
        attributes=[
            AttributeRule(name="department", field="acl_department", match=MatchKind.ANY_OF, required=True,
                          claims={"entra": "extn.department"}),
            AttributeRule(name="clearance", field="acl_clearance", match=MatchKind.MAX_LEVEL,
                          claims={"entra": "extn.clearance"}),
        ],
        combine=CombineRule(all_of=["department", "clearance"]),
    )


def test_either_spelling_of_a_directory_extension_claim_is_accepted() -> None:
    m = ClaimsMapper(_extn_policy())
    short = m.map({"sub": "u", "extn.department": "HR", "extn.clearance": 2}, "entra")
    long = m.map({"sub": "u", f"extension_{APP_ID}_department": "HR",
                  f"extension_{APP_ID}_clearance": "2"}, "entra")
    assert short.attributes == {"department": ["HR"], "clearance": 2}
    assert long.attributes == short.attributes, "the two documented spellings must be indistinguishable"


def test_the_long_form_matches_regardless_of_the_case_it_was_registered_in() -> None:
    """Entra treats extension names as case-sensitive when the token service reads them, so a tenant that
    registered `Department` would never match a policy saying `department` if this were exact."""
    m = ClaimsMapper(_extn_policy())
    p = m.map({"sub": "u", f"extension_{APP_ID}_Department": "HR"}, "entra")
    assert p.attributes["department"] == ["HR"]


@pytest.mark.parametrize("claim", [
    "my_extension_clearance",            # merely contains the word
    "extension_clearance",               # no app id at all
    "extension_notanappid_clearance",    # app id is not 32 hex
    f"extension_{APP_ID}_clearance_2",   # a different attribute that shares a prefix
])
def test_a_claim_that_only_resembles_the_long_form_is_not_accepted(claim: str) -> None:
    """The tolerance is for one documented ambiguity, not a substring search over the token."""
    p = ClaimsMapper(_extn_policy()).map({"sub": "u", claim: "3"}, "entra")
    assert "clearance" not in p.attributes


def test_an_exact_match_wins_over_the_long_form() -> None:
    """A token carrying both spellings is pathological, but it must resolve predictably rather than by dict
    ordering - and the configured name is the one the operator wrote down."""
    claims = {"sub": "u", "extn.clearance": 1, f"extension_{APP_ID}_clearance": 3}
    assert ClaimsMapper(_extn_policy()).map(claims, "entra").attributes["clearance"] == 1


def test_every_entra_claim_in_the_shipped_policy_is_one_entra_can_emit() -> None:
    """The guard for the bug that prompted all of this.

    The policy used to name `extension_Department`, `extension_Region` and `extension_Clearance`. No Entra
    route emits those: a directory extension is `extn.<name>` (or the long form), `oid` and `roles` arrive by
    default, and `groups` is switched on with groupMembershipClaims. Since department and region are
    `required: true`, a name Entra never sends means every real caller sees nothing at all - with no error
    anywhere to say why.
    """
    policy = FileConfigRepository(config_dir="./config").load_access_policy()
    emitted = {"oid", "roles", "groups", "ctry", "email", "upn", "preferred_username"}
    for rule in policy.attributes:
        claim = rule.claims.get("entra")
        assert claim, f"attribute '{rule.name}' has no entra claim"
        assert claim in emitted or claim.startswith("extn."), (
            f"attribute '{rule.name}' reads the claim '{claim}', which Microsoft Entra does not emit. Use a "
            "directory extension (extn.<name>), a default claim, or groups with a value_map.")
