"""GraphDirectory against a stubbed Microsoft Graph.

Every test here pins a Graph behaviour that fails *quietly* if you get it wrong - the write lands on the wrong
user, or a role looks unheld, or a value reads as unset. None of them would surface as an exception in
production, which is why they are worth the stub.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from rag_os.domain.errors import ConfigError, Conflict, DependencyUnavailable, NotFound
from rag_os.infrastructure.directory.graph import GraphDirectory

APP_ID = "b7d8e648-520f-41d3-b9c0-fdeb91768a0a"
APP32 = APP_ID.replace("-", "")
SP_ID = "sp-object-id"
ATTRS = {"department": "department", "region": "region", "clearance": "clearance"}
ROLE_IDS = {"rag.admin": "role-admin-guid", "rag.sme": "role-sme-guid"}
# The adapter reads these off our own service principal rather than taking them from configuration, so the
# registration is the authority on which roles exist and which are still enabled.
SP_ROLES = {"appRoles": [{"value": v, "id": i, "isEnabled": True} for v, i in ROLE_IDS.items()]}
OID = "11111111-2222-3333-4444-555555555555"


class Graph:
    """A stub that records every request, so a test can assert on the URL as well as the outcome."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self.routes = routes or {}

    @staticmethod
    def _path(request: httpx.Request) -> str:
        """The path without the API-version prefix httpx merges in from base_url."""
        return request.url.path.removeprefix("/v1.0")

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            path = self._path(request)
            # Routes are keyed on (method, path), so a query string used to go entirely unexamined - which is how
            # a $filter real Graph rejects shipped and passed every test here. appRoleAssignedTo does not support
            # $filter on principalId/appRoleId/resourceId in EITHER literal form, so refuse it the way Graph does.
            if path.endswith("/appRoleAssignedTo") and b"$filter" in request.url.query:
                return httpx.Response(400, json={"error": {"code": "Request_BadRequest", "message": (
                    "Invalid filter clause: A binary operator with incompatible types was detected. Found "
                    "operand types 'Edm.Guid' and 'Edm.String' for operator kind 'Equal'.")}})
            key = (request.method, path)
            body = self.routes.get(key, ...)
            if body is ...:
                return httpx.Response(204)
            if isinstance(body, list):  # a queue of successive responses for the same route
                body = body.pop(0)
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, json=body)

        return httpx.MockTransport(handler)

    def paths(self) -> list[str]:
        return [self._path(r) for r in self.seen]

    def query_of(self, method: str, path: str) -> dict[str, list[str]]:
        """Parsed query parameters. parse_qs does the percent-decoding, so a value that had to be encoded to
        survive the URL (a guest's '#', say) reads back as it was written."""
        for r in self.seen:
            if r.method == method and self._path(r) == path:
                return parse_qs(r.url.query.decode())
        raise AssertionError(f"no {method} {path} in {self.paths()}")

    def bodies(self, method: str, path_contains: str = "") -> list[Any]:
        return [
            json.loads(r.content)
            for r in self.seen
            if r.method == method and path_contains in self._path(r) and r.content
        ]


def directory(graph: Graph, **over: Any) -> GraphDirectory:
    graph.routes.setdefault(("GET", f"/servicePrincipals/{SP_ID}"), SP_ROLES)
    kwargs: dict[str, Any] = dict(
        extension_app_id=APP_ID, service_principal_object_id=SP_ID, attribute_names=ATTRS,
        app_role_values=tuple(ROLE_IDS), transport=graph.transport(), token_provider=lambda: "a-token",
    )
    kwargs.update(over)
    return GraphDirectory(**kwargs)


def user_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": OID, "userPrincipalName": "priya@contoso.com", "displayName": "Priya",
        "mail": "priya@contoso.com", "accountEnabled": True, "userType": "Member",
        f"extension_{APP32}_department": "HR", f"extension_{APP32}_clearance": "1",
    }
    row.update(over)
    return row


