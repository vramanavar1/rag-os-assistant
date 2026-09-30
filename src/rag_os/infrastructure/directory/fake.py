"""In-memory DirectoryAdmin for tests and offline runs.

Deliberately hostile. A fake that accepted everything would pass code the real provider rejects, and the tests
built on it would be decoration - the same reasoning as the mini-Graph stub in tests/unit/test_infra_entra_script.py.
So this one reproduces the Microsoft Graph behaviours that actually bite:

  * a repeated grant creates a SECOND assignment, because Graph does not deduplicate
  * a non-string attribute value is refused, because every extension is declared as a string
  * an attribute name the policy does not define is refused rather than silently stored
  * attribute names are case-sensitive, because the token service reads them case-sensitively
  * an address matching two users is a Conflict, because `mail` is not unique in Entra
  * only an object id addresses a user; an email passed where an id belongs raises
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from rag_os.application.ports import DirectoryAdmin, DirectoryUser, RoleAssignment
from rag_os.domain.errors import Conflict, NotFound, ValidationFailed
from rag_os.infrastructure.registry import DIRECTORIES


@dataclass
class FakeUser:
    object_id: str
    user_principal_name: str
    display_name: str = ""
    mail: str | None = None
    account_enabled: bool = True
    user_type: str = "Member"
    attributes: dict[str, str] = field(default_factory=dict)
    # One entry per assignment, so a duplicate grant is visible as two entries - as it is in Graph.
    assignments: list[str] = field(default_factory=list)
    group_roles: dict[str, str] = field(default_factory=dict)  # role value -> group display name
    sessions_revoked: int = 0


@DIRECTORIES.register("fake", description="In-memory directory for tests. Writes nowhere.", stub=True)
class FakeDirectory(DirectoryAdmin):
    def __init__(self, *, known_attributes: tuple[str, ...] = ("department", "region", "clearance"),
                 **_: Any) -> None:
        self.users: dict[str, FakeUser] = {}
        self.known_attributes = known_attributes
        self.fail_on: dict[str, str] = {}  # operation name -> message, for testing partial failure

    # -- test seam -------------------------------------------------------------------------------------
    def seed(self, user: FakeUser) -> FakeUser:
        self.users[user.object_id] = user
        return user

    def _user(self, object_id: str) -> FakeUser:
        if "@" in object_id:
            raise AssertionError(
                f"{object_id!r} looks like an address, not an object id - resolve it with find_user first")
        try:
            return self.users[object_id]
        except KeyError:
            raise NotFound(f"no user with object id {object_id}") from None

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise ValidationFailed(self.fail_on[op])

    # -- port ------------------------------------------------------------------------------------------
    async def find_user(self, email: str) -> DirectoryUser:
        needle = email.strip().casefold()
        matches = [
            u for u in self.users.values()
            if u.user_principal_name.casefold() == needle or (u.mail or "").casefold() == needle
        ]
        if not matches:
            raise NotFound(f"no user in the directory with the address {email}")
        if len(matches) > 1:
            raise Conflict(
                f"{email} matches {len(matches)} users: " + ", ".join(sorted(u.user_principal_name for u in matches))
            )
        u = matches[0]
        return DirectoryUser(
            object_id=u.object_id, user_principal_name=u.user_principal_name, display_name=u.display_name,
            mail=u.mail, account_enabled=u.account_enabled, user_type=u.user_type, attributes=dict(u.attributes),
        )

    async def set_attributes(self, object_id: str, values: Mapping[str, str | None]) -> None:
        self._maybe_fail("set_attributes")
        u = self._user(object_id)
        for name, value in values.items():
            if name not in self.known_attributes:
                raise ValidationFailed(f"the directory has no attribute named {name!r}")
            if value is None:
                u.attributes.pop(name, None)
                continue
            if not isinstance(value, str):
                raise ValidationFailed(
                    f"attribute {name!r} is declared as a string; {value!r} is {type(value).__name__}")
            u.attributes[name] = value

    async def list_roles(self, object_id: str) -> list[RoleAssignment]:
        u = self._user(object_id)
        out = [
            RoleAssignment(role_value=v, duplicates=u.assignments.count(v))
            for v in sorted(set(u.assignments))
        ]
        out += [RoleAssignment(role_value=v, via_group=g) for v, g in sorted(u.group_roles.items())]
        return out

    async def grant_role(self, object_id: str, role_value: str) -> None:
        self._maybe_fail("grant_role")
        # No deduplication, on purpose.
        self._user(object_id).assignments.append(role_value)

    async def revoke_role(self, object_id: str, role_value: str) -> int:
        self._maybe_fail("revoke_role")
        u = self._user(object_id)
        removed = u.assignments.count(role_value)
        u.assignments = [v for v in u.assignments if v != role_value]
        return removed

    async def revoke_sessions(self, object_id: str) -> None:
        self._maybe_fail("revoke_sessions")
        self._user(object_id).sessions_revoked += 1
