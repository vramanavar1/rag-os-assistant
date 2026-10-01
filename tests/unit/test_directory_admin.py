"""DirectoryAdminService: what may be written, to whom, by whom, and what happens when a write half-lands.

These run against the hostile in-memory directory rather than a mock, so a rule that only appears to hold -
"we never grant twice" against a fake that silently deduplicates - cannot pass.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from rag_os.application.services.directory_admin import (
    PROPAGATION_NOTE,
    DirectoryAdminService,
    DirectoryWrite,
)
from rag_os.domain.access import AccessPolicy, Principal
from rag_os.domain.classification import FacetDef, FacetSchema, FacetValue
from rag_os.domain.errors import AccessDenied, Conflict, NotSupported, ValidationFailed
from rag_os.infrastructure.directory.fake import FakeDirectory, FakeUser

ACTOR_OID = "aaaaaaaa-0000-0000-0000-000000000001"
TARGET_OID = "bbbbbbbb-0000-0000-0000-000000000002"

REGION_FACET = FacetSchema(facets=[FacetDef(name="region", field="f_region", hierarchical=True, values=[
    FacetValue(id="Global"), FacetValue(id="EMEA", parent="Global"), FacetValue(id="UK", parent="EMEA"),
])])

POLICY_DICT: dict[str, Any] = {
    "attributes": [
        {"name": "department", "field": "acl_department", "match": "any_of", "required": True,
         "claims": {"entra": "extn.department"},
         "allowed_values": [{"value": "HR"}, {"value": "Finance"}]},
        {"name": "region", "field": "acl_region", "match": "hierarchical", "hierarchy_facet": "region",
         "required": True, "claims": {"entra": "extn.region"},
         "allowed_values": [{"value": "UK"}, {"value": "EMEA"}]},
        {"name": "clearance", "field": "acl_clearance", "match": "max_level",
         "claims": {"entra": "extn.clearance"},
         "levels": [{"value": 0, "label": "Public"}, {"value": 1, "label": "Internal"},
                    {"value": 2, "label": "Confidential"}]},
        {"name": "employee_id", "field": "acl_employee_id", "match": "exact", "claims": {"entra": "oid"}},
    ],
    "combine": {"all_of": ["department", "region", "clearance"], "grant_any_of": ["employee_id"]},
    "roles": {"admin": ["rag.admin"], "reviewer": ["rag.reviewer"], "contributor": ["rag.contributor", "rag.admin"]},
    "app_roles": [
        {"value": "rag.admin", "display_name": "Administrator", "description": "Everything."},
        {"value": "rag.reviewer", "display_name": "Reviewer", "description": "The review queue."},
        {"value": "rag.contributor", "display_name": "Contributor", "description": "Uploads."},
    ],
}


def policy(**over: Any) -> AccessPolicy:
    return AccessPolicy.model_validate({**POLICY_DICT, **over})


def service(directory: FakeDirectory, **over: Any) -> DirectoryAdminService:
    return DirectoryAdminService(directory, policy(**over), REGION_FACET)


def actor(*, oid: str | None = ACTOR_OID, issuer: str = "entra") -> Principal:
    claims = {"sub": "pairwise-value-not-the-oid"}
    if oid:
        claims["oid"] = oid
    return Principal(subject="pairwise-value-not-the-oid", issuer_kind=issuer, display_name="Ada",
                     roles={"admin"}, raw_claims=claims)


def fake(**over: Any) -> FakeDirectory:
    d = FakeDirectory()
    d.seed(FakeUser(object_id=ACTOR_OID, user_principal_name="ada@contoso.com", display_name="Ada"))
    d.seed(FakeUser(object_id=TARGET_OID, user_principal_name="priya@contoso.com", display_name="Priya",
                    mail="priya@contoso.com", **over))
    return d


async def apply(svc: DirectoryAdminService, write: DirectoryWrite, *, email: str = "priya@contoso.com") -> Any:
    etag = (await svc.describe(email))["etag"]
    return await svc.apply(actor(), email, write, etag)


# ---------------------------------------------------------------- the master lists


def test_only_attributes_backed_by_a_directory_extension_are_assignable() -> None:
    """employee_id is read from the `oid` claim. There is no extension behind it, so a write would succeed and
    change nothing at all - the sort of no-op that looks like a working feature."""
    svc = service(fake())
    assert [r.name for r in svc.writable] == ["department", "region", "clearance"]
    assert svc.extension_names() == {"department": "department", "region": "region", "clearance": "clearance"}


def test_the_capability_carries_the_master_lists_and_the_propagation_note() -> None:
    cap = service(fake()).capability()
    by_name = {a["name"]: a for a in cap["attributes"]}
    assert [v["value"] for v in by_name["department"]["values"]] == ["HR", "Finance"]
    assert [v["value"] for v in by_name["clearance"]["values"]] == ["0", "1", "2"], "levels are the master list"
    assert by_name["clearance"]["values"][1]["label"] == "Internal"
    assert by_name["department"]["clearable"] is False, "a required attribute cannot be cleared"
    assert cap["propagation_note"] == PROPAGATION_NOTE
    admin = next(r for r in cap["app_roles"] if r["value"] == "rag.admin")
    assert admin["needs_confirmation"] is True
    assert next(r for r in cap["app_roles"] if r["value"] == "rag.reviewer")["needs_confirmation"] is False


def test_a_region_value_missing_from_the_facet_tree_is_warned_about_not_silently_accepted() -> None:
    """_expand_ancestors falls through for an unknown value, so the caller reaches that region and none of its
    parents - narrower access than was granted, with nothing to explain it."""
    attrs = [dict(a) for a in POLICY_DICT["attributes"]]
    attrs[1] = {**attrs[1], "allowed_values": [{"value": "UK"}, {"value": "Atlantis"}]}
    warnings = service(fake(), attributes=attrs)._warnings()
    assert any("Atlantis" in w and "parents" in w for w in warnings), warnings


def test_an_attribute_with_no_master_list_says_the_running_policy_is_the_one_that_counts() -> None:
    """The symptom is an empty dropdown. The cause, on a deployed environment, is that the policy being read lives
    in the config store - a blob 08-bootstrap.ps1 seeds and then never overwrites - so a checkout carrying
    allowed_values is no evidence the running policy carries them. The warning has to say that, or the reader
    checks the file in front of them, sees the values, and goes looking somewhere else entirely."""
    attrs = [dict(a) for a in POLICY_DICT["attributes"]]
    attrs[0] = {k: v for k, v in attrs[0].items() if k != "allowed_values"}
    warnings = service(fake(), attributes=attrs)._warnings()
    hit = [w for w in warnings if "department" in w]
    assert hit, f"an attribute with no values must be warned about: {warnings}"
    assert "allowed_values" in hit[0], "name the key to add"
    assert "configuration store" in hit[0], (
        f"the reader has to be told the running policy may differ from their checkout: {hit[0]}")


# ---------------------------------------------------------------- who may write


@pytest.mark.anyio
async def test_an_admin_cannot_change_their_own_attributes_or_roles() -> None:
    """The guard has to compare the Entra object id. Principal.subject is the `sub` claim, which for Entra is a
    pairwise per-application value that exists nowhere in the directory - comparing it would never match, so
    this check would pass for everybody and an admin could raise their own clearance."""
    svc = service(fake())
    etag = (await svc.describe("ada@contoso.com"))["etag"]
    with pytest.raises(AccessDenied, match="your own"):
        await svc.apply(actor(), "ada@contoso.com", DirectoryWrite(attributes={"clearance": "2"}), etag)


@pytest.mark.anyio
async def test_the_self_edit_guard_is_not_comparing_the_subject_claim() -> None:
    """Proves the test above is testing something: the actor's subject and object id must differ, or a broken
    guard that compared `subject` would look correct."""
    a = actor()
    assert a.subject != a.directory_object_id
    assert a.directory_object_id == ACTOR_OID


@pytest.mark.anyio
async def test_a_dev_token_cannot_reach_the_directory_at_all() -> None:
    """A dev token asserts whatever a developer typed, roles included. With Graph write permissions in hand
    that is a tenant-wide escalation, so a token that cannot identify a directory object is refused rather than
    having the self-edit check quietly skipped."""
    svc = service(fake())
    etag = (await svc.describe("priya@contoso.com"))["etag"]
    with pytest.raises(AccessDenied, match="Entra sign-in"):
        await svc.apply(actor(issuer="dev"), "priya@contoso.com",
                        DirectoryWrite(attributes={"clearance": "1"}), etag)


@pytest.mark.anyio
async def test_an_entra_token_without_an_object_id_is_refused_too() -> None:
    svc = service(fake())
    etag = (await svc.describe("priya@contoso.com"))["etag"]
    with pytest.raises(AccessDenied, match="directory object"):
        await svc.apply(actor(oid=None), "priya@contoso.com", DirectoryWrite(attributes={"clearance": "1"}), etag)


# ---------------------------------------------------------------- what may be written


@pytest.mark.anyio
async def test_a_value_outside_the_master_list_is_refused_even_though_the_browser_sent_it() -> None:
    """The allow-list is enforced here, never by the page. A browser is not a security boundary."""
    with pytest.raises(ValidationFailed, match="not an allowed value"):
        await apply(service(fake()), DirectoryWrite(attributes={"department": "Executive"}))


@pytest.mark.anyio
async def test_the_allow_list_is_case_sensitive() -> None:
    """Document tags are matched case-sensitively, so "hr" would be a grant that reads nothing."""
    with pytest.raises(ValidationFailed, match="not an allowed value"):
        await apply(service(fake()), DirectoryWrite(attributes={"department": "hr"}))


@pytest.mark.anyio
async def test_a_clearance_outside_the_ladder_is_refused() -> None:
    """A caller carrying clearance 7 reads everything, and Account Information would render it as
    "documents classified 7 or below" - a level nothing is classified at and no rung describes."""
    with pytest.raises(ValidationFailed, match="outside the ladder"):
        await apply(service(fake()), DirectoryWrite(attributes={"clearance": "7"}))


@pytest.mark.anyio
async def test_an_attribute_whose_claim_is_not_an_extension_is_refused() -> None:
    with pytest.raises(NotSupported, match="employee_id"):
        await apply(service(fake()), DirectoryWrite(attributes={"employee_id": TARGET_OID}))


@pytest.mark.anyio
async def test_an_unknown_attribute_names_the_ones_that_are_assignable() -> None:
    with pytest.raises(NotSupported, match="assignable: department, region, clearance"):
        await apply(service(fake()), DirectoryWrite(attributes={"cost_center": "42"}))


@pytest.mark.anyio
async def test_a_required_attribute_cannot_be_cleared() -> None:
    """department is required: clearing it does not loosen this person's access, it removes all of it."""
    with pytest.raises(ValidationFailed, match="read nothing at all"):
        await apply(service(fake()), DirectoryWrite(attributes={"department": None}))