# ---------------------------------------------------------------- configuration refusals


def test_a_non_guid_extension_app_id_is_refused_at_construction() -> None:
    """Extension property names embed the owning app's id. Without a usable one the adapter cannot name a
    single attribute, and every read would silently return "no value set"."""
    with pytest.raises(ConfigError, match="ENTRA_EXTENSION_APP_ID"):
        GraphDirectory(extension_app_id="https://contoso.com/api", service_principal_object_id=SP_ID)


def test_a_missing_service_principal_object_id_is_refused_at_construction() -> None:
    """App-role assignments hang off the enterprise application's object id, which is not the client id - the
    trap Set-EntraAppRoleAssignment.ps1's header warns about."""
    with pytest.raises(ConfigError, match="ENTRA_SERVICE_PRINCIPAL_OBJECT_ID"):
        GraphDirectory(extension_app_id=APP_ID, service_principal_object_id="")


# ---------------------------------------------------------------- addressing a user


@pytest.mark.anyio
async def test_a_guest_upn_is_never_placed_in_a_url_path() -> None:
    """The one that corrupts data silently.

    A B2B guest's UPN is priya_contoso.com#EXT#@tenant.onmicrosoft.com. "#" begins a URL fragment, so a UPN
    interpolated into a path makes httpx request /users/priya_contoso.com and discard the rest - landing the
    PATCH on a different user, or on none, with no error either way. So the address may only ever appear inside
    a $filter, and every later call must use the object id.
    """
    guest = "priya_contoso.com#EXT#@tenant.onmicrosoft.com"
    g = Graph({("GET", "/users"): {"value": [user_row(userPrincipalName=guest, userType="Guest")]}})
    d = directory(g)

    found = await d.find_user(guest)
    assert found.object_id == OID
    assert found.user_type == "Guest", "an administrator should see they are extending a guest's access"

    await d.set_attributes(found.object_id, {"department": "Finance"})
    for request in g.seen:
        assert "EXT" not in request.url.path, f"a UPN reached a request path: {request.url.path}"
    assert f"/users/{OID}" in g.paths(), "the write must address the object id"

    # And the whole address must survive into the filter. Asserting only that "#" is absent from the URL would
    # pass while the request was broken: httpx strips the fragment before dispatch, so a truncated filter looks
    # clean from here. The decoded query is the only honest check.
    flt = g.query_of("GET", "/users")["$filter"][0]
    assert guest in flt, f"the guest address was truncated in the filter: {flt}"
    assert flt.endswith("'"), f"the filter literal is not closed: {flt}"


@pytest.mark.anyio
async def test_a_quote_in_an_email_cannot_alter_the_odata_filter() -> None:
    """An unescaped quote turns a lookup into a directory-enumeration primitive."""
    g = Graph({("GET", "/users"): {"value": []}})
    with pytest.raises(NotFound):
        await directory(g).find_user("x' or startsWith(userPrincipalName,'a")
    flt = g.query_of("GET", "/users")["$filter"][0]
    assert "''" in flt, "the quote must be doubled"
    # Counting " or " in the whole filter would count the one inside the literal, which is harmless. What
    # matters is that nothing the caller typed survives OUTSIDE a string literal, so strip the literals out
    # ('' is an escaped quote inside one) and check the skeleton that is left is still two clauses.
    skeleton = re.sub(r"'(?:[^']|'')*'", "?", flt)
    assert skeleton == "userPrincipalName eq ? or mail eq ?", f"injected text escaped the literal: {skeleton}"


@pytest.mark.anyio
async def test_an_address_is_matched_against_mail_as_well_as_the_principal_name() -> None:
    """A very ordinary tenant has mail=first.last@contoso.com and upn=12345@contoso.onmicrosoft.com. Filtering
    on the principal name alone finds nobody for most of its users."""
    g = Graph({("GET", "/users"): {"value": []}})
    with pytest.raises(NotFound, match="group"):  # and the 404 names the group route
        await directory(g).find_user("first.last@contoso.com")
    flt = g.query_of("GET", "/users")["$filter"][0]
    assert "userPrincipalName eq" in flt and "mail eq" in flt


