"""Microsoft Graph DirectoryAdmin: reads and writes Entra user attributes and app-role assignments.

The application-layer service decides WHAT may be written and to whom. This adapter's job is to do it without
falling into any of the traps below, each of which fails quietly rather than loudly:

  * A user principal name must never appear in a request path. A B2B guest's UPN contains "#EXT#", "#" begins a
    URL fragment, and the request then addresses a DIFFERENT user - or none - with no error. A UPN beginning "$"
    fails path addressing outright, because "$" is OData's parameter prefix. So: resolve the address to an
    object id once, through $filter, and use only the id afterwards.
  * `mail` is not `userPrincipalName`. A very common tenant has mail=first.last@contoso.com and
    upn=12345@contoso.onmicrosoft.com, so filtering on one finds nobody. And `mail` is not unique, so two
    matches is a conflict to report, never a first row to pick.
  * A directory extension is returned ONLY when $select names it. A plain GET omits it, which reads as "no
    value set" and is the most common false alarm in this whole area.
  * appRoleAssignedTo pages at 100. Reading page one of a busy tenant reports "holds no roles", after which a
    grant creates a duplicate - and Graph does not deduplicate, so a later revoke leaves one behind.
  * A successful write is 204 with an empty body. There is nothing to parse and nothing to confirm.

AppRoleAssignment.ReadWrite.All cannot be scoped to one service principal: it permits granting any app role on
any service principal in the tenant, including Graph's own. The narrowing in _role_id and the fixed resourceId
are therefore the only limits that exist on what this adapter can grant, so they live here rather than upstream.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from rag_os.application.ports import DirectoryAdmin, DirectoryUser, RoleAssignment
from rag_os.application.services.access_policy import odata_quote
from rag_os.domain.errors import ConfigError, Conflict, DependencyUnavailable, NotFound
from rag_os.infrastructure.registry import DIRECTORIES

log = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = "https://graph.microsoft.com/.default"
_TIMEOUT_S = 20.0
_RETRY_STATUS = (429, 500, 502, 503, 504)
_RETRY_ATTEMPTS = 4
_MAX_RETRY_AFTER_S = 30.0
_PAGE_GUARD = 100  # pages, not rows: a runaway nextLink loop must end
# Control characters are the other way an address can change the meaning of a $filter; quotes are doubled.
_UNSAFE_IN_FILTER = re.compile(r"[\x00-\x1f\x7f]")
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


@DIRECTORIES.register("graph", description="Microsoft Entra ID via Microsoft Graph (keyless, managed identity).")
class GraphDirectory(DirectoryAdmin):
    def __init__(
        self,
        *,
        extension_app_id: str | None = None,
        service_principal_object_id: str | None = None,
        attribute_names: Mapping[str, str] | None = None,
        app_role_values: Iterable[str] = (),
        transport: httpx.AsyncBaseTransport | None = None,
        token_provider: Any = None,
        **_: Any,
    ) -> None:
        """`attribute_names` maps policy attribute name -> directory extension short name.

        `app_role_values` is the set of role values this deployment defines, from the access policy. Their
        GUIDs are NOT configured: they are read from our own service principal on first use, because it is the
        authoritative record of what those roles are and whether they are still enabled. So a role the policy
        names but the registration does not define is refused rather than assigned under a stale id.
        """
        app_id = (extension_app_id or "").replace("-", "")
        if not _HEX32.match(app_id):
            raise ConfigError(
                "ENTRA_EXTENSION_APP_ID must be the application id that owns the directory extensions "
                f"(a GUID); got {extension_app_id!r}. Directory extension property names embed it, so it "
                "cannot be derived when ENTRA_AUDIENCE is not a bare app id."
            )
        if not service_principal_object_id:
            raise ConfigError(
                "ENTRA_SERVICE_PRINCIPAL_OBJECT_ID is required: app-role assignments hang off the enterprise "
                "application's object id, which is not the app (client) id. "
                "./infra/scripts/Set-EntraAppRegistration.ps1 records it."
            )
        self._prefix = f"extension_{app_id}_"
        self._sp_id = service_principal_object_id
        self._attrs = dict(attribute_names or {})
        self._permitted_roles = frozenset(app_role_values)
        self._role_ids: dict[str, str] | None = None  # resolved from the service principal on first use
        self._role_values: dict[str, str] = {}
        self._cred = None
        self._token_provider = token_provider
        if self._token_provider is None:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            self._cred = DefaultAzureCredential()
            self._token_provider = get_bearer_token_provider(self._cred, _SCOPE)
        self._client = httpx.AsyncClient(base_url=_GRAPH, timeout=_TIMEOUT_S, transport=transport)

    # -- naming ----------------------------------------------------------------------------------------
    def _extension(self, policy_name: str) -> str:
        """Policy attribute name -> extension_<appid>_<short name>.

        The short name comes from configuration and its case is preserved exactly. Entra accepts any casing
        when a value is SET but the token service matches case-sensitively when reading, so "Clearance" on one
        person and "clearance" on another means only one of them gets a claim, with no error either way.
        """
        try:
            return self._prefix + self._attrs[policy_name]
        except KeyError:
            raise ConfigError(f"no directory extension is configured for attribute {policy_name!r}") from None

    # -- transport -------------------------------------------------------------------------------------
    async def _request(self, method: str, url: str, *, json: Any = None, absolute: bool = False) -> Any:
        last: Exception | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            token = self._token_provider()
            if asyncio.iscoroutine(token):
                token = await token
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if json is not None:
                headers["Content-Type"] = "application/json"
            try:
                target: Any = httpx.URL(url) if absolute else url
                res = await self._client.request(method, target, json=json, headers=headers)
            except httpx.TransportError as e:
                last = e
                if attempt == _RETRY_ATTEMPTS:
                    raise DependencyUnavailable(f"Microsoft Graph is unreachable: {type(e).__name__}") from None
                await asyncio.sleep(min(2.0 * attempt, _MAX_RETRY_AFTER_S))
                continue
            if res.status_code in _RETRY_STATUS and attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(self._backoff(res, attempt))
                continue
            return self._decode(res)
        raise DependencyUnavailable(f"Microsoft Graph did not respond: {last}")

    @staticmethod
    def _backoff(res: httpx.Response, attempt: int) -> float:
        """Honour Retry-After when Graph sends one; it knows its own throttle window better than we do."""
        raw = res.headers.get("retry-after", "")
        try:
            return min(float(raw), _MAX_RETRY_AFTER_S)
        except ValueError:
            return min(2.0 * attempt, _MAX_RETRY_AFTER_S)

    def _decode(self, res: httpx.Response) -> Any:
        if res.status_code == 404:
            raise NotFound("Microsoft Graph returned 404 for that directory object")
        if res.status_code >= 400:
            # Graph's own message names the property or permission at fault, which is the whole diagnostic
            # value; the status alone is not actionable.
            try:
                detail = str(res.json().get("error", {}).get("message", ""))[:300]
            except Exception:  # pragma: no cover - a non-JSON error body is still a failure
                detail = res.text[:200]
            log.warning("graph call failed", extra={"status": res.status_code, "graph_error": detail})
            if res.status_code in (401, 403):
                raise DependencyUnavailable(
                    "Microsoft Graph refused this application's credentials. Check that the managed identity "
                    "holds User.ReadWrite.All, AppRoleAssignment.ReadWrite.All, Application.Read.All and "
                    "GroupMember.Read.All, and that the API revision was restarted after they were granted. "
                    "Application.Read.All is the one most often missing: reading an app-role assignment is a "
                    "read of the service principal, which AppRoleAssignment.ReadWrite.All does not cover. "
                    "./infra/scripts/Set-EntraGraphPermissions.ps1 -Env <env> -List shows what is held. "
                    f"Graph said: {detail}"
                )
            raise DependencyUnavailable(f"Microsoft Graph rejected the request ({res.status_code}): {detail}")
        # A write is 204 with an empty body. There is nothing to parse, and parsing it would be an error.
        if res.status_code == 204 or not res.content:
            return None
        return res.json()

    async def _paged(self, url: str) -> list[dict[str, Any]]:
        """Every row, following @odata.nextLink. Reading only page one is how duplicate grants happen."""
        out: list[dict[str, Any]] = []
        absolute = False
        for _ in range(_PAGE_GUARD):
            body = await self._request("GET", url, absolute=absolute) or {}
            out.extend(body.get("value") or [])
            nxt = body.get("@odata.nextLink")
            if not nxt:
                return out
            url, absolute = str(nxt), True
        raise DependencyUnavailable(f"Microsoft Graph paged past {_PAGE_GUARD} pages; refusing to continue")

    # -- port ------------------------------------------------------------------------------------------
    @staticmethod
    def _filter_literal(value: str) -> str:
        """A value safe to interpolate inside an OData string literal in a URL.

        For Edm.String properties only - userPrincipalName and mail, which is its one caller. Quoting an
        Edm.Guid property such as appRoleAssignment.principalId is a 400; see _all_assignments.

        Two separate escapes, and both are needed. odata_quote doubles the quote so the value cannot end the
        literal early and inject an operator. Percent-encoding then protects it from the URL itself - which
        matters most for the one character you would not think of: a B2B guest's principal name contains '#',
        and '#' begins a fragment even inside a query string, so an unencoded one truncates the filter and the
        fragment is never sent. The request then asks Graph to match a prefix of the address and finds nobody,
        or worse, somebody else.
        """
        return quote(odata_quote(value), safe="")

    async def find_user(self, email: str) -> DirectoryUser:
        needle = email.strip()
        if not needle or _UNSAFE_IN_FILTER.search(needle):
            raise NotFound("that is not a usable email address")
        q = self._filter_literal(needle)
        selected = ",".join(
            ["id", "userPrincipalName", "displayName", "mail", "accountEnabled", "userType"]
            + [self._extension(n) for n in self._attrs]
        )
        # A $filter, never /users/{upn}: see the module docstring on #EXT# and $-leading names. And $select,
        # because a directory extension is simply absent from the response without it.
        rows = await self._paged(
            f"/users?$filter=userPrincipalName eq '{q}' or mail eq '{q}'&$select={selected}&$top=5"
        )
        if not rows:
            raise NotFound(
                f"No user in the directory has the address {needle}. If this is a group, a whole-group grant "
                "is made in Entra rather than here - see the access policy's notes on the groups claim."
            )
        if len(rows) > 1:
            names = ", ".join(sorted(str(r.get("userPrincipalName")) for r in rows))
            raise Conflict(
                f"{needle} matches {len(rows)} users ({names}). Entra does not require `mail` to be unique, so "
                "use the exact user principal name instead."
            )
        return self._to_user(rows[0])

    def _to_user(self, row: Mapping[str, Any]) -> DirectoryUser:
        attributes = {}
        for policy_name in self._attrs:
            raw = row.get(self._extension(policy_name))
            if raw is not None:
                attributes[policy_name] = str(raw)
        return DirectoryUser(
            object_id=str(row["id"]),
            user_principal_name=str(row.get("userPrincipalName") or ""),
            display_name=str(row.get("displayName") or ""),
            mail=str(row["mail"]) if row.get("mail") else None,
            account_enabled=bool(row.get("accountEnabled", True)),
            user_type=str(row.get("userType") or "Member"),
            attributes=attributes,
        )

    async def set_attributes(self, object_id: str, values: Mapping[str, str | None]) -> None:
        body: dict[str, Any] = {}
        for name, value in values.items():
            if value is not None and not isinstance(value, str):  # pragma: no cover - the port types this out
                raise ConfigError(f"attribute {name!r} must be written as a string, not {type(value).__name__}")
            body[self._extension(name)] = value  # None clears it, which is what Graph wants
        if not body:
            return
        await self._request("PATCH", f"/users/{object_id}", json=body)

    async def list_roles(self, object_id: str) -> list[RoleAssignment]:
        await self._ensure_roles()
        rows = await self._all_assignments()
        direct: dict[str, int] = {}
        for row in self._assignments_for(rows, object_id):
            value = self._role_values.get(str(row.get("appRoleId")))
            if value:
                direct[value] = direct.get(value, 0) + 1
        out = [RoleAssignment(role_value=v, duplicates=n) for v, n in sorted(direct.items())]
        out += [r for r in await self._group_roles(rows, object_id) if r.role_value not in direct]
        return out

    async def _all_assignments(self) -> list[dict[str, Any]]:
        """Every app-role assignment on OUR service principal, read whole and narrowed in Python.

        Unfiltered on purpose. appRoleAssignedTo does not support $filter on principalId in either literal form,
        and the two failures look unrelated, so both are worth naming:

            principalId eq '<guid>'   ->  400 "A binary operator with incompatible types was detected. Found
                                         operand types 'Edm.Guid' and 'Edm.String'" - the property is Edm.Guid,
                                         and quoting makes the literal a string.
            principalId eq <guid>     ->  400 "Links to EntitlementGrant are not supported between specified
                                         entities" - so removing the quotes is not the fix either.

        Microsoft's guidance is to read the collection and filter client-side, which is what this does. The
        reference page lists $filter as supported here; it is wrong about these properties.
        """
        return await self._paged(f"/servicePrincipals/{self._sp_id}/appRoleAssignedTo")

    @staticmethod
    def _assignments_for(rows: Iterable[dict[str, Any]], object_id: str) -> list[dict[str, Any]]:
        """The rows belonging to one principal. Pure, because the narrowing moved here from the server and an
        over-matching comparison would report every person as holding everyone else's roles."""
        return [r for r in rows if str(r.get("principalId")) == object_id]

    async def _group_roles(self, rows: list[dict[str, Any]], object_id: str) -> list[RoleAssignment]:
        """Roles this person holds because a GROUP holds them.

        A group-assigned app role reaches the token exactly as a direct one does, so leaving these out would
        show "not held" for somebody who is an administrator - and deleting the direct assignment an
        administrator then creates would not take it away.

        Takes the rows its caller already read: this used to fetch the same collection a second time.
        """
        by_group = {
            str(r.get("principalId")): str(r.get("principalDisplayName") or "a group")
            for r in rows
            if str(r.get("principalType")) == "Group"
        }
        if not by_group:
            return []
        groups = await self._paged(f"/users/{object_id}/transitiveMemberOf/microsoft.graph.group?$select=id")
        mine = {str(g.get("id")) for g in groups}
        out: list[RoleAssignment] = []
        for row in rows:
            gid = str(row.get("principalId"))
            if gid not in mine or gid not in by_group:
                continue
            value = self._role_values.get(str(row.get("appRoleId")))
            if value:
                out.append(RoleAssignment(role_value=value, principal_type="Group", via_group=by_group[gid]))
        return out

    async def _ensure_roles(self) -> dict[str, str]:
        """value -> appRoleId for the roles this deployment defines, read from our own service principal.

        Narrowed to the policy's catalogue and to roles Entra reports as enabled. Together with the fixed
        resourceId on every write, this is the entire limit on what can be assigned:
        AppRoleAssignment.ReadWrite.All itself permits granting any app role on any service principal in the
        tenant, Graph's own included, and cannot be scoped.
        """
        if self._role_ids is not None:
            return self._role_ids
        body = await self._request("GET", f"/servicePrincipals/{self._sp_id}?$select=appRoles") or {}
        found = {}
        for role in body.get("appRoles") or []:
            value = str(role.get("value") or "")
            if value in self._permitted_roles and role.get("isEnabled", True) and role.get("id"):
                found[value] = str(role["id"])
        self._role_ids = found
        self._role_values = {v: k for k, v in found.items()}
        missing = sorted(self._permitted_roles - set(found))
        if missing:
            log.warning("app roles named by the access policy are missing or disabled on the registration",
                        extra={"missing_app_roles": missing})
        return found

    async def _role_id(self, role_value: str) -> str:
        if role_value not in self._permitted_roles:
            # Refused before any lookup: an appRoleId from outside our own catalogue is precisely the
            # escalation the permission would otherwise allow.
            raise ConfigError(
                f"{role_value!r} is not an application role of this deployment, so it will not be assigned"
            )
        roles = await self._ensure_roles()
        try:
            return roles[role_value]
        except KeyError:
            raise ConfigError(
                f"{role_value!r} is named by the access policy but is not defined and enabled on this app "
                f"registration; run ./infra/scripts/Set-EntraAppRegistration.ps1 to reconcile it"
            ) from None

    async def grant_role(self, object_id: str, role_value: str) -> None:
        # resourceId is always OUR service principal. The permission would allow any other; nothing else here
        # constrains it.
        role_id = await self._role_id(role_value)
        body = {"principalId": object_id, "resourceId": self._sp_id, "appRoleId": role_id}
        await self._request("POST", f"/servicePrincipals/{self._sp_id}/appRoleAssignedTo", json=body)

    async def revoke_role(self, object_id: str, role_value: str) -> int:
        role_id = await self._role_id(role_value)
        rows = self._assignments_for(await self._all_assignments(), object_id)
        # Every match, not the first: Graph does not deduplicate, so one grant posted twice is two rows and
        # removing one of them leaves the person holding the role.
        targets = [str(r["id"]) for r in rows if str(r.get("appRoleId")) == role_id and r.get("id")]
        for assignment_id in targets:
            await self._request("DELETE", f"/servicePrincipals/{self._sp_id}/appRoleAssignedTo/{assignment_id}")
        return len(targets)

    async def revoke_sessions(self, object_id: str) -> None:
        await self._request("POST", f"/users/{object_id}/revokeSignInSessions", json={})

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._cred is not None:
            await self._cred.close()
