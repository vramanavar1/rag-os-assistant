"""Assigning a person's access attributes and application roles.

All of the judgement about what may be written, to whom, and by whom lives here rather than in the router or the
adapter. Two reasons: mypy is configured over domain/ and application/ only, so this is the only layer where the
rules are type-checked; and the rules are the security boundary, which should not be spread across a request
handler and an HTTP client.

The shape of a write is deliberately a PUT of the desired state rather than grant/revoke calls:

  * Microsoft Graph does not deduplicate app-role assignments, so a "grant" endpoint called twice leaves two
    rows and a later single revoke leaves the person still holding the role.
  * A desired-state write is idempotent, which this project requires of every transaction.
  * It needs no DELETE or PATCH method, so the API's CORS allow-list and the browser's client stay as they are.

Because a desired-state write can revoke something the caller never saw, it is guarded by an ETag: a second tab
that loaded before a grant cannot silently undo it.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from rag_os.application.ports import DirectoryAdmin, DirectoryUser, RoleAssignment
from rag_os.domain.access import AccessPolicy, AttributeRule, MatchKind, Principal
from rag_os.domain.classification import FacetSchema
from rag_os.domain.errors import AccessDenied, Conflict, NotSupported, ValidationFailed

log = logging.getLogger(__name__)
audit = logging.getLogger("rag_os.audit")

_EXTN = "extn."

# What the page must tell an administrator every time, because it is the one thing that makes a correct write
# look like a failed one. It is returned by the API rather than written in the browser so that the UI never
# restates access semantics of its own - the same rule Account Information is held to.
PROPAGATION_NOTE = (
    "The directory record is updated now. The next token this person is issued carries the new values; a token "
    "they already hold keeps the old ones until it expires, usually within an hour. Revoking their sign-in "
    "sessions ends that wait but signs them out of every other application in the tenant too."
)


@dataclass(frozen=True)
class DirectoryWrite:
    """The desired state. `attributes` is keyed by policy attribute name; None clears one.

    `roles` is the complete set of DIRECT assignments wanted - anything held directly and absent here is
    revoked. Roles held through a group are not expressible and are never touched.
    """

    attributes: dict[str, str | None] = field(default_factory=dict)
    roles: list[str] | None = None  # None = leave role assignments alone entirely
    revoke_sessions: bool = False
    confirm: str = ""  # the target's user principal name, required to grant an administrator role


def writable_attributes(policy: AccessPolicy) -> list[AttributeRule]:
    """The attributes that can be written per-person: the ones fed by a directory extension.

    An attribute read from `oid`, or from `groups` through a value_map, has no extension behind it, so writing
    one would succeed and change nothing. Module-level because the composition root needs the extension names
    to build the adapter, before any service exists.
    """
    return [r for r in policy.attributes if r.claims.get("entra", "").startswith(_EXTN)]


def extension_names(policy: AccessPolicy) -> dict[str, str]:
    """Policy attribute name -> directory extension short name, taken from the claim the policy reads.

    Derived rather than configured separately so the write side and the read side cannot disagree: if the policy
    reads `extn.department`, this writes `department`.
    """
    return {r.name: r.claims["entra"][len(_EXTN):] for r in writable_attributes(policy)}


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    applied: list[str]
    failed: list[str]
    state: dict[str, Any]
    etag: str


class DirectoryAdminService:
    def __init__(self, directory: DirectoryAdmin | None, policy: AccessPolicy, facets: FacetSchema) -> None:
        """`directory` may be None: the page still has to render its master lists and explain why it is off."""
        self._directory = directory
        self.policy = policy
        self.facets = facets

    @property
    def enabled(self) -> bool:
        return self._directory is not None

    @property
    def directory(self) -> DirectoryAdmin:
        if self._directory is None:
            raise NotSupported(
                "Directory administration is not configured for this deployment. Set DIRECTORY=graph in "
                "ExtraAppSettings in infra/env/<env>.psd1 and re-run 07-container-apps.ps1. It also requires "
                "DEV_AUTH_ENABLED=false, which the API enforces by refusing to start the adapter, and the Graph "
                "permissions granted by Set-EntraGraphPermissions.ps1. The full runbook is Deployment.md section "
                "9.3. ENTRA_SERVICE_PRINCIPAL_OBJECT_ID needs no action: step 07 passes it from the outputs file."
            )
        return self._directory

    # ---------------------------------------------------------------- the master lists
    @property
    def writable(self) -> list[AttributeRule]:
        return writable_attributes(self.policy)

    def extension_names(self) -> dict[str, str]:
        return extension_names(self.policy)

    def _admin_role_values(self) -> set[str]:
        """App role values that grant the internal `admin` role.

        Config-driven rather than a literal "rag.admin": admin bypasses the document filter entirely, so
        whatever grants it is the grant that needs a deliberate confirmation.
        """
        return set(self.policy.roles.get("admin", []))

    def capability(self) -> dict[str, Any]:
        """Everything the page needs to render itself, assembled where the rules live."""
        admin_values = self._admin_role_values()
        attributes = []
        for rule in self.writable:
            values = (
                [{"value": str(lvl.value), "label": lvl.label, "description": lvl.description}
                 for lvl in rule.levels]
                if rule.levels
                else [{"value": v.value, "label": v.label or v.value, "description": v.description}
                      for v in rule.allowed_values]
            )
            attributes.append({
                "name": rule.name,
                "label": rule.label or rule.name.replace("_", " ").title(),
                "description": rule.description,
                "required": rule.required,
                "clearable": not rule.required,
                "values": values,
            })
        roles = [
            {"value": r.value, "display_name": r.display_name or r.value, "description": r.description,
             "needs_confirmation": r.value in admin_values}
            for r in self.policy.app_roles
        ]
        return {
            "enabled": self.enabled,
            "attributes": attributes,
            "app_roles": roles,
            "warnings": self._warnings(),
            "propagation_note": PROPAGATION_NOTE,
        }

    def _warnings(self) -> list[str]:
        """Advisory, never blocking. Each of these is a configuration that works but grants less than it looks.

        Deliberately not enforced when the policy is written: facets.yaml is editable by a weaker role than
        access-policy.yaml, so making one file's validity depend on the other would leave a taxonomy editor
        stuck behind a file they cannot edit.
        """
        out: list[str] = []
        for rule in self.writable:
            if rule.match == MatchKind.HIERARCHICAL and rule.hierarchy_facet:
                tree = {
                    v.id for f in self.facets.facets if f.name == rule.hierarchy_facet for v in f.values
                }
                orphans = sorted(v.value for v in rule.allowed_values if v.value not in tree)
                if orphans:
                    out.append(
                        f"{rule.name}: {', '.join(orphans)} are not in the '{rule.hierarchy_facet}' facet tree, so "
                        f"assigning one reaches only that value and none of its parents. They cannot be assigned "
                        f"until the facet defines them."
                    )
            if not rule.levels and not rule.allowed_values:
                out.append(f"{rule.name}: no allowed_values in the access policy, so it cannot be assigned here.")
        return out

    # ---------------------------------------------------------------- reading a person
    async def describe(self, email: str) -> dict[str, Any]:
        user = await self.directory.find_user(email)
        roles = await self.directory.list_roles(user.object_id)
        return self._state(user, roles)

    def _state(self, user: DirectoryUser, roles: list[RoleAssignment]) -> dict[str, Any]:
        return {
            "object_id": user.object_id,
            "user_principal_name": user.user_principal_name,
            "display_name": user.display_name,
            "mail": user.mail,
            "account_enabled": user.account_enabled,
            "user_type": user.user_type,
            "attributes": {r.name: user.attributes.get(r.name) for r in self.writable},
            "roles": [
                {"value": a.role_value, "via_group": a.via_group, "removable": a.removable,
                 "duplicates": a.duplicates}
                for a in roles
            ],
            "etag": self._etag(user, roles),
            "propagation_note": PROPAGATION_NOTE,
        }

    def _etag(self, user: DirectoryUser, roles: list[RoleAssignment]) -> str:
        """A digest of the state this page manages, so a stale tab cannot revoke what it never saw.

        `duplicates` is part of it on purpose: a second assignment created behind the page's back is a real
        state change, and hashing only role names would hide it.
        """
        parts = [f"{r.name}={user.attributes.get(r.name) or ''}" for r in self.writable]
        parts += [
            f"role={a.role_value}:{a.duplicates}:{a.via_group or ''}"
            for a in sorted(roles, key=lambda a: (a.role_value, a.via_group or ""))
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    # ---------------------------------------------------------------- validating a write
    def _rule(self, name: str) -> AttributeRule:
        for rule in self.writable:
            if rule.name == name:
                return rule
        known = ", ".join(r.name for r in self.writable) or "none"
        raise NotSupported(
            f"{name!r} is not an attribute this deployment can assign (assignable: {known}). An attribute is "
            f"assignable only when the access policy reads it from a directory extension - 'extn.<name>'."
        )

    def _check_value(self, rule: AttributeRule, value: str) -> None:
        if rule.levels:
            allowed = {str(lvl.value) for lvl in rule.levels}
            if value not in allowed:
                raise ValidationFailed(
                    f"{rule.name} must be one of {sorted(allowed)}; {value!r} is outside the ladder the access "
                    f"policy defines, and a caller carrying it would read more than any named level permits."
                )
            return
        allowed_values = {v.value for v in rule.allowed_values}
        if not allowed_values:
            raise NotSupported(
                f"{rule.name} has no allowed_values in the access policy, so there is nothing it may be set to."
            )
        if value not in allowed_values:
            # Case-sensitive on purpose: document tags are matched case-sensitively, so "hr" would grant nothing.
            raise ValidationFailed(
                f"{value!r} is not an allowed value for {rule.name}. Allowed: {', '.join(sorted(allowed_values))}."
            )
        if rule.match == MatchKind.HIERARCHICAL and rule.hierarchy_facet:
            tree = {v.id for f in self.facets.facets if f.name == rule.hierarchy_facet for v in f.values}
            if value not in tree:
                raise ValidationFailed(
                    f"{value!r} is not in the '{rule.hierarchy_facet}' facet tree, so a caller assigned it would "
                    f"reach only {value!r} and none of its parents. Add it to the facet first."
                )

    def _check_roles(self, wanted: list[str], user: DirectoryUser, confirm: str) -> None:
        defined = {r.value for r in self.policy.app_roles}
        unknown = sorted(set(wanted) - defined)
        if unknown:
            raise ValidationFailed(
                f"not application roles of this deployment: {', '.join(unknown)}. Assignable: "
                f"{', '.join(sorted(defined))}."
            )
        needs_confirmation = sorted(set(wanted) & self._admin_role_values())
        if needs_confirmation and confirm.strip().casefold() != user.user_principal_name.casefold():
            raise ValidationFailed(
                f"granting {', '.join(needs_confirmation)} makes this person an administrator, which also "
                f"bypasses the document access filter entirely. To confirm, type their user principal name "
                f"({user.user_principal_name}) exactly."
            )

    def _check_actor(self, actor: Principal, user: DirectoryUser) -> str:
        """Refuse a caller editing their own access, and refuse a caller we cannot identify.

        The identity check uses the Entra object id, NOT Principal.subject: the claims mapper prefers the `sub`
        claim, which for Entra is a pairwise per-application value that exists nowhere in the directory, so
        comparing it would never match and this guard would pass for everyone.

        No object id at all means a dev-issuer token. Those assert whatever a developer typed, roles included,
        so they must be refused outright rather than have the check skipped.
        """
        actor_oid = actor.directory_object_id
        if not actor_oid:
            raise AccessDenied(
                "Directory administration requires a Microsoft Entra sign-in. This token does not identify a "
                "directory object, so the check that you are not editing your own access cannot be made."
            )
        if actor_oid == user.object_id:
            raise AccessDenied(
                "You cannot change your own department, region, clearance or roles. Ask another administrator, "
                "or use ./infra/scripts/Set-EntraAppRoleAssignment.ps1 from a terminal."
            )
        return actor_oid

    # ---------------------------------------------------------------- applying a write
    async def apply(self, actor: Principal, email: str, write: DirectoryWrite, if_match: str) -> ApplyResult:
        if not if_match:
            raise ValidationFailed(
                "An If-Match header carrying the etag from the last read is required, so a page loaded before "
                "somebody else's change cannot silently undo it."
            )
        user = await self.directory.find_user(email)
        actor_oid = self._check_actor(actor, user)
        before_roles = await self.directory.list_roles(user.object_id)
        current = self._etag(user, before_roles)
        if if_match.strip('"') != current:
            raise Conflict(
                "This person's access changed since the page was loaded. Reload to see the current values "
                "before writing.",
                detail={"etag": current},
            )

        attributes = {name: value for name, value in write.attributes.items()}
        for name, value in attributes.items():
            rule = self._rule(name)
            if value is None:
                if rule.required:
                    raise ValidationFailed(
                        f"{rule.name} is required by the access policy: clearing it leaves this person able to "
                        f"read nothing at all. Assign a value instead."
                    )
                continue
            self._check_value(rule, value)

        held_directly = sorted({a.role_value for a in before_roles if a.removable})
        wanted = sorted(set(write.roles)) if write.roles is not None else held_directly
        if write.roles is not None:
            self._check_roles(wanted, user, write.confirm)
        to_revoke = [v for v in held_directly if v not in wanted]
        # A duplicate assignment is re-written even when the role is wanted, so one PUT converges on one row.
        to_grant = [v for v in wanted if v not in held_directly]
        duplicated = [a.role_value for a in before_roles if a.removable and a.duplicates > 1
                      and a.role_value in wanted]

        return await self._run(actor, actor_oid, user, attributes, to_revoke, to_grant, duplicated,
                               write.revoke_sessions, before_roles)

    async def _run(
        self,
        actor: Principal,
        actor_oid: str,
        user: DirectoryUser,
        attributes: dict[str, str | None],
        to_revoke: list[str],
        to_grant: list[str],
        duplicated: list[str],
        revoke_sessions: bool,
        before_roles: list[RoleAssignment],
    ) -> ApplyResult:
        """Revokes, then attributes, then grants - so an abort leaves the person with LESS access, not more.

        Aborts on the first failure rather than continuing. Applying grants after a failed revoke is the worst
        outcome the sequence can produce, and "best effort" would produce exactly that.
        """
        applied: list[str] = []
        failed: list[str] = []
        try:
            for value in to_revoke:
                removed = await self.directory.revoke_role(user.object_id, value)
                applied.append(f"revoked {value}" + (f" ({removed} assignments)" if removed > 1 else ""))
            for value in duplicated:
                removed = await self.directory.revoke_role(user.object_id, value)
                await self.directory.grant_role(user.object_id, value)
                applied.append(f"collapsed {removed} duplicate assignments of {value} into one")
            if attributes:
                await self.directory.set_attributes(user.object_id, attributes)
                applied += [f"set {k} to {v!r}" if v is not None else f"cleared {k}" for k, v in attributes.items()]
            for value in to_grant:
                await self.directory.grant_role(user.object_id, value)
                applied.append(f"granted {value}")
            if revoke_sessions:
                await self.directory.revoke_sessions(user.object_id)
                applied.append("revoked sign-in sessions")
        except Exception as e:
            # Reported as a 200 with a report, not raised. The error body would carry only a message, and the
            # one thing the administrator needs is which of these steps already landed in the directory.
            failed.append(f"{type(e).__name__}: {e}")

        after = await self.directory.find_user(user.user_principal_name)
        after_roles = await self.directory.list_roles(user.object_id)
        self._audit(actor, actor_oid, user, before_roles, after, after_roles, applied, failed, revoke_sessions)
        state = self._state(after, after_roles)
        return ApplyResult(ok=not failed, applied=applied, failed=failed, state=state, etag=state["etag"])

    def _audit(
        self,
        actor: Principal,
        actor_oid: str,
        before: DirectoryUser,
        before_roles: list[RoleAssignment],
        after: DirectoryUser,
        after_roles: list[RoleAssignment],
        applied: list[str],
        failed: list[str],
        revoke_sessions: bool,
    ) -> None:
        """One record per attempt, including a failed one - a partial write is the case worth being able to read.

        This contains personal data (addresses, clearance levels) and goes wherever the application's logs go.
        """
        changed = {
            r.name: [before.attributes.get(r.name), after.attributes.get(r.name)]
            for r in self.writable
            if before.attributes.get(r.name) != after.attributes.get(r.name)
        }
        was = {a.role_value for a in before_roles if a.removable}
        now = {a.role_value for a in after_roles if a.removable}
        granted = sorted(now - was)
        record = {
            "actor": actor.display_name or actor.subject,
            "actor_object_id": actor_oid,
            "target_object_id": after.object_id,
            "target": after.user_principal_name,
            "attributes_changed": changed,
            "roles_granted": granted,
            "roles_revoked": sorted(was - now),
            "sessions_revoked": revoke_sessions,
            "applied": applied,
            "failed": failed,
        }
        if failed:
            audit.error("directory write partially applied", extra=record)
        elif set(granted) & self._admin_role_values():
            audit.warning("administrator role granted", extra=record)
        else:
            audit.info("directory write applied", extra=record)
