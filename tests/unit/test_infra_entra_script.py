"""Set-EntraAppRegistration.ps1 end to end, against a stub that enforces what Microsoft Graph enforces.

This file exists because of a bug no unit test could have caught. `Get-EntraAppPatch` was correct in isolation and
its 33 tests passed; the script around it was correct in isolation too. The defect lived in the *interaction*: the
one PATCH they produced together created a scope and, in the same body, pre-authorised a client for that
brand-new scope id. Graph rejected the lot with

    InvalidValue: Property api.preAuthorizedApplications.delegatedPermissionIds has a Permission Id
    that cannot be found in the AppPermissions sets.

because `preAuthorizedApplications` is validated against the permission set **already persisted** on the
application, never against the `oauth2PermissionScopes` arriving in the same request. The rejection is atomic, so
the scope — the only part that actually had to land — went down with it.

**The stub therefore enforces that rule.** `Invoke-AzRest` here rejects any `delegatedPermissionIds` entry naming
an id absent from the *previously persisted* scopes, with Graph's own message. That is the whole value of this
file: a stub that accepted everything would have passed the broken code, and these tests would be decoration. It
also models Graph's wholesale replacement of top-level properties, which is the other trap in this area.

What is deliberately NOT modelled: replication lag. Graph can reject a pre-authorisation for a scope it has
already acknowledged on a read, which is why the real second write is retried. A stub cannot usefully fake that,
so the retry is verified by reading the code, not here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from .test_infra_readiness import write_env

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra" / "scripts" / "Set-EntraAppRegistration.ps1"
ASSIGN_SCRIPT = REPO / "infra" / "scripts" / "Set-EntraAppRoleAssignment.ps1"
COMMON = REPO / "infra" / "scripts" / "common.ps1"
ENV_DIR = REPO / "infra" / "env"
PWSH = shutil.which("pwsh")

# write_env derives a fixture env from dev.psd1, which is gitignored (.gitignore: *.psd1), so it is present on a
# working machine and absent from a fresh clone. Skipping beats erroring on a file the repo does not carry.
needs_pwsh = pytest.mark.skipif(
    PWSH is None or not (ENV_DIR / "dev.psd1").exists(),
    reason="needs pwsh and a local infra/env/dev.psd1 to derive the fixture env from")

ENV_NAME = "zzentra"   # 1-8 lowercase chars, per common.ps1; "zz" marks a fixture env like the siblings
APP = "72f70e5a-291a-4c27-a6c9-1a7d1fbe7f9e"
TENANT = "c2ff8ba6-8824-4dbb-85d6-b12c6fc80d0c"
OTHER_APP = "99999999-8888-7777-6666-555555555555"
AZ_CLI = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
FQDN = "rag-chat-ui.example.westus.azurecontainerapps.io"
CALLBACK = f"https://{FQDN}/auth/callback"

GRAPH_ORIGIN_400 = (
    'az command failed (exit 1): az rest --method patch ERROR: Bad Request({"error":{"code":"InvalidValue",'
    '"message":"Property appRoles.origin is read-only and cannot be set."}})')

GRAPH_400 = (
    'az command failed (exit 1): az rest --method patch --url https://graph.microsoft.com/v1.0/applications/obj1'
    ' ERROR: Bad Request({"error":{"code":"InvalidValue","message":"Property api.preAuthorizedApplications.'
    'delegatedPermissionIds has a Permission Id that cannot be found in the AppPermissions sets."}})')

# Injected straight after the script's own `. common.ps1`, so these override the real helpers but are defined
# before Initialize-RagOsScript runs.
STUB = r'''
$script:StatePath = $env:STUB_STATE
$script:PatchLog = $env:STUB_PATCHLOG
function Get-StubState { Get-Content -LiteralPath $script:StatePath -Raw | ConvertFrom-Json -AsHashtable }
function Set-StubState($s) { ($s | ConvertTo-Json -Depth 30) | Set-Content -LiteralPath $script:StatePath -Encoding utf8 }

function Set-RagOsAzContext { param($Config) }
function Save-Outputs { param($Config, $Values, $Clear) }
function Get-Outputs { param($Config) return @{ chatUiFqdn = $env:STUB_FQDN } }

function Get-DeployerPrincipal { @{ ObjectId = $env:STUB_ME; PrincipalType = 'User'; Name = 'me@example.com' } }

function Invoke-Az {
    param([string[]]$Arguments, [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    $joined = $Arguments -join ' '
    if ($joined -match 'ad app show') {
        if ($env:STUB_NO_APP -eq '1') { return $null }
        return (Get-StubState)
    }
    if ($joined -match 'ad sp show') { return ($env:STUB_NO_SP -eq '1') ? $null : @{ id = 'sp-object-id' } }
    if ($joined -match 'ad sp create') { return @{ id = 'sp-object-id' } }
    if ($joined -match 'ad user show') { return 'user-object-id' }
    return $null
}

function Invoke-AzRest {
    param([string]$Method, [string]$Url, [object]$Body, [switch]$AllowNotFound, [switch]$Sensitive)

    # ---- app role assignments live on the SERVICE PRINCIPAL, not the application. Stored as a LIST: Graph
    # does not deduplicate, one principal can hold several roles, and a delete addresses one entry by its own id.
    if ($Url -match 'appRoleAssignedTo') {
        $all = (Test-Path $env:STUB_ASSIGNMENTS) ?
            @(Get-Content $env:STUB_ASSIGNMENTS -Raw | ConvertFrom-Json -AsHashtable) : @()
        if ($Method -eq 'get') { return @{ value = $all } }
        if ($Method -eq 'delete') {
            # .../appRoleAssignedTo/<assignment-id>
            $target = ($Url -split '/')[-1]
            Add-Content -LiteralPath $script:PatchLog -Value "REVOKE $target"
            $kept = @($all | Where-Object { $_.id -ne $target })
            (, $kept | ConvertTo-Json -Depth 10 -AsArray) | Set-Content -LiteralPath $env:STUB_ASSIGNMENTS -Encoding utf8
            return @{}
        }
        if ($env:STUB_DENY_ASSIGN -eq '1') { throw 'Forbidden: Authorization_RequestDenied - Insufficient privileges.' }
        Add-Content -LiteralPath $script:PatchLog -Value ("ASSIGN " + ($Body | ConvertTo-Json -Depth 10 -Compress))
        $entry = @{}
        foreach ($k in $Body.Keys) { $entry[$k] = $Body[$k] }
        $entry['id'] = "assignment-$($all.Count + 1)"
        $entry['principalDisplayName'] = $env:STUB_PRINCIPAL_NAME
        $entry['principalType'] = 'User'
        (@($all + $entry) | ConvertTo-Json -Depth 10 -AsArray) |
            Set-Content -LiteralPath $env:STUB_ASSIGNMENTS -Encoding utf8
        return @{}
    }

    # ---- directory extensions are their own Graph resource, created one at a time. The script predicts the
    # registered name (extension_<appid>_<name>) so it can name it in optionalClaims; the stub composes the
    # same thing, because a mismatch there is the bug that would ship silently.
    if ($Url -match 'extensionProperties') {
        $defined = (Test-Path $env:STUB_EXTENSIONS) ?
            @(Get-Content $env:STUB_EXTENSIONS -Raw | ConvertFrom-Json -AsHashtable) : @()
        if ($Method -eq 'get') { return @{ value = $defined } }
        Add-Content -LiteralPath $script:PatchLog -Value ('EXTENSION ' + ($Body | ConvertTo-Json -Depth 10 -Compress))
        $entry = @{}
        foreach ($k in $Body.Keys) { $entry[$k] = $Body[$k] }
        $entry['name'] = "extension_$($env:STUB_CLIENT_ID -replace '-', '')_$($Body['name'])"
        (@($defined + $entry) | ConvertTo-Json -Depth 10 -AsArray) |
            Set-Content -LiteralPath $env:STUB_EXTENSIONS -Encoding utf8
        return @{}
    }

    $state = Get-StubState

    # ---- `origin` on an appRole is READ-ONLY: Graph rejects any write that carries it back. Modelled because
    # echoing what was read is the obvious implementation, and without this the stub would accept it happily.
    # Where-Object, because @($null) is a ONE-element array holding $null in PowerShell, not an empty one -
    # so a body without this property would otherwise iterate once with $role = $null.
    foreach ($role in @(Get-Value $Body 'appRoles') | Where-Object { $_ }) {
        if ($null -ne (Get-Value $role 'origin')) { throw $env:STUB_GRAPH_ORIGIN_400 }
    }

    # ---- Graph's validation, and the reason this stub exists. Pre-authorisations are checked against the
    # permissions ALREADY PERSISTED on the application - not against the scopes in this same body.
    $persisted = @(@(Get-Value $state 'api.oauth2PermissionScopes') | ForEach-Object { [string](Get-Value $_ 'id') })
    foreach ($entry in @(Get-Value $Body 'api.preAuthorizedApplications') | Where-Object { $_ }) {
        foreach ($permissionId in @(Get-Value $entry 'delegatedPermissionIds') | Where-Object { $_ }) {
            if ($persisted -notcontains $permissionId) { throw $env:STUB_GRAPH_400 }
        }
    }
    # ---- optional: refuse the pre-authorisation write even when it is valid, to exercise the degraded path.
    if ($env:STUB_FAIL_PREAUTH -eq '1' -and
        @(@(Get-Value $Body 'api.preAuthorizedApplications') | Where-Object { $_ }).Count -gt 0) {
        throw $env:STUB_GRAPH_400
    }

    # Logged apart from the reconcile writes: the patch-count assertions below are about the scope /
    # pre-authorisation two-write dance, and an optional-claims write is a different concern that would
    # otherwise silently change every one of those numbers.
    $tag = ($Body.Keys -contains 'optionalClaims' -and $Body.Keys.Count -eq 1) ? 'CLAIMS ' : ''
    Add-Content -LiteralPath $script:PatchLog -Value ($tag + ($Body | ConvertTo-Json -Depth 30 -Compress))
    # Graph replaces a top-level property wholesale rather than merging into it - modelled, because that is the
    # other way this area goes wrong.
    foreach ($key in $Body.Keys) { $state[$key] = $Body[$key] }
    Set-StubState $state
    return @{}
}
'''


@pytest.fixture()
def env_files() -> Iterator[None]:
    written = write_env(ENV_NAME, {
        # Env must agree with the file name; common.ps1 checks and refuses otherwise.
        "Env": f"'{ENV_NAME}'",
        "EntraTenantId": f"'{TENANT}'", "EntraClientId": f"'{APP}'",
        "EntraAudience": f"'{APP}'", "EntraApiScope": f"'api://{APP}/access_as_user'",
        # Pinned empty: dev.psd1 names a real administrator, and inheriting it would make these tests assert
        # about whoever happens to be set up on this machine.
        "EntraGrantAdminTo": "''",
    })
    try:
        yield
    finally:
        for path in written:
            path.unlink(missing_ok=True)


def app_state(*, scopes: list[dict] | None = None, pre_auth: list[dict] | None = None,
              version: int | None = None, spa: list[str] | None = None,
              app_roles: list[dict] | None = None) -> dict:
    """An application object in the shape `az ad app show` returns, defaulting to the state that broke: created
    with `az ad app create`, identifier URI set, nothing else."""
    return {
        "id": "obj1", "appId": APP, "displayName": "RAG-OS", "signInAudience": "AzureADMyOrg",
        "identifierUris": [f"api://{APP}"], "appRoles": app_roles or [],
        "api": {"oauth2PermissionScopes": scopes or [], "preAuthorizedApplications": pre_auth or [],
                "requestedAccessTokenVersion": version},
        "spa": {"redirectUris": spa or []},
    }


def run_script(state: dict, *, args: str = "", fail_preauth: bool = False, no_app: bool = False,
               no_sp: bool = False, deny_assign: bool = False, work: Path | None = None,
               script: Path | None = None) -> dict:
    """Run the script with az stubbed. Returns stdout, exit code, the PATCH bodies sent, and the final app."""
    assert PWSH
    owned = work is None
    work = work or Path(tempfile.mkdtemp())
    state_path, log_path = work / "app.json", work / "patches.jsonl"
    if owned or not state_path.exists():
        state_path.write_text(json.dumps(state), encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    source = (script or SCRIPT).read_text(encoding="utf-8")
    marker = ". (Join-Path $PSScriptRoot 'common.ps1')"
    assert marker in source, "the script's common.ps1 dot-source moved"
    runner = work / "run.ps1"
    runner.write_text(source.replace(marker, f". '{COMMON.as_posix()}'\n{STUB}"), encoding="utf-8")

    # NO_COLOR keeps ANSI escapes out of the captured text, which matters twice: assertions match plain strings,
    # and a failure report is readable rather than a wall of escape codes.
    env = {**os.environ, "NO_COLOR": "1", "STUB_STATE": str(state_path), "STUB_PATCHLOG": str(log_path),
           "STUB_FQDN": FQDN, "STUB_GRAPH_400": GRAPH_400, "STUB_GRAPH_ORIGIN_400": GRAPH_ORIGIN_400,
           "STUB_ASSIGNMENTS": str(work / "assignments.json"), "STUB_ME": "my-object-id",
           "STUB_PRINCIPAL_NAME": "me@example.com",
           "STUB_EXTENSIONS": str(work / "extensions.json"), "STUB_CLIENT_ID": APP,
           "STUB_NO_SP": "1" if no_sp else "", "STUB_DENY_ASSIGN": "1" if deny_assign else "",
           "STUB_FAIL_PREAUTH": "1" if fail_preauth else "",
           "STUB_NO_APP": "1" if no_app else ""}
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(runner), "-Env", ENV_NAME, *args.split()],
                          capture_output=True, text=True, timeout=180, check=False, env=env, cwd=str(REPO))
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    patches = [json.loads(line) for line in lines
               if not line.startswith(("ASSIGN ", "REVOKE ", "EXTENSION ", "CLAIMS "))]
    optional_claims = [json.loads(line[len("CLAIMS "):]) for line in lines if line.startswith("CLAIMS ")]
    extensions = [json.loads(line[len("EXTENSION "):]) for line in lines if line.startswith("EXTENSION ")]
    assignments = [json.loads(line[len("ASSIGN "):]) for line in lines if line.startswith("ASSIGN ")]
    revokes = [line[len("REVOKE "):] for line in lines if line.startswith("REVOKE ")]
    return {"out": proc.stdout + proc.stderr, "exit": proc.returncode, "patches": patches,
            "assignments": assignments, "revokes": revokes, "extensions": extensions,
            "optional_claims": optional_claims,
            "app": json.loads(state_path.read_text(encoding="utf-8")), "work": work}


def flat(text: str) -> str:
    """Normalise a thrown PowerShell message before matching a phrase in it.

    PowerShell renders an error wrapped at the console width, prefixing each continuation with a ` | ` gutter, so
    "no longer exposes" arrives as "no longer" / gutter / "exposes" and a plain `in` check fails on text that is
    in fact present. NO_COLOR is set for the subprocess so there are no escape sequences to contend with as well,
    but the gutter remains.
    """
    text = re.sub("\x1b" + r"\[[0-9;]*m", "", text)   # in case NO_COLOR is not honoured
    text = re.sub(r"(?m)^\s*\|\s?", "", text)       # the error gutter
    return re.sub(r"\s+", " ", text)


def scope_ids(body: dict) -> list[str]:
    return [s["id"] for s in body.get("api", {}).get("oauth2PermissionScopes", [])]


def pre_auth_ids(body: dict) -> list[str]:
    return [i for e in body.get("api", {}).get("preAuthorizedApplications", [])
            for i in e["delegatedPermissionIds"]]


# ------------------------------------------------------------------------------------ the regression, end to end
# ------------------------------------------------------------------ Set-EntraAppRoleAssignment.ps1
# Granting somebody a role is the operation this deployment had no answer for: Set-EntraAppRegistration.ps1
# assigns rag.admin to one person as a side-effect of reconciling the registration, and nothing could list who
# holds what or take a role back. The preconditions below are most of the value - the failure modes they catch
# all produce Graph errors about ids and types that say nothing about what the operator should do next.

ROLE_ID = "role-admin-id"
ENABLED_APP_ROLES = [
    {"id": ROLE_ID, "value": "rag.admin", "displayName": "RAG-OS administrator", "isEnabled": True,
     "allowedMemberTypes": ["User"]},
    {"id": "role-contrib-id", "value": "rag.contributor", "displayName": "RAG-OS contributor", "isEnabled": True,
     "allowedMemberTypes": ["User"]},
]


def run_assign(state: dict, args: str, **kw: object) -> dict:
    return run_script(state, args=args, script=ASSIGN_SCRIPT, **kw)   # type: ignore[arg-type]


@needs_pwsh
def test_granting_a_role_assigns_it_once(env_files: None) -> None:
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me")
    assert result["exit"] == 0, result["out"]
    assert len(result["assignments"]) == 1, result["out"]
    assigned = result["assignments"][0]
    assert assigned["principalId"] == "my-object-id"
    assert assigned["appRoleId"] == ROLE_ID
    assert assigned["resourceId"] == "sp-object-id", "assignments hang off the enterprise application"


@needs_pwsh
def test_the_rag_prefix_is_optional(env_files: None) -> None:
    """Both spellings are in circulation: access-policy.yaml says `admin`, Entra and the token say `rag.admin`.
    Refusing either would be a trap for somebody copying from the other."""
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role admin -To me")
    assert result["exit"] == 0, result["out"]
    assert result["assignments"][0]["appRoleId"] == ROLE_ID


@needs_pwsh
def test_several_roles_are_granted_in_one_call(env_files: None) -> None:
    """The reason the stub had to store a list: with a single overwritten object this test could not fail."""
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin,rag.contributor -To me")
    assert result["exit"] == 0, result["out"]
    assert sorted(a["appRoleId"] for a in result["assignments"]) == sorted([ROLE_ID, "role-contrib-id"])


@needs_pwsh
def test_a_second_grant_of_the_same_role_writes_nothing(env_files: None) -> None:
    """Graph does not deduplicate - posting the same assignment twice produces two of them."""
    work = Path(tempfile.mkdtemp())
    first = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me", work=work)
    assert len(first["assignments"]) == 1
    second = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me", work=work)
    assert second["exit"] == 0, second["out"]
    assert second["assignments"] == [], "a role already held must not be assigned again"
    assert "already assigned" in second["out"]


# ---------------------------------------------------------------------------- the preconditions
@needs_pwsh
def test_a_role_that_does_not_exist_yet_is_refused_before_any_write(env_files: None) -> None:
    """The ordering rule. Graph rejects an unknown permission id with a message about ids, which says nothing
    about the roles never having been created - so this is caught here and names the script that creates them."""
    result = run_assign(app_state(app_roles=[]), "-Role rag.admin -To me")
    assert result["exit"] != 0
    assert result["assignments"] == [], "nothing may be written when a precondition fails"
    assert "Set-EntraAppRegistration.ps1" in flat(result["out"]), "the failure must name the fix"


@needs_pwsh
def test_a_disabled_role_is_refused(env_files: None) -> None:
    """A disabled role is present in the manifest and grants nothing. Assigning one looks like success and is
    not, which is worse than refusing."""
    disabled = [{**ENABLED_APP_ROLES[0], "isEnabled": False}]
    result = run_assign(app_state(app_roles=disabled), "-Role rag.admin -To me")
    assert result["exit"] != 0
    assert result["assignments"] == []


@needs_pwsh
def test_a_role_the_policy_does_not_map_is_refused_without_calling_graph(env_files: None) -> None:
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.auditor -To me")
    assert result["exit"] != 0
    assert "Unknown application role" in flat(result["out"])
    assert result["assignments"] == []


@needs_pwsh
def test_a_missing_app_registration_is_refused(env_files: None) -> None:
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me", no_app=True)
    assert result["exit"] != 0
    assert result["assignments"] == []


@needs_pwsh
def test_role_and_to_must_be_given_together(env_files: None) -> None:
    """Half a grant is a mistake, not a default - and silently listing instead would hide it."""
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin")
    assert result["exit"] != 0
    assert "together" in flat(result["out"])


# ---------------------------------------------------------------------------- revoke
@needs_pwsh
def test_revoking_deletes_the_assignment_by_its_own_id(env_files: None) -> None:
    work = Path(tempfile.mkdtemp())
    run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me", work=work)
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me -Remove", work=work)
    assert result["exit"] == 0, result["out"]
    assert result["revokes"] == ["assignment-1"], "revoke addresses the assignment, not the principal or the role"


@needs_pwsh
def test_revoking_a_role_nobody_holds_is_a_no_op(env_files: None) -> None:
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me -Remove")
    assert result["exit"] == 0, result["out"]
    assert result["revokes"] == []
    assert "nothing to revoke" in flat(result["out"])


# ---------------------------------------------------------------------------- the two read modes
@needs_pwsh
def test_listing_roles_names_the_ones_that_are_missing(env_files: None) -> None:
    """The direct answer to "I cannot see the four entries under App registrations - App roles"."""
    out = flat(run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-ListRoles")["out"])
    assert "rag.admin" in out and "rag.contributor" in out
    assert "Not defined: rag.sme, rag.reviewer" in out, out
    assert "Set-EntraAppRegistration.ps1" in out, "a missing role must name how to create it"


@needs_pwsh
def test_listing_roles_says_holders_are_unknown_without_a_service_principal(env_files: None) -> None:
    """"Nobody holds it" and "this run never looked" are different facts. Printing 0 for the second would be a
    confident statement about a number that was never fetched."""
    out = flat(run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-ListRoles", no_sp=True)["out"])
    assert "holders unknown" in out
    assert "0 holder(s)" not in out


@needs_pwsh
def test_listing_shows_role_values_rather_than_guids(env_files: None) -> None:
    work = Path(tempfile.mkdtemp())
    run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me", work=work)
    out = flat(run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-List", work=work)["out"])
    assert "rag.admin" in out and "me@example.com" in out
    assert ROLE_ID not in out, "a GUID tells the reader nothing; the role value is the answer"


@needs_pwsh
def test_no_arguments_prints_both_listings_and_writes_nothing(env_files: None) -> None:
    """Running it bare must be safe, and seeing the definitions above the assignments is what makes the
    two-objects distinction obvious."""
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "")
    assert result["exit"] == 0, result["out"]
    out = flat(result["out"])
    assert "Roles defined on the app registration" in out
    assert "Who holds which role" in out
    assert result["assignments"] == [] and result["patches"] == []


@needs_pwsh
def test_a_read_only_run_does_not_create_a_service_principal(env_files: None) -> None:
    """Listing is a question, not a change. Creating a directory object as a side-effect of asking one would be
    a surprise, and on a deployment that has never granted anything it is the normal state."""
    out = flat(run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-List", no_sp=True)["out"])
    assert "creating one" not in out
    assert "nothing has ever been assigned" in out


@needs_pwsh
def test_a_dry_run_writes_nothing(env_files: None) -> None:
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me -DryRun")
    assert result["exit"] == 0, result["out"]
    assert result["assignments"] == []
    assert "would assign" in flat(result["out"])


@needs_pwsh
def test_a_dry_run_with_no_enterprise_application_reports_rather_than_crashing(env_files: None) -> None:
    """A dry run must not create the enterprise application - that would not be dry - but the grant path then has
    no service principal id to work with. Reporting beats passing $null into a mandatory parameter, which fails
    with a binder error that names neither the object nor the reason."""
    result = run_assign(app_state(app_roles=ENABLED_APP_ROLES), "-Role rag.admin -To me -DryRun", no_sp=True)
    assert result["exit"] == 0, result["out"]
    assert result["assignments"] == []
    out = flat(result["out"])
    assert "would create the enterprise application" in out
    assert "would assign rag.admin" in out


@needs_pwsh
def test_help_lists_every_parameter_without_touching_azure(env_files: None) -> None:
    """-Help must work before anything is configured - it is what you run to find out how to configure it."""
    out = flat(run_assign(app_state(), "-Help")["out"])
    for parameter in ("-Env", "-Role", "-To", "-Remove", "-List", "-ListRoles", "-DryRun"):
        assert parameter in out, f"{parameter} is missing from -Help"
    assert "sign-in name" in out, "parameters must carry their purpose, not just their names"


# ------------------------------------------------------------------ wired into the deployment, not remembered
PROVISION_ALL = REPO / "infra" / "scripts" / "provision-all.ps1"


def provision_all() -> str:
    return PROVISION_ALL.read_text(encoding="utf-8")


def test_provisioning_configures_the_app_registration_before_it_validates_it() -> None:
    """The ordering IS the fix, and nothing else would catch a reordering.

    00-prereqs.ps1 FAILs when the app registration exposes no roles. So if provisioning ran step 00 first, a
    fresh deployment would abort on roles it was about to create one line later - a chicken-and-egg with no
    automated way out. The Entra step therefore has to run before step 00, not merely somewhere in the run.
    """
    text = provision_all()
    assert "Set-EntraAppRegistration.ps1" in text, (
        "provision-all.ps1 never runs Set-EntraAppRegistration.ps1, so the scope and the application roles are "
        "still something an operator has to remember - which is how they came to be missing twice")
    # Positional, but on the CONTROL FLOW rather than on where the filename first appears: the name now lives in
    # a variable declared near the top, so "first mention" says nothing about when it runs.
    pre_run = text.index("$From -le 0")
    loop = text.index("foreach ($number in $steps.Keys)")
    assert pre_run < loop, (
        "the Entra pre-run must happen before the step loop; step 00 FAILs on a registration with no roles, so "
        "running it first aborts the deployment on something the next action was about to create")


def test_the_redirect_uri_is_registered_after_the_chat_ui_exists() -> None:
    """The other half has the opposite constraint: the SPA redirect URI needs the chat UI FQDN, which only
    exists once step 07 has recorded it. One invocation cannot satisfy both, which is why there are two."""
    text = provision_all()
    calls = text.count("Invoke-ProvisionStep -Script $entraScript")
    assert calls == 2, (
        f"expected two Entra invocations - before 00 for the scope and roles, after 07 for the redirect URI - "
        f"but found {calls}. One cannot serve both: the first needs nothing from Azure, the second needs a "
        "chat UI FQDN that does not exist until step 07 records it")
    assert "$number -eq 7 -and $entraConfigured" in text, (
        "the redirect-URI pass must be pinned to step 7 completing, not run at some other point in the sequence")


@needs_pwsh
def test_the_configured_administrator_is_granted_without_a_command_line_flag(env_files: None) -> None:
    """The mechanism that makes provisioning able to grant at all: provision-all invokes every step as
    `& $script -Env $Env` and passes nothing else, so the recipient has to come from the psd1."""
    written = write_env(ENV_NAME, {
        "Env": f"'{ENV_NAME}'", "EntraTenantId": f"'{TENANT}'", "EntraClientId": f"'{APP}'",
        "EntraAudience": f"'{APP}'", "EntraApiScope": f"'api://{APP}/access_as_user'",
        "EntraGrantAdminTo": "'me'",
    })
    try:
        result = run_script(app_state(), args=f"-ChatUiFqdn {FQDN}")   # note: no -GrantAdminTo
        assert result["exit"] == 0, result["out"]
        assert len(result["assignments"]) == 1, f"the psd1 value was ignored\n{result['out']}"
        assert result["assignments"][0]["principalId"] == "my-object-id"
    finally:
        for path in written:
            path.unlink(missing_ok=True)


def run_provision_all(args: str) -> str:
    """Run provision-all.ps1 and return its output.

    Safe to execute for real: the callers below pass an inverted range, which selects no steps at all, so the
    only thing that runs is the header. Nothing reaches Azure, and the header is exactly what is under test.
    """
    assert PWSH
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(PROVISION_ALL), "-Env", ENV_NAME, *args.split()],
                          capture_output=True, text=True, timeout=120, check=False, cwd=str(REPO),
                          env={**os.environ, "NO_COLOR": "1"})
    return flat(proc.stdout + proc.stderr)


@needs_pwsh
def test_a_resume_that_skips_the_entra_pass_says_so(env_files: None) -> None:
    """Silence here reads as "done", not as "not attempted".

    The Entra pass runs before step 00, so any `-From` above 0 skips it. Without a word about that, the header
    still announces the range, every selected step reports OK, and the app registration is untouched - which is
    indistinguishable from a run that did the work. Somebody then re-runs steps 1-5 expecting the application
    roles, waits half an hour, and ends up exactly where they started.
    """
    out = run_provision_all("-From 5 -To 1")
    assert "Set-EntraAppRegistration.ps1" in out, (
        "a run that skips the Entra pass says nothing about it, so there is no way to tell it was skipped from "
        "a run where it succeeded")
    assert "-From" in out, "the message must say WHY it was skipped, not merely that it was"


@needs_pwsh
def test_the_two_reasons_for_skipping_entra_are_not_confused(env_files: None) -> None:
    """Empty settings and an out-of-range resume are different situations with different remedies, and the
    message for one is actively misleading about the other: the dev-auth deployment needs nothing done, while a
    resume needs the script run on its own."""
    out = run_provision_all("-From 5 -To 1")
    assert "dev-token sign-in only" not in out, (
        "this deployment has all four Entra* values set - reporting it as a dev-token deployment would send the "
        "reader to check settings that are already correct")


def test_extra_arguments_are_splatted_by_name_not_by_position() -> None:
    """Array splatting passes elements POSITIONALLY, so `-Arguments @('-Brief')` does not set a switch.

    It binds the literal string '-Brief' to the next positional parameter and leaves the switch false. On this
    script the next positional parameter is -ChatUiFqdn, so the run would have written a redirect URI of
    `https://-Brief/auth/callback` onto the app registration and reported success. A hashtable binds by name.
    """
    text = provision_all()
    assert "[hashtable]$Arguments" in text, (
        "Invoke-ProvisionStep must take a hashtable: an array splat passes '-Brief' as a positional value, "
        "silently setting -ChatUiFqdn to it and leaving the switch unset")
    assert "-Arguments @(" not in text, "an array splat binds positionally and will not set a switch"


def test_the_deploying_account_is_not_silently_made_an_administrator() -> None:
    """Creating the roles affects nobody; assigning one grants filter-bypass rights. It has to be named in the
    env file rather than falling to whoever happens to run the script, including CI."""
    assert "EntraGrantAdminTo" in (REPO / "infra" / "env" / "dev.sample.psd1").read_text(encoding="utf-8"), (
        "EntraGrantAdminTo must be declared in dev.sample.psd1 - Import-RagOsConfig loads that file as the "
        "schema, so an undeclared key only warns at load and then throws under StrictMode at first use")
    script = (REPO / "infra" / "scripts" / "Set-EntraAppRegistration.ps1").read_text(encoding="utf-8")
    assert "EntraGrantAdminTo" in script, (
        "the script must fall back to $Config.EntraGrantAdminTo: provision-all invokes every step as "
        "`& $script -Env $Env` and passes nothing else, so a command-line-only flag can never fire there")


@needs_pwsh
def test_a_new_scope_is_written_before_it_is_pre_authorised(env_files: None) -> None:
    """The exact run that failed against the real tenant. Two writes, and the first must not name the scope id it
    is creating - if it does, the stub rejects it the way Graph did and the scope never lands."""
    result = run_script(app_state(), args=f"-PreAuthorizeAzureCli -ChatUiFqdn {FQDN}")
    assert result["exit"] == 0, result["out"]
    assert len(result["patches"]) == 2, f"expected two writes, got {len(result['patches'])}\n{result['out']}"

    first, second = result["patches"]
    new_id = scope_ids(first)[0]
    assert pre_auth_ids(first) == [], "the first write must not pre-authorise the scope it is creating"
    assert new_id not in pre_auth_ids(first)

    # The second write pre-authorises, reusing the id the first one persisted rather than minting another.
    assert sorted(e["appId"] for e in second["api"]["preAuthorizedApplications"]) == sorted([APP, AZ_CLI])
    assert set(pre_auth_ids(second)) == {new_id}
    # And it restates the scopes, because `api` is replaced rather than merged.
    assert scope_ids(second) == [new_id]

    final = result["app"]
    assert [s["value"] for s in final["api"]["oauth2PermissionScopes"]] == ["access_as_user"]
    assert final["api"]["requestedAccessTokenVersion"] == 2
    assert final["spa"]["redirectUris"] == [CALLBACK]


@needs_pwsh
def test_the_stub_rejects_a_pre_authorisation_graph_would_reject(env_files: None) -> None:
    """Proves the stub has teeth, and covers the one case the fix cannot repair.

    An app already carrying a pre-authorisation for a permission it no longer defines fails every write that
    carries the entry forward — and it must be carried forward, because dropping it would delete other clients'
    entries, and because an id there may name an app role rather than a scope, so it cannot be filtered either.
    The script's job is to say so rather than emit a raw Graph error.
    """
    stale = app_state(pre_auth=[{"appId": OTHER_APP, "delegatedPermissionIds": ["a-permission-since-deleted"]}])
    result = run_script(stale, args=f"-ChatUiFqdn {FQDN}")
    assert result["exit"] != 0
    assert "no longer exposes" in flat(result["out"]), result["out"]
    assert OTHER_APP in result["out"], "the message must name which client to go and fix"
    assert result["patches"] == [], "nothing may be written when the write is rejected"


@needs_pwsh
def test_a_failing_pre_authorisation_does_not_block_sign_in(env_files: None) -> None:
    """The degraded path, and the reason it is worth having. Sign-in needs the scope and the redirect URI; the
    pre-authorisation only removes a consent prompt. Aborting here would report total failure for a deployment
    that works - which is how the cosmetic half of this script came to block the half that matters."""
    result = run_script(app_state(), args=f"-ChatUiFqdn {FQDN}", fail_preauth=True)
    assert result["exit"] == 0, result["out"]
    assert "SIGN-IN IS NOT BLOCKED" in result["out"]
    # The scope was still written, which is the entire point.
    assert [s["value"] for s in result["app"]["api"]["oauth2PermissionScopes"]] == ["access_as_user"]
    assert result["app"]["api"]["requestedAccessTokenVersion"] == 2


@needs_pwsh
def test_a_dry_run_writes_nothing_and_says_how_many_writes_it_would_make(env_files: None) -> None:
    result = run_script(app_state(), args=f"-DryRun -ChatUiFqdn {FQDN}")
    assert result["exit"] == 0, result["out"]
    assert result["patches"] == []
    assert "two writes" in result["out"], result["out"]
    assert result["app"] == app_state(), "the app object must be untouched"


@needs_pwsh
def test_a_second_run_changes_nothing(env_files: None) -> None:
    """Idempotency through the whole script, not just the body builder: the second run must make no Graph write at
    all, because re-sending an identical body would report success whatever the body contained."""
    work = Path(tempfile.mkdtemp())
    first = run_script(app_state(), args=f"-PreAuthorizeAzureCli -ChatUiFqdn {FQDN}", work=work)
    assert first["exit"] == 0 and len(first["patches"]) == 2

    second = run_script(app_state(), args=f"-PreAuthorizeAzureCli -ChatUiFqdn {FQDN}", work=work)
    assert second["exit"] == 0, second["out"]
    assert second["patches"] == [], "a re-run must not write"
    assert "Already correct" in second["out"]
    # The scope id must survive: a new one would silently invalidate every consent grant against the old one.
    assert scope_ids({"api": second["app"]["api"]}) == scope_ids(first["patches"][0])


@needs_pwsh
def test_the_app_roles_are_created_alongside_the_scope(env_files: None) -> None:
    """Without these nobody can upload or open the admin console, and nothing else in the deployment makes them.
    They carry no ordering constraint against the scope, so one write is enough."""
    result = run_script(app_state(), args=f"-ChatUiFqdn {FQDN}")
    assert result["exit"] == 0, result["out"]
    values = sorted(r["value"] for r in result["app"]["appRoles"])
    assert values == ["rag.admin", "rag.contributor", "rag.reviewer", "rag.sme"]
    assert "appRoles" in result["patches"][0], "the roles belong in the first write, not a later one"


@needs_pwsh
def test_the_stub_rejects_an_app_role_carrying_read_only_origin(env_files: None) -> None:
    """Proves the stub models the rule, and that the script does not trip it.

    Every role read back from Graph carries `origin`, and Graph refuses any write that includes it. A stub that
    accepted it would pass the naive implementation and this test would be decoration - the same mistake that let
    the preAuthorizedApplications ordering rule reach the tenant.
    """
    seeded = app_state(app_roles=[{
        "id": "keep-me", "value": "some.other.role", "displayName": "Other", "description": "d",
        "allowedMemberTypes": ["User"], "isEnabled": True, "origin": "Application"}])
    result = run_script(seeded, args=f"-ChatUiFqdn {FQDN}")
    assert result["exit"] == 0, f"the script sent a role carrying origin:\n{result['out']}"
    values = sorted(r["value"] for r in result["app"]["appRoles"])
    assert "some.other.role" in values and "rag.admin" in values
    assert all("origin" not in r for r in result["patches"][0]["appRoles"])


@needs_pwsh
def test_granting_admin_assigns_once_and_is_a_no_op_on_a_re_run(env_files: None) -> None:
    """Creating a role grants nobody anything; the assignment is a separate object, and posting an identical one
    twice creates a duplicate rather than being ignored."""
    work = Path(tempfile.mkdtemp())
    first = run_script(app_state(), args=f"-ChatUiFqdn {FQDN} -GrantAdminTo me", work=work)
    assert first["exit"] == 0, first["out"]
    assert len(first["assignments"]) == 1, first["out"]
    assigned = first["assignments"][0]
    assert assigned["principalId"] == "my-object-id"
    assert assigned["resourceId"] == "sp-object-id", "assignments hang off the service principal, not the app"
    role_id = next(r["id"] for r in first["app"]["appRoles"] if r["value"] == "rag.admin")
    assert assigned["appRoleId"] == role_id

    second = run_script(app_state(), args=f"-ChatUiFqdn {FQDN} -GrantAdminTo me", work=work)
    assert second["exit"] == 0
    assert second["assignments"] == [], "a second run must not assign again"
    assert "already assigned" in second["out"]


@needs_pwsh
def test_a_service_principal_is_created_when_the_app_has_none(env_files: None) -> None:
    """`az ad app create` makes an application, not a service principal, and the assignment needs the latter."""
    result = run_script(app_state(), args=f"-ChatUiFqdn {FQDN} -GrantAdminTo me", no_sp=True)
    assert result["exit"] == 0, result["out"]
    assert len(result["assignments"]) == 1
    assert "creating one" in result["out"]


@needs_pwsh
def test_a_refused_assignment_leaves_the_roles_created_and_exits_zero(env_files: None) -> None:
    """The grant needs AppRoleAssignment.ReadWrite.All on top of a directory role. If it is refused the
    registration is still usable and somebody can finish the job in the portal, so reporting total failure would
    be wrong - and would hide that the hard part already succeeded."""
    result = run_script(app_state(), args=f"-ChatUiFqdn {FQDN} -GrantAdminTo me", deny_assign=True)
    assert result["exit"] == 0, result["out"]
    assert "ROLES THEMSELVES ARE CREATED" in flat(result["out"])
    assert "Users and" in flat(result["out"]), "the portal fallback must be named"
    assert sorted(r["value"] for r in result["app"]["appRoles"])[0] == "rag.admin"


@needs_pwsh
def test_without_a_grant_the_script_says_nobody_can_administer_yet(env_files: None) -> None:
    """Creating the roles looks like success. It is not, until somebody holds one.

    The remedy names Set-EntraAppRoleAssignment.ps1 rather than -GrantAdminTo: the flag exists to bootstrap the
    first administrator during provisioning, while the companion script is what grants a role to anybody, at any
    time, and can also show who already holds one.
    """
    out = flat(run_script(app_state(), args=f"-ChatUiFqdn {FQDN}")["out"])
    assert "No role is assigned to anyone" in out
    assert "Set-EntraAppRoleAssignment.ps1" in out
    assert "-Role admin -To me" in out


@needs_pwsh
def test_a_missing_app_registration_says_how_to_create_one(env_files: None) -> None:
    result = run_script(app_state(), no_app=True)
    assert result["exit"] != 0
    assert "az ad app create" in result["out"]
    assert result["patches"] == []


@needs_pwsh
def test_an_already_exposed_scope_needs_only_one_write(env_files: None) -> None:
    """Nothing is deferred when the scope is already persisted, so the two-phase path must not become the cost of
    every run."""
    existing = app_state(scopes=[{"id": "already-there", "value": "access_as_user", "type": "User",
                                  "isEnabled": True}], version=2, spa=[CALLBACK])
    result = run_script(existing, args=f"-ChatUiFqdn {FQDN}")
    assert result["exit"] == 0, result["out"]
    assert len(result["patches"]) == 1, "only the pre-authorisation was missing - that is one write"
    assert pre_auth_ids(result["patches"][0]) == ["already-there"]


# ---------------------------------------------------------------- user attributes
# Without these, sign-in succeeds and every caller arrives with no department and no region. Both are
# required: true, so they read nothing at all - and there is no error anywhere saying why. The registration
# was correct and the tenant was silent; that is the failure this step exists to prevent.


@needs_pwsh
def test_the_three_user_attributes_are_created_as_directory_extensions(env_files: None) -> None:
    result = run_script(app_state(), args="")
    assert result["exit"] == 0, result["out"]
    created = {e["name"] for e in result["extensions"]}
    assert created == {"department", "region", "clearance"}, created
    for e in result["extensions"]:
        assert e["targetObjects"] == ["User"], "an extension has to target the user object to carry a claim"


@needs_pwsh
def test_the_extensions_are_emitted_as_access_token_claims(env_files: None) -> None:
    """Creating the attribute is half of it. Without the optional claim the value sits on the user object and
    never reaches a token, which looks identical from the application's side."""
    result = run_script(app_state(), args="")
    assert len(result["optional_claims"]) == 1, result["out"]
    names = {c["name"] for c in result["optional_claims"][0]["optionalClaims"]["accessToken"]}
    assert names == {f"extension_{APP.replace('-', '')}_{n}" for n in ("department", "region", "clearance")}, names