@pytest.mark.anyio
async def test_two_users_sharing_an_address_is_a_conflict_not_a_first_row() -> None:
    """Entra does not require `mail` to be unique, and a guest's mail is their external address - so
    collisions are likely, and picking one would write somebody's clearance onto a stranger."""
    g = Graph({("GET", "/users"): {"value": [user_row(), user_row(id="other", userPrincipalName="other@x.com")]}})
    with pytest.raises(Conflict, match="matches 2 users"):
        await directory(g).find_user("priya@contoso.com")


@pytest.mark.anyio
async def test_every_directory_extension_is_named_in_select() -> None:
    """The most common false alarm in this area: without $select a directory extension is simply absent from
    the response, which reads as "no value set" rather than as a missing query parameter."""
    g = Graph({("GET", "/users"): {"value": [user_row()]}})
    found = await directory(g).find_user("priya@contoso.com")
    selected = g.query_of("GET", "/users")["$select"][0]
    for short in ATTRS.values():
        assert f"extension_{APP32}_{short}" in selected, f"{short} is not selected, so it can never be read"
    assert found.attributes == {"department": "HR", "clearance": "1"}, "keyed by policy name, absent when unset"
    assert "region" not in found.attributes, "an unset attribute is absent, not empty"


@pytest.mark.anyio
async def test_the_extension_names_preserve_the_configured_casing() -> None:
    """Entra ignores case when a value is set but the token service matches case-sensitively when reading, so
    a value written under the wrong spelling never reaches a claim and nothing reports it."""
    g = Graph({("GET", "/users"): {"value": [user_row()]}})
    d = directory(g, attribute_names={"clearance": "Clearance"})
    await d.set_attributes(OID, {"clearance": "2"})
    assert g.bodies("PATCH")[0] == {f"extension_{APP32}_Clearance": "2"}


# ---------------------------------------------------------------- writing attributes


@pytest.mark.anyio
async def test_clearance_is_sent_as_a_json_string() -> None:
    """Every RAG-OS extension is declared dataType String. Graph rejects a JSON literal whose type does not
    match the declaration, so sending 1 instead of "1" is a 400 - and the port types this out on purpose."""
    g = Graph()
    await directory(g).set_attributes(OID, {"clearance": "1"})
    body = g.bodies("PATCH")[0]
    assert body == {f"extension_{APP32}_clearance": "1"}
    assert isinstance(body[f"extension_{APP32}_clearance"], str)


@pytest.mark.anyio
async def test_clearing_a_value_sends_null() -> None:
    g = Graph()
    await directory(g).set_attributes(OID, {"region": None})
    assert g.bodies("PATCH")[0] == {f"extension_{APP32}_region": None}


@pytest.mark.anyio
async def test_an_attribute_with_no_configured_extension_is_refused() -> None:
    with pytest.raises(ConfigError, match="cost_center"):
        await directory(Graph()).set_attributes(OID, {"cost_center": "42"})


@pytest.mark.anyio
async def test_an_empty_write_makes_no_request_at_all() -> None:
    g = Graph()
    await directory(g).set_attributes(OID, {})
    assert g.seen == []


# ---------------------------------------------------------------- roles


@pytest.mark.anyio
async def test_role_assignments_beyond_the_first_page_are_not_lost() -> None:
    """appRoleAssignedTo pages at 100. Stopping at page one reports "holds no roles" for anyone further down,
    after which a grant creates a second assignment - and Graph does not deduplicate, so a later revoke of the
    visible one leaves the person still holding the role."""
    page2 = "https://graph.microsoft.com/v1.0/servicePrincipals/sp-object-id/appRoleAssignedTo?$skiptoken=x"
    g = Graph({("GET", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): [
        {"value": [{"id": "a1", "appRoleId": "role-sme-guid", "principalType": "User", "principalId": OID}],
         "@odata.nextLink": page2},
        {"value": [{"id": "a2", "appRoleId": "role-admin-guid", "principalType": "User", "principalId": OID}]},
    ]})
    roles = await directory(g).list_roles(OID)
    assert sorted(r.role_value for r in roles) == ["rag.admin", "rag.sme"], "page two was dropped"


