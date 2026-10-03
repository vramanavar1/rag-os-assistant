"""Filling in attribute claims an Entra token did not carry.

This exists for one account type and one documented platform behaviour. Microsoft's optional-claims reference:
"If your application manifest requests a custom extension and an MSA user logs in to your app, these extensions
aren't returned." So a Microsoft-account guest signs in successfully, carries no department and no region, and -
because both are required - reads nothing, while the admin page that set those values reads them back correctly
through Graph. Nothing is misconfigured and nothing raises.

These tests are mostly about what it must NOT do. It sits on the authentication path of every request, and it
reads the attributes that decide what a caller may read, so the ways it could be wrong are: granting access the
token did not justify, calling Graph when it did not need to, and turning a directory outage into an outage.
"""

from __future__ import annotations

from typing import Any

import pytest

from rag_os.application.services.claims import ClaimsMapper
from rag_os.application.services.directory_claims import DirectoryAttributes
from rag_os.domain.access import AccessPolicy
from rag_os.infrastructure.directory.fake import FakeDirectory, FakeUser

pytestmark = pytest.mark.anyio

OID = "11111111-2222-3333-4444-555555555555"
APP32 = "72f70e5a291a4c27a6c91a7d1fbe7f9e"

POLICY_DICT: dict[str, Any] = {
    "attributes": [
        {"name": "department", "field": "acl_department", "match": "any_of", "required": True,
         "claims": {"entra": "extn.department", "dev": "departments"}},
        {"name": "clearance", "field": "acl_clearance", "match": "max_level", "required": False,
         "claims": {"entra": "extn.clearance"},
         "levels": [{"value": 0, "label": "Public"}, {"value": 2, "label": "Confidential"}]},
        # No extension behind it: read straight from the token, so it must never trigger a directory read.
        {"name": "employee_id", "field": "acl_employee", "match": "any_of", "required": False,
         "claims": {"entra": "oid"}},
    ],
    "combine": {"all_of": ["department", "clearance"], "grant_any_of": ["employee_id"]},
    "roles": {"admin": ["rag.admin"]},
    "role_sources": {"trusted_for_roles": ["entra"], "role_claim": {"entra": "roles"}},
    "app_roles": [{"value": "rag.admin"}],
}


def policy(**over: Any) -> AccessPolicy:
    return AccessPolicy.model_validate({**POLICY_DICT, **over})


class CountingDirectory(FakeDirectory):
    """A FakeDirectory that records how many times the query path actually reached it."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def read_user(self, object_id: str) -> Any:
        self.reads += 1
        return await super().read_user(object_id)


def directory(**attributes: str) -> CountingDirectory:
    d = CountingDirectory()
    d.seed(FakeUser(object_id=OID, user_principal_name="x_yahoo.com#EXT#@contoso.onmicrosoft.com",
                    display_name="Guest", user_type="Guest", attributes=dict(attributes)))
    return d


def resolver(d: FakeDirectory, **over: Any) -> DirectoryAttributes:
    return DirectoryAttributes(d, policy(), **over)


async def test_a_token_without_the_claims_gets_them_from_the_directory() -> None:
    """The case this exists for: an MSA guest, whose token Entra will not put extn.* into."""
    d = directory(department="HR", clearance="2")
    claims = {"oid": OID, "sub": "pairwise"}
    filled = await resolver(d).enrich(claims, "entra")
    assert filled["extn.department"] == "HR"
    assert d.reads == 1


async def test_the_filled_values_go_through_the_normal_claims_mapping() -> None:
    """Injected into the CLAIMS, not onto the principal, so value_map, drop_unmapped and the numeric coercion
    all still apply. clearance is stored as the string "2" because every directory extension is declared String;
    the policy needs the int 2, and only ClaimsMapper knows that."""
    d = directory(department="HR", clearance="2")
    filled = await resolver(d).enrich({"oid": OID}, "entra")
    principal = ClaimsMapper(policy()).map(filled, "entra")
    assert principal.attributes["department"] == ["HR"]
    assert principal.attributes["clearance"] == 2, "a string from the directory must become the policy's integer"


async def test_a_claim_the_token_already_carries_is_never_overridden() -> None:
    """The security property. The issuer signed that value; the directory did not. A directory read must never
    be able to raise what a token asserted, or the signature stops being the thing that decides access."""
    d = directory(department="Finance", clearance="0")
    # Every extension-backed claim present, so there is nothing legitimate to fetch.
    claims = {"oid": OID, "extn.department": "HR", "extn.clearance": "2"}
    filled = await resolver(d).enrich(claims, "entra")
    assert filled["extn.department"] == "HR", "the signed claim wins over the directory's Finance"
    assert filled["extn.clearance"] == "2", "and so does the signed clearance over the directory's 0"
    assert d.reads == 0, "and nothing was read, because nothing was missing"


async def test_a_token_carrying_the_long_form_is_not_treated_as_missing() -> None:
    """Microsoft's own documentation gives both spellings, so ClaimsMapper accepts either. If presence were
    decided with `in claims` here, every caller whose token used the long form would hit Graph on every single
    request - a permanent cost for users who never needed the fallback at all."""
    d = directory(department="Finance")
    claims = {"oid": OID, f"extension_{APP32}_department": "HR", f"extension_{APP32}_clearance": "0"}
    filled = await resolver(d).enrich(claims, "entra")
    assert d.reads == 0
    assert ClaimsMapper(policy()).map(filled, "entra").attributes["department"] == ["HR"]


async def test_a_non_entra_token_never_reaches_the_directory() -> None:
    """A dev token's subject is self-asserted and means nothing in the tenant."""
    d = directory(department="HR")
    await resolver(d).enrich({"oid": OID, "departments": ["Sales"]}, "dev")
    assert d.reads == 0


async def test_a_token_with_no_object_id_is_left_alone() -> None:
    d = directory(department="HR")
    assert await resolver(d).enrich({"sub": "pairwise"}, "entra") == {"sub": "pairwise"}
    assert d.reads == 0


async def test_an_attribute_with_no_extension_behind_it_never_triggers_a_read() -> None:
    """employee_id is read from `oid`, which is in every token. Treating it as fillable would mean a Graph call
    on every request for an attribute that is never absent."""
    d = directory()
    await resolver(d).enrich({"oid": OID, "extn.department": "HR", "extn.clearance": "2"}, "entra")
    assert d.reads == 0


async def test_a_directory_outage_leaves_the_caller_with_what_the_token_had() -> None:
    """Fail closed, and never fail the request. The caller keeps the token's own claims - nothing, for the
    accounts this exists for - so an outage can only ever narrow access, never widen it, and never 500."""
    d = directory(department="HR")
    d.fail_on = "read_user"
    claims = {"oid": OID}
    assert await resolver(d).enrich(claims, "entra") == claims
    assert "department" not in ClaimsMapper(policy()).map(claims, "entra").attributes


async def test_a_second_request_inside_the_ttl_does_not_read_again() -> None:
    now = [1000.0]
    d = directory(department="HR")
    r = resolver(d, ttl_s=300.0, clock=lambda: now[0])
    await r.enrich({"oid": OID}, "entra")
    await r.enrich({"oid": OID}, "entra")
    assert d.reads == 1, "the cache is what keeps this off the hot path"
    now[0] += 301.0
    await r.enrich({"oid": OID}, "entra")
    assert d.reads == 2, "and it must expire, or an attribute change would never take effect"