@needs_pwsh
def test_the_claims_registered_match_what_the_access_policy_reads(env_files: None) -> None:
    """Two halves of one setup, in two languages: the script creates the attributes, and access-policy.yaml
    names the claims. A mismatch means a caller with a department set still reads nothing."""
    import re as _re

    policy = (REPO / "config" / "access-policy" / "access-policy.yaml").read_text(encoding="utf-8")
    wanted = {m.group(1) for m in _re.finditer(r"entra:\s*extn\.([a-z_]+)", policy)}
    assert wanted, "the policy no longer reads any extn.* claim; re-point this test"
    created = {e["name"] for e in run_script(app_state(), args="")["extensions"]}
    assert created == wanted, (
        f"the script creates {sorted(created)} but access-policy.yaml reads {sorted(wanted)}")


@needs_pwsh
def test_a_re_run_creates_nothing_a_second_time(env_files: None) -> None:
    """Idempotent like every other step: an attribute that exists is left alone."""
    work = Path(tempfile.mkdtemp())
    first = run_script(app_state(), args="", work=work)
    assert len(first["extensions"]) == 3, first["out"]
    second = run_script(app_state(), args="", work=work)
    assert second["exit"] == 0, second["out"]
    assert second["extensions"] == [], "the extensions already existed"
    assert "already defined" in flat(second["out"])


@needs_pwsh
def test_skipping_the_attributes_still_reconciles_the_registration(env_files: None) -> None:
    """For a tenant where directory schema is governed separately, or an operator without the directory role -
    the rest of the registration must still complete, and the consequence must be stated."""
    result = run_script(app_state(), args="-SkipUserAttributes")
    assert result["exit"] == 0, result["out"]
    assert result["extensions"] == [] and result["optional_claims"] == []
    assert result["patches"], "the scope and roles must still be written"
    assert "no department or region" in flat(result["out"]), "the cost of skipping has to be said out loud"