@pytest.mark.anyio
async def test_a_repeated_assignment_is_reported_as_a_duplicate() -> None:
    """Graph does not deduplicate, so this state is reachable and has to be visible."""
    g = Graph({("GET", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): {"value": [
        {"id": "a1", "appRoleId": "role-sme-guid", "principalId": OID},
        {"id": "a2", "appRoleId": "role-sme-guid", "principalId": OID},
    ]}})
    roles = await directory(g).list_roles(OID)
    assert [(r.role_value, r.duplicates) for r in roles] == [("rag.sme", 2)]


@pytest.mark.anyio
async def test_revoking_a_role_removes_every_duplicate_assignment() -> None:
    # Somebody else's rag.sme sits in the same collection. The narrowing is client-side now, so a revoke that
    # ignored principalId would strip a role from a person the administrator never named.
    g = Graph({("GET", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): {"value": [
        {"id": "a1", "appRoleId": "role-sme-guid", "principalId": OID},
        {"id": "a2", "appRoleId": "role-sme-guid", "principalId": OID},
        {"id": "a3", "appRoleId": "role-admin-guid", "principalId": OID},
        {"id": "b1", "appRoleId": "role-sme-guid", "principalId": "someone-else"},
    ]}})
    removed = await directory(g).revoke_role(OID, "rag.sme")
    assert removed == 2
    deleted = [p.rsplit("/", 1)[-1] for p in g.paths() if p.endswith(("a1", "a2", "a3", "b1"))]
    assert sorted(deleted) == ["a1", "a2"], "both duplicates go; the other role and the other person are untouched"


@pytest.mark.anyio
async def test_another_persons_assignment_is_not_attributed_to_this_one() -> None:
    """The narrowing by principalId moved from Graph into Python, because appRoleAssignedTo rejects a $filter on
    that property in either literal form. A comparison that over-matched would report every person as holding
    everyone else's roles - rag.admin included, which bypasses the document filter entirely. That failure is
    silent and grants access, so it gets its own test rather than riding on the duplicate one."""
    g = Graph({("GET", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): {"value": [
        {"id": "a1", "appRoleId": "role-sme-guid", "principalType": "User", "principalId": OID},
        {"id": "b1", "appRoleId": "role-admin-guid", "principalType": "User", "principalId": "someone-else"},
        {"id": "b2", "appRoleId": "role-sme-guid", "principalType": "User", "principalId": None},
    ]}})
    roles = await directory(g).list_roles(OID)
    assert [(r.role_value, r.duplicates) for r in roles] == [("rag.sme", 1)], (
        "only the looked-up principal's row counts; a row for somebody else, or one with no principalId at all, "
        "must not be attributed to them")


@pytest.mark.anyio
async def test_a_lookup_reads_the_assignment_collection_once() -> None:
    """It used to read it twice - once through the broken $filter and once unfiltered for the group sweep. The
    collection is every assignment on our service principal, so a second full read is pure cost on a large
    tenant, and nothing else would notice it had come back."""
    g = Graph({
        ("GET", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): {"value": [
            {"id": "g1", "appRoleId": "role-admin-guid", "principalType": "Group",
             "principalId": "group-oid", "principalDisplayName": "HR Leads"}]},
        ("GET", f"/users/{OID}/transitiveMemberOf/microsoft.graph.group"): {"value": [{"id": "group-oid"}]},
    })
    await directory(g).list_roles(OID)
    reads = [pth for pth in g.paths() if pth.endswith("/appRoleAssignedTo")]
    assert len(reads) == 1, f"the collection was read {len(reads)} times: {reads}"