@pytest.mark.anyio
async def test_an_optional_attribute_can_be_cleared() -> None:
    d = fake(attributes={"clearance": "2"})
    result = await apply(service(d), DirectoryWrite(attributes={"clearance": None}))
    assert result.ok, result.failed
    assert result.state["attributes"]["clearance"] is None


@pytest.mark.anyio
async def test_clearance_is_written_as_a_string() -> None:
    """Every directory extension is declared as a string, and Graph rejects a mismatched JSON literal type.
    The fake refuses a non-string for the same reason, so this would fail loudly rather than in a real tenant."""
    d = fake()
    await apply(service(d), DirectoryWrite(attributes={"clearance": "2"}))
    assert d.users[TARGET_OID].attributes["clearance"] == "2"
    assert isinstance(d.users[TARGET_OID].attributes["clearance"], str)


# ---------------------------------------------------------------- roles


@pytest.mark.anyio
async def test_a_desired_role_set_grants_and_revokes_to_match() -> None:
    d = fake(assignments=["rag.reviewer"])
    result = await apply(service(d), DirectoryWrite(roles=["rag.contributor"]))
    assert result.ok, result.failed
    assert d.users[TARGET_OID].assignments == ["rag.contributor"]
    assert "revoked rag.reviewer" in result.applied and "granted rag.contributor" in result.applied