@pytest.mark.anyio
async def test_a_grant_always_names_our_own_service_principal() -> None:
    """AppRoleAssignment.ReadWrite.All is not scoped to one application: it permits granting any app role on
    any service principal in the tenant, Graph's own included. This is the only narrowing that exists."""
    g = Graph({("POST", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): httpx.Response(201, json={"id": "n"})})
    await directory(g).grant_role(OID, "rag.sme")
    assert g.bodies("POST")[0] == {"principalId": OID, "resourceId": SP_ID, "appRoleId": "role-sme-guid"}


@pytest.mark.anyio
@pytest.mark.parametrize("role", ["RoleManagement.ReadWrite.Directory", "rag.typo"])
async def test_a_role_outside_our_own_catalogue_is_never_assigned(role: str) -> None:
    g = Graph()
    with pytest.raises(ConfigError, match="not an application role"):
        await directory(g).grant_role(OID, role)
    assert g.seen == [], "nothing may reach Graph before the role is recognised"


@pytest.mark.anyio
async def test_a_role_held_through_a_group_is_reported_and_marked_unremovable() -> None:
    """A group-assigned app role reaches the token exactly as a direct one does. Omitting it would show "not
    held" for an administrator, and the direct assignment somebody then creates and later revokes would not
    take the role away."""
    assignments = {"value": [{"id": "g1", "appRoleId": "role-admin-guid", "principalType": "Group",
                              "principalId": "group-oid", "principalDisplayName": "HR Leads"}]}
    g = Graph({
        ("GET", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): assignments,
        ("GET", f"/users/{OID}/transitiveMemberOf/microsoft.graph.group"): {"value": [{"id": "group-oid"}]},
    })
    roles = await directory(g).list_roles(OID)
    assert [(r.role_value, r.via_group, r.removable) for r in roles] == [("rag.admin", "HR Leads", False)]


@pytest.mark.anyio
async def test_a_group_role_the_caller_is_not_a_member_of_is_not_reported() -> None:
    assignments = {"value": [{"id": "g1", "appRoleId": "role-admin-guid", "principalType": "Group",
                              "principalId": "someone-elses-group", "principalDisplayName": "Finance"}]}
    g = Graph({
        ("GET", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): assignments,
        ("GET", f"/users/{OID}/transitiveMemberOf/microsoft.graph.group"): {"value": [{"id": "mine"}]},
    })
    assert await directory(g).list_roles(OID) == []


# ---------------------------------------------------------------- failure handling


@pytest.mark.anyio
async def test_a_refusal_names_the_one_call_and_the_one_permission() -> None:
    """A 403 that lists every permission the application uses is barely better than none: it was answered three
    separate times by re-checking permissions that were already granted. The adapter knows the method and the URL
    at the point of refusal, so it names them and the single permission that governs that call."""
    g = Graph({("PATCH", f"/users/{OID}"): httpx.Response(403, json={"error": {"message": "Insufficient privileges"}})})
    with pytest.raises(DependencyUnavailable) as e:
        await directory(g).set_attributes(OID, {"department": "HR"})
    said = str(e.value)
    assert "PATCH" in said and "/users/" in said, f"name the call that was refused: {said}"
    assert "User.ReadWrite.All" in said, "and the permission that governs it"
    assert "AppRoleAssignment.ReadWrite.All" not in said, (
        f"a PATCH on a user has nothing to do with app-role assignments; listing it sends the reader to "
        f"re-check a permission that is not involved: {said}")
    assert "restart" in said, "the cached Graph token is the other half of the usual cause"