@pytest.mark.anyio
async def test_writing_the_same_role_set_twice_leaves_one_assignment() -> None:
    """Graph does not deduplicate - the fake does not either - so an idempotent write has to notice that the
    role is already held rather than granting it again."""
    d = fake()
    svc = service(d)
    await apply(svc, DirectoryWrite(roles=["rag.reviewer"]))
    await apply(svc, DirectoryWrite(roles=["rag.reviewer"]))
    assert d.users[TARGET_OID].assignments == ["rag.reviewer"]


@pytest.mark.anyio
async def test_a_pre_existing_duplicate_assignment_is_collapsed() -> None:
    """Somebody granted the role twice from a terminal. A desired-state write should converge on one row."""
    d = fake(assignments=["rag.reviewer", "rag.reviewer"])
    result = await apply(service(d), DirectoryWrite(roles=["rag.reviewer"]))
    assert result.ok, result.failed
    assert d.users[TARGET_OID].assignments == ["rag.reviewer"]
    assert any("duplicate" in line for line in result.applied), result.applied


@pytest.mark.anyio
async def test_omitting_roles_entirely_leaves_them_alone() -> None:
    """An attribute-only write must not revoke every role just because the field was absent."""
    d = fake(assignments=["rag.reviewer"])
    await apply(service(d), DirectoryWrite(attributes={"department": "HR"}))
    assert d.users[TARGET_OID].assignments == ["rag.reviewer"]


@pytest.mark.anyio
async def test_granting_an_administrator_role_needs_the_target_named_explicitly() -> None:
    """rag.admin bypasses the document access filter completely, and an admin can grant it to somebody who
    grants it back. It stays assignable, but not with one unremarkable click."""
    d = fake()
    with pytest.raises(ValidationFailed, match="bypasses the document access filter"):
        await apply(service(d), DirectoryWrite(roles=["rag.admin"]))
    assert d.users[TARGET_OID].assignments == []

    result = await apply(service(d), DirectoryWrite(roles=["rag.admin"], confirm="priya@contoso.com"))
    assert result.ok, result.failed
    assert d.users[TARGET_OID].assignments == ["rag.admin"]


@pytest.mark.anyio
async def test_confirming_with_the_wrong_person_does_not_count() -> None:
    with pytest.raises(ValidationFailed, match="type their user principal name"):
        await apply(service(fake()), DirectoryWrite(roles=["rag.admin"], confirm="someone.else@contoso.com"))


@pytest.mark.anyio
async def test_a_role_this_deployment_does_not_define_is_refused() -> None:
    with pytest.raises(ValidationFailed, match=re.escape("RoleManagement.ReadWrite.Directory")):
        await apply(service(fake()), DirectoryWrite(roles=["RoleManagement.ReadWrite.Directory"]))


@pytest.mark.anyio
async def test_a_role_held_through_a_group_is_reported_and_never_revoked_here() -> None:
    """The failure mode most likely to be reported as a security incident: a group-assigned role reaches the
    token exactly as a direct one does. If the page showed it as not held, an administrator would grant it
    (creating a second, direct assignment), later revoke that, and the person would still be an administrator."""
    d = fake(group_roles={"rag.admin": "HR Leads"})
    svc = service(d)
    state = await svc.describe("priya@contoso.com")
    row = next(r for r in state["roles"] if r["value"] == "rag.admin")
    assert row["via_group"] == "HR Leads" and row["removable"] is False

    result = await svc.apply(actor(), "priya@contoso.com", DirectoryWrite(roles=[]), state["etag"])
    assert result.ok, result.failed
    assert d.users[TARGET_OID].group_roles == {"rag.admin": "HR Leads"}, "a group grant is not ours to remove"
    assert not any("revoked rag.admin" in line for line in result.applied)


# ---------------------------------------------------------------- concurrency


@pytest.mark.anyio
async def test_a_write_without_an_if_match_is_refused() -> None:
    with pytest.raises(ValidationFailed, match="If-Match"):
        await service(fake()).apply(actor(), "priya@contoso.com", DirectoryWrite(roles=[]), "")


@pytest.mark.anyio
async def test_a_stale_etag_is_a_conflict_carrying_the_current_one() -> None:
    """Two tabs. B grants a role; A, loaded earlier, saves its own idea of the role set and would revoke it
    without anybody asking."""
    d = fake()
    svc = service(d)
    stale = (await svc.describe("priya@contoso.com"))["etag"]
    await apply(svc, DirectoryWrite(roles=["rag.reviewer"]))  # the other tab

    with pytest.raises(Conflict) as e:
        await svc.apply(actor(), "priya@contoso.com", DirectoryWrite(roles=[]), stale)
    assert e.value.detail["etag"] == (await svc.describe("priya@contoso.com"))["etag"]
    assert d.users[TARGET_OID].assignments == ["rag.reviewer"], "the stale write must not have landed"


@pytest.mark.anyio
async def test_a_duplicate_created_behind_the_pages_back_changes_the_etag() -> None:
    """Hashing only role names would hide a second assignment, which is a real state change."""
    d = fake(assignments=["rag.reviewer"])
    svc = service(d)
    before = (await svc.describe("priya@contoso.com"))["etag"]
    d.users[TARGET_OID].assignments.append("rag.reviewer")
    assert (await svc.describe("priya@contoso.com"))["etag"] != before


# ---------------------------------------------------------------- partial failure