@pytest.mark.anyio
async def test_a_refused_role_read_blames_the_read_permission_not_the_write_one() -> None:
    """The distinction that cost a full debugging round: AppRoleAssignment.ReadWrite.All creates and deletes an
    assignment but cannot read one, because reading it is a read of the service principal."""
    g = Graph({("GET", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"):
               httpx.Response(403, json={"error": {"message": "Insufficient privileges"}})})
    with pytest.raises(DependencyUnavailable) as e:
        await directory(g).list_roles(OID)
    said = str(e.value)
    assert "Application.Read.All" in said, f"the read is governed by Application.Read.All: {said}"
    assert "only writes" in said or "NOT AppRoleAssignment" in said, (
        f"say why the write permission does not cover it, or the reader grants it again: {said}")


@pytest.mark.anyio
async def test_a_throttle_is_retried_and_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    waited: list[float] = []

    async def fake_sleep(s: float) -> None:
        waited.append(s)

    monkeypatch.setattr("rag_os.infrastructure.directory.graph.asyncio.sleep", fake_sleep)
    g = Graph({("PATCH", f"/users/{OID}"): [
        httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(204),
    ]})
    await directory(g).set_attributes(OID, {"department": "HR"})
    assert waited == [7.0], "Graph's own throttle window beats a guess"


@pytest.mark.anyio
async def test_an_absurd_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request thread must not be parked for an hour because a header said so."""
    waited: list[float] = []

    async def fake_sleep(s: float) -> None:
        waited.append(s)

    monkeypatch.setattr("rag_os.infrastructure.directory.graph.asyncio.sleep", fake_sleep)
    g = Graph({("PATCH", f"/users/{OID}"): [
        httpx.Response(503, headers={"Retry-After": "3600"}), httpx.Response(204),
    ]})
    await directory(g).set_attributes(OID, {"department": "HR"})
    assert waited == [30.0]


@pytest.mark.anyio
async def test_a_missing_user_is_not_found_rather_than_unavailable() -> None:
    g = Graph({("PATCH", f"/users/{OID}"): httpx.Response(404, json={"error": {"message": "nope"}})})
    with pytest.raises(NotFound):
        await directory(g).set_attributes(OID, {"department": "HR"})


@pytest.mark.anyio
async def test_a_paging_loop_terminates() -> None:
    """A nextLink that points at itself must not hang the request forever."""
    same = "https://graph.microsoft.com/v1.0/users?$skiptoken=loop"
    g = Graph({("GET", "/users"): httpx.Response(200, json={"value": [], "@odata.nextLink": same})})
    with pytest.raises(DependencyUnavailable, match="paged past"):
        await directory(g).find_user("priya@contoso.com")


def test_the_url_parsing_helper_is_not_fooled_by_a_fragment() -> None:
    """Belt and braces for the #EXT# case above: documents why httpx truncating at "#" is the danger."""
    assert urlparse("https://graph/v1.0/users/a#EXT#@b.com").path == "/v1.0/users/a"


@pytest.mark.anyio
async def test_a_role_disabled_on_the_registration_is_not_assigned() -> None:
    """The registration is the authority, not the policy file. A disabled role assigns nothing in Entra, so
    granting it would report success and leave the person without it."""
    g = Graph({("GET", f"/servicePrincipals/{SP_ID}"): {"appRoles": [
        {"value": "rag.sme", "id": "role-sme-guid", "isEnabled": False},
    ]}})
    with pytest.raises(ConfigError, match="not defined and enabled"):
        await directory(g).grant_role(OID, "rag.sme")
    assert not g.bodies("POST"), "nothing may be written for a role Entra will not honour"


@pytest.mark.anyio
async def test_role_ids_are_read_once_and_reused() -> None:
    """One extra read per process, not per request."""
    g = Graph({
        ("GET", f"/servicePrincipals/{SP_ID}"): SP_ROLES,
        ("POST", f"/servicePrincipals/{SP_ID}/appRoleAssignedTo"): httpx.Response(201, json={"id": "n"}),
    })
    d = directory(g)
    await d.grant_role(OID, "rag.sme")
    await d.grant_role(OID, "rag.admin")
    reads = [p for p in g.paths() if p == f"/servicePrincipals/{SP_ID}"]
    assert len(reads) == 1, f"the role catalogue was read {len(reads)} times"