@pytest.mark.anyio
async def test_a_failed_revoke_aborts_before_any_grant_is_applied() -> None:
    """Order is revokes, then attributes, then grants, so an abort leaves the person with LESS access than
    intended rather than more. Continuing past a failed revoke and granting anyway is the worst outcome
    available, which is why this does not run on a best-effort basis."""
    d = fake(assignments=["rag.reviewer"])
    d.fail_on["revoke_role"] = "Graph said no"
    result = await apply(service(d), DirectoryWrite(roles=["rag.contributor"],
                                                    attributes={"department": "HR"}))
    assert not result.ok
    assert result.applied == [], "nothing should have been attempted after the failure"
    assert d.users[TARGET_OID].assignments == ["rag.reviewer"], "the revoke did not happen"
    assert "rag.contributor" not in d.users[TARGET_OID].assignments, "and the grant must not have happened"
    assert "department" not in d.users[TARGET_OID].attributes, "nor the attribute write"


@pytest.mark.anyio
async def test_a_partial_failure_reports_what_landed_and_is_not_an_error() -> None:
    """Raising would discard the list of what already reached the directory, which is the only thing the
    administrator actually needs in order to decide what to do next."""
    d = fake(assignments=["rag.reviewer"])
    d.fail_on["grant_role"] = "Graph said no"
    result = await apply(service(d), DirectoryWrite(roles=["rag.contributor"],
                                                    attributes={"department": "Finance"}))
    assert not result.ok
    assert "revoked rag.reviewer" in result.applied
    assert any("set department" in line for line in result.applied)
    assert result.failed and "Graph said no" in result.failed[0]
    assert result.state["attributes"]["department"] == "Finance", "the state reflects what really happened"
    assert result.etag == result.state["etag"], "and the caller gets an etag it can retry with"


@pytest.mark.anyio
async def test_the_etag_after_a_partial_write_lets_the_caller_retry_without_a_reload() -> None:
    d = fake()
    d.fail_on["grant_role"] = "transient"
    first = await apply(service(d), DirectoryWrite(roles=["rag.reviewer"]))
    assert not first.ok
    d.fail_on.clear()
    second = await service(d).apply(actor(), "priya@contoso.com", DirectoryWrite(roles=["rag.reviewer"]),
                                    first.etag)
    assert second.ok, second.failed
    assert d.users[TARGET_OID].assignments == ["rag.reviewer"]


# ---------------------------------------------------------------- sessions and audit


@pytest.mark.anyio
async def test_sessions_are_only_revoked_when_asked() -> None:
    d = fake()
    await apply(service(d), DirectoryWrite(attributes={"department": "HR"}))
    assert d.users[TARGET_OID].sessions_revoked == 0, "signing somebody out of the whole tenant is opt-in"
    await apply(service(d), DirectoryWrite(attributes={"department": "Finance"}, revoke_sessions=True))
    assert d.users[TARGET_OID].sessions_revoked == 1


@pytest.mark.anyio
async def test_every_write_emits_an_audit_record_with_before_and_after(
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = fake(attributes={"department": "HR"})
    with caplog.at_level(logging.INFO, logger="rag_os.audit"):
        await apply(service(d), DirectoryWrite(attributes={"department": "Finance"}, roles=["rag.reviewer"]))
    record = next(r for r in caplog.records if r.name == "rag_os.audit")
    assert record.attributes_changed == {"department": ["HR", "Finance"]}
    assert record.roles_granted == ["rag.reviewer"]
    assert record.actor_object_id == ACTOR_OID
    assert record.target_object_id == TARGET_OID
    assert record.target == "priya@contoso.com"


@pytest.mark.anyio
async def test_a_partial_failure_is_audited_too(caplog: pytest.LogCaptureFixture) -> None:
    """The half-applied write is the one somebody will need to reconstruct later."""
    d = fake()
    d.fail_on["grant_role"] = "Graph said no"
    with caplog.at_level(logging.INFO, logger="rag_os.audit"):
        await apply(service(d), DirectoryWrite(attributes={"department": "HR"}, roles=["rag.reviewer"]))
    record = next(r for r in caplog.records if r.name == "rag_os.audit")
    assert record.levelno == logging.ERROR
    assert record.attributes_changed == {"department": [None, "HR"]}
    assert record.failed and "Graph said no" in record.failed[0]


@pytest.mark.anyio
async def test_granting_an_administrator_role_is_audited_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    d = fake()
    with caplog.at_level(logging.INFO, logger="rag_os.audit"):
        await apply(service(d), DirectoryWrite(roles=["rag.admin"], confirm="priya@contoso.com"))
    record = next(r for r in caplog.records if r.name == "rag_os.audit")
    assert record.levelno == logging.WARNING, "the most consequential grant should not read like routine INFO"
    assert record.roles_granted == ["rag.admin"]


# ---------------------------------------------------------------- reading a person


@pytest.mark.anyio
async def test_describe_reports_an_unset_attribute_as_null_not_missing() -> None:
    state = await service(fake(attributes={"department": "HR"})).describe("priya@contoso.com")
    assert state["attributes"] == {"department": "HR", "region": None, "clearance": None}


@pytest.mark.anyio
async def test_a_guest_is_identified_as_one() -> None:
    d = FakeDirectory()
    d.seed(FakeUser(object_id=TARGET_OID, user_principal_name="p_x.com#EXT#@t.onmicrosoft.com",
                    mail="p@x.com", user_type="Guest"))
    state = await service(d).describe("p@x.com")
    assert state["user_type"] == "Guest"
    assert state["object_id"] == TARGET_OID


# ---------------------------------------------------------------- wiring
# These belong with the rules rather than with general settings tests: each one is a way the feature could be
# switched on and be dangerous, or switched off and look broken.


def _container(**over: Any) -> Any:
    from rag_os.composition import Container
    from rag_os.infrastructure.settings import Settings

    base: dict[str, Any] = dict(
        _env_file=None, app_env="test", config_store="filesystem", config_dir="./config",
        search_backend="in_memory", queue="in_memory", embedder="fake", llm="fake", dev_auth_enabled=False,
    )
    return Container(Settings(**{**base, **over}))


def test_a_directory_is_off_unless_a_deployment_asks_for_one() -> None:
    """It holds Graph permissions that can rewrite any user in the tenant, so it is opt-in per deployment."""
    c = _container()
    assert c.directory is None
    assert c.directory_admin.enabled is False


def test_the_page_still_describes_itself_with_no_directory_configured() -> None:
    """Otherwise the only thing an administrator sees is a bare 501, which does not say what to turn on."""
    cap = _container().directory_admin.capability()
    assert cap["enabled"] is False
    assert cap["attributes"], "the master lists come from the policy, not from the directory"


@pytest.mark.anyio
async def test_reading_a_person_without_a_directory_says_what_to_configure() -> None:
    """Naming the setting is not enough on its own: an operator who read the earlier version of this message still
    had to ask where DIRECTORY goes, because in this deployment model nobody edits a container env var by hand."""
    with pytest.raises(NotSupported, match="DIRECTORY=graph") as excinfo:
        await _container().directory_admin.describe("priya@contoso.com")
    detail = str(excinfo.value)
    assert ".psd1" in detail, "say which file the setting lives in"
    assert "07-container-apps.ps1" in detail, "and what pushes it to the running app"
    assert "DEV_AUTH_ENABLED" in detail, "a hard refusal at construction, so it belongs in the same breath"
    assert "9.3" in detail, "point at the runbook rather than restating half of it"
    # The regression this guards: ENTRA_SERVICE_PRINCIPAL_OBJECT_ID used to be phrased as something to set, and
    # step 07 already passes it from the outputs file. Mentioning it is fine; instructing it wastes an afternoon.
    assert not re.search(r"(?:[Ss]et|[Aa]dd|with)\s+ENTRA_SERVICE_PRINCIPAL_OBJECT_ID", detail), (
        f"step 07 supplies this; do not instruct the operator to set it: {detail}")


def test_directory_writes_and_dev_tokens_cannot_be_enabled_together() -> None:
    """The dev issuer is trusted for roles and ships with a default signing key, so anyone who can reach the API
    can mint an admin token. Reading documents with one is a convenience; writing the directory with one is a
    tenant-wide escalation."""
    from rag_os.domain.errors import ConfigError

    with pytest.raises(ConfigError, match="DEV_AUTH_ENABLED"):
        _ = _container(directory="fake", dev_auth_enabled=True).directory


def test_the_fake_directory_cannot_be_selected_outside_tests() -> None:
    """It accepts every write and performs none - a grant that silently does nothing, which is worse than an
    outage because nobody goes looking for it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="APP_ENV=test"):
        _container(directory="fake", app_env="prod")


def test_reloading_the_policy_rebuilds_the_master_lists() -> None:
    """The service caches the policy's allowed_values, and the adapter caches the extension names derived from
    its claims. A hot reload that left either behind would validate against a policy no longer in force."""
    c = _container()
    first = c.directory_admin
    c._apply(c.domain)
    assert c.directory_admin is not first
