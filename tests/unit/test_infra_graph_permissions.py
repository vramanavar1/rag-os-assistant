"""Set-EntraGraphPermissions.ps1 against a stubbed Microsoft Graph.

The stub PAGES, deliberately. Graph returns 100 rows and a link to the rest, and the permissions this script
grants live in a tenant-wide collection, so a reader that stops at page one concludes "not granted" and posts a
duplicate - which Graph accepts, because it does not deduplicate. A stub that returned one tidy page would have
passed the broken version of this, and these tests would be decoration.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from .test_infra_readiness import write_env

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "infra" / "scripts"
SCRIPT = SCRIPTS / "Set-EntraGraphPermissions.ps1"
COMMON = SCRIPTS / "common.ps1"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(not PWSH, reason="pwsh (PowerShell 7) is not installed")

ENV_NAME = "zzgraph"
TENANT = "11111111-1111-1111-1111-111111111111"
APP = "22222222-2222-2222-2222-222222222222"
MI = "360e1d5b-01bc-4235-8c64-450035198c11"  # the managed identity's service-principal object id
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_SP = "graph-sp-object-id"

# Microsoft Graph's own permissions, as its service principal reports them.
GRAPH_ROLES = {
    "User.ReadWrite.All": "role-user-rw",
    "AppRoleAssignment.ReadWrite.All": "role-appraa-rw",
    "GroupMember.Read.All": "role-groupmember-r",
    "RoleManagement.ReadWrite.Directory": "role-rolemgmt-rw",  # present, and deliberately never asked for
}

STUB = r'''
$script:AssignPath = $env:STUB_ASSIGNMENTS
$script:PatchLog = $env:STUB_PATCHLOG

function Set-RagOsAzContext { param($Config) }
function Save-Outputs { param($Config, $Values, $Clear) }
function Get-Outputs { param($Config) return @{ identityPrincipalId = $env:STUB_MI } }

function Get-StubAssignments {
    (Test-Path $script:AssignPath) ?
        @(Get-Content $script:AssignPath -Raw | ConvertFrom-Json -AsHashtable) : @()
}
function Set-StubAssignments($rows) {
    (@($rows) | ConvertTo-Json -Depth 10 -AsArray) | Set-Content -LiteralPath $script:AssignPath -Encoding utf8
}

function Invoke-Az {
    param([string[]]$Arguments, [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    $joined = $Arguments -join ' '
    if ($joined -match 'ad sp create') {
        # Creating an enterprise application for Microsoft Graph is never correct. Fail loudly if asked.
        Add-Content -LiteralPath $script:PatchLog -Value 'SPCREATE'
        throw 'the stub refuses az ad sp create'
    }
    if ($joined -match 'ad sp show') {
        if ($env:STUB_NO_GRAPH_SP -eq '1') { return $null }
        $roles = @()
        foreach ($pair in ($env:STUB_GRAPH_ROLES | ConvertFrom-Json -AsHashtable).GetEnumerator()) {
            $roles += @{ id = $pair.Value; value = $pair.Key; isEnabled = $true }
        }
        return @{ id = $env:STUB_GRAPH_SP; appId = $env:STUB_GRAPH_APP_ID; appRoles = $roles }
    }
    return $null
}

function Invoke-AzRest {
    param([string]$Method, [string]$Url, [object]$Body, [switch]$AllowNotFound, [switch]$Sensitive)
    Add-Content -LiteralPath $script:PatchLog -Value "$($Method.ToUpper()) $Url"

    # ---- what the identity has been granted, read from its own side. PAGED: page 1 is filler plus a nextLink,
    # page 2 carries the real rows. A reader that stops at page one sees none of them.
    if ($Url -match 'appRoleAssignments') {
        if ($Url -match 'PAGE2') { return @{ value = @(Get-StubAssignments) } }
        $filler = @()
        for ($i = 1; $i -le 100; $i++) {
            $filler += @{ id = "filler-$i"; appRoleId = "someone-elses-role-$i"; resourceId = 'other-sp' }
        }
        return @{
            value            = $filler
            '@odata.nextLink' = "https://graph.microsoft.com/v1.0/servicePrincipals/$env:STUB_MI/appRoleAssignments?PAGE2"
        }
    }

    # ---- a consent is posted to the RESOURCE's appRoleAssignedTo; revoked from the identity's own collection.
    if ($Url -match 'appRoleAssignedTo') {
        if ($env:STUB_DENY -eq '1') { throw 'Forbidden: Authorization_RequestDenied - Insufficient privileges.' }
        $rows = @(Get-StubAssignments)
        $entry = @{}
        foreach ($k in $Body.Keys) { $entry[$k] = $Body[$k] }
        $entry['id'] = "assignment-$($rows.Count + 1)"
        Add-Content -LiteralPath $script:PatchLog -Value ('GRANT ' + ($Body | ConvertTo-Json -Depth 10 -Compress))
        Set-StubAssignments @($rows + $entry)
        return @{}
    }
    return @{}
}
'''


@pytest.fixture()
def env_files():
    written = write_env(ENV_NAME, {
        "Env": f"'{ENV_NAME}'",
        "EntraTenantId": f"'{TENANT}'", "EntraClientId": f"'{APP}'",
        "EntraAudience": f"'{APP}'", "EntraApiScope": f"'api://{APP}/access_as_user'",
        "EntraGrantAdminTo": "''",
    })
    try:
        yield
    finally:
        for path in written:
            path.unlink(missing_ok=True)


def run(args: str = "", *, held: list[dict] | None = None, deny: bool = False, no_graph_sp: bool = False,
        work: Path | None = None) -> dict:
    assert PWSH
    work = work or Path(tempfile.mkdtemp())
    assign_path, log_path = work / "assignments.json", work / "patches.jsonl"
    if not assign_path.exists():
        assign_path.write_text(json.dumps(held or []), encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    source = SCRIPT.read_text(encoding="utf-8")
    marker = ". (Join-Path $PSScriptRoot 'common.ps1')"
    assert marker in source, "the script's common.ps1 dot-source moved"
    runner = work / "run.ps1"
    runner.write_text(source.replace(marker, f". '{COMMON.as_posix()}'\n{STUB}"), encoding="utf-8")

    env = {**os.environ, "NO_COLOR": "1", "STUB_ASSIGNMENTS": str(assign_path),
           "STUB_PATCHLOG": str(log_path), "STUB_MI": MI, "STUB_GRAPH_SP": GRAPH_SP,
           "STUB_GRAPH_APP_ID": GRAPH_APP_ID, "STUB_GRAPH_ROLES": json.dumps(GRAPH_ROLES),
           "STUB_DENY": "1" if deny else "", "STUB_NO_GRAPH_SP": "1" if no_graph_sp else ""}
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(runner), "-Env", ENV_NAME, *args.split()],
                          capture_output=True, text=True, timeout=180, check=False, env=env, cwd=str(REPO))
    lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return {
        "out": proc.stdout + proc.stderr,
        "exit": proc.returncode,
        "grants": [json.loads(ln[len("GRANT "):]) for ln in lines if ln.startswith("GRANT ")],
        "calls": [ln for ln in lines if not ln.startswith("GRANT ")],
        "assignments": json.loads(assign_path.read_text(encoding="utf-8") or "[]"),
        "work": work,
    }


def flat(text: str) -> str:
    """PowerShell wraps a thrown message at the console width with a ` | ` gutter, so a plain substring check
    fails on text that is in fact present."""
    return " ".join(text.replace("|", " ").split())


# ---------------------------------------------------------------- granting


@needs_pwsh
def test_each_permission_is_granted_once_against_graphs_service_principal(env_files: None) -> None:
    result = run()
    assert result["exit"] == 0, result["out"]
    granted = {g["appRoleId"] for g in result["grants"]}
    assert granted == {GRAPH_ROLES[v] for v in
                       ("User.ReadWrite.All", "AppRoleAssignment.ReadWrite.All", "GroupMember.Read.All")}
    for g in result["grants"]:
        assert g["principalId"] == MI, "the grantee is the managed identity, not the app registration"
        assert g["resourceId"] == GRAPH_SP, "a consent hangs off the resource exposing the permission"


@needs_pwsh
def test_nothing_beyond_the_catalogue_is_ever_granted(env_files: None) -> None:
    """RoleManagement.ReadWrite.Directory exists on Graph and is the escalation this identity must never hold."""
    granted = {g["appRoleId"] for g in run()["grants"]}
    assert GRAPH_ROLES["RoleManagement.ReadWrite.Directory"] not in granted


@needs_pwsh
def test_a_re_run_grants_nothing_a_second_time(env_files: None) -> None:
    """The regression that matters. The stub pages, so a reader that stopped at page one would find none of the
    existing grants and post every one of them again - and Graph would accept all three, because it does not
    deduplicate. Two runs would leave six assignments."""
    work = Path(tempfile.mkdtemp())
    first = run(work=work)
    assert len(first["grants"]) == 3, first["out"]
    second = run(work=work)
    assert second["exit"] == 0, second["out"]
    assert second["grants"] == [], "a second run must write nothing"
    assert "already granted" in flat(second["out"])
    assert len(second["assignments"]) == 3, "and must not have stacked duplicates"


@needs_pwsh
def test_the_grants_already_held_are_read_from_the_identitys_own_side(env_files: None) -> None:
    """Asking Microsoft Graph's service principal who holds its roles returns every app permission consented
    anywhere in the tenant. Asking the identity what it has been granted returns a handful. The direction is a
    correctness decision, not a style one."""
    reads = [c for c in run()["calls"] if c.startswith("GET ")]
    assert reads, "the script must read before it writes"
    assert all("appRoleAssignments" in c for c in reads), (
        f"a read went to the wrong collection: {reads}")
    assert not any("appRoleAssignedTo" in c for c in reads), (
        "reading Graph's appRoleAssignedTo is the tenant-wide query this design exists to avoid")


@needs_pwsh
def test_every_page_of_existing_grants_is_read(env_files: None) -> None:
    reads = [c for c in run()["calls"] if c.startswith("GET ") and "appRoleAssignments" in c]
    assert any("PAGE2" in c for c in reads), f"@odata.nextLink was not followed: {reads}"


@needs_pwsh
def test_microsoft_graph_is_never_created_as_an_enterprise_application(env_files: None) -> None:
    """`az ad sp create` against Microsoft Graph is always a mistake; the stub throws if it is attempted."""
    result = run()
    assert "SPCREATE" not in result["calls"], "the script tried to create Microsoft Graph"
    assert result["exit"] == 0, result["out"]


# ---------------------------------------------------------------- opting out and reporting


@needs_pwsh
def test_the_dangerous_permission_can_be_left_out(env_files: None) -> None:
    """AppRoleAssignment.ReadWrite.All is tenant-wide and unscopable. A deployment may reasonably decline it and
    keep the attribute half of the page, leaving role assignment to Set-EntraAppRoleAssignment.ps1."""
    result = run("-SkipRoleAssignment")
    assert result["exit"] == 0, result["out"]
    granted = {g["appRoleId"] for g in result["grants"]}
    assert GRAPH_ROLES["AppRoleAssignment.ReadWrite.All"] not in granted
    assert GRAPH_ROLES["User.ReadWrite.All"] in granted


@needs_pwsh
def test_a_dry_run_writes_nothing(env_files: None) -> None:
    result = run("-DryRun")
    assert result["exit"] == 0, result["out"]
    assert result["grants"] == [] and result["assignments"] == []
    assert "would grant" in flat(result["out"])
    assert "Nothing was written" in flat(result["out"])


@needs_pwsh
def test_listing_reports_what_is_held_and_changes_nothing(env_files: None) -> None:
    held = [{"id": "a1", "appRoleId": GRAPH_ROLES["User.ReadWrite.All"], "resourceId": GRAPH_SP}]
    result = run("-List", held=held)
    assert result["exit"] == 0, result["out"]
    assert result["grants"] == []
    assert "User.ReadWrite.All" in result["out"]


@needs_pwsh
def test_removing_revokes_what_was_granted(env_files: None) -> None:
    held = [{"id": "a1", "appRoleId": GRAPH_ROLES["User.ReadWrite.All"], "resourceId": GRAPH_SP}]
    result = run("-Remove", held=held)
    assert result["exit"] == 0, result["out"]
    deletes = [c for c in result["calls"] if c.startswith("DELETE ")]
    assert len(deletes) == 1 and "a1" in deletes[0], result["calls"]
    assert f"servicePrincipals/{MI}/appRoleAssignments" in deletes[0], (
        "a revoke addresses the assignment on the identity's own collection")


# ---------------------------------------------------------------- failure and the step people skip


@needs_pwsh
def test_a_refusal_names_the_directory_role_that_is_needed(env_files: None) -> None:
    """Consenting to a privileged Graph permission needs Privileged Role Administrator or Global Administrator.
    Application Administrator and subscription Owner both look sufficient and are not."""
    result = run(deny=True)
    out = flat(result["out"])
    assert "PRIVILEGED ROLE ADMINISTRATOR" in out
    # Each wrong guess fails for a different reason, so naming the role is not enough - say why it is excluded.
    assert "Application Administrator" in out, "the role people wrongly expect to work must be named"
    assert "APP ROLES" in out, "the reason all three are excluded, not just the dangerous-looking one"
    assert "Azure RBAC" in out, "subscription Owner is the other wrong guess"
    assert "PIM" in out, "an eligible-but-inactive role fails identically and is easy to miss"
    assert "transitiveMemberOf" in out, "give a way to see what is actually active"
    # Bans the imperative, not the word: saying the portal CANNOT do this is the correct message, and used to be
    # the opposite - "Grant them in the portal under Enterprise applications -> ... -> Permissions".
    assert not re.search(r"[Gg]rant\s+(?:them|it|these|the\s+permissions?)\s+(?:in|from|via|through)\s+the\s+portal",
                         out), f"the portal has no UI for this, so it cannot be where a blocked operator is sent: {out}"
    assert "portal CANNOT" in out, "say plainly that the portal is not the route"
    assert "-List" in out, "give the read-only check that does work"


@needs_pwsh
def test_a_missing_graph_service_principal_fails_with_something_actionable(env_files: None) -> None:
    result = run(no_graph_sp=True)
    assert result["exit"] != 0
    assert "az account show" in flat(result["out"]), "the likely cause is the wrong tenant; say so"


@needs_pwsh
def test_a_successful_grant_demands_a_restart(env_files: None) -> None:
    """The identity caches its Graph token to expiry, so a container started before the grant keeps presenting a
    token without these permissions and every write fails with 403 for up to an hour. This is the step people
    skip and then spend an afternoon on."""
    out = flat(run()["out"])
    assert "RESTART" in out
    assert "07-container-apps.ps1" in out, "say how, not only that"
    assert "403" in out, "and what it looks like when skipped"


@needs_pwsh
def test_the_dev_auth_requirement_is_stated(env_files: None) -> None:
    """Dev tokens are self-asserted and the access policy trusts them for roles. With these permissions granted,
    leaving dev auth on means anyone who can reach the API can rewrite the tenant."""
    assert "DevAuthEnabled" in flat(run()["out"])


# ---------------------------------------------------------------- cross-artefact


def test_the_permission_catalogue_matches_what_the_api_requires() -> None:
    """Three copies of one list: the script that grants it, the adapter that fails without it, and the README an
    operator shows to whoever owns the tenant. Nothing links them, so this does."""
    ps = COMMON.read_text(encoding="utf-8")
    block = ps.split("$script:RagOsGraphPermissions = @(", 1)
    assert len(block) == 2, "the PowerShell permission catalogue moved; re-point this test"

    granted = set(re.findall(r"Value\s*=\s*'([^']+)'", block[1].split("\n)", 1)[0]))
    assert granted == {"User.ReadWrite.All", "AppRoleAssignment.ReadWrite.All", "GroupMember.Read.All"}, granted

    adapter = (REPO / "src" / "rag_os" / "infrastructure" / "directory" / "graph.py").read_text(encoding="utf-8")
    for permission in granted:
        assert permission in adapter, (
            f"{permission} is granted but the adapter's 403 message does not name it, so an operator debugging "
            f"a refusal is not told what is missing")
    for doc in ("README.md", "Deployment.md"):
        text = (REPO / doc).read_text(encoding="utf-8")
        for permission in granted:
            assert permission in text, f"{doc} does not document the {permission} permission this grants"


def test_the_service_principal_object_id_is_recorded_on_both_exit_paths() -> None:
    """Set-EntraAppRegistration.ps1 returns early when the registration is already correct. A deployment that was
    set up before this feature existed takes exactly that path, so recording the id only on the write path would
    leave it permanently unable to enable Settings (Security) - with nothing saying why."""
    text = (SCRIPTS / "Set-EntraAppRegistration.ps1").read_text(encoding="utf-8")
    calls = text.count("Save-ServicePrincipalId")
    assert calls >= 3, (
        f"expected the helper plus a call on each exit path, found {calls} mentions; if the script was "
        f"restructured, re-point this test")
    early, _, rest = text.partition("Save-ServicePrincipalId -DryRun")
    assert "return\n}" in early or "    return" in early, "the early-return path must record it too"
    assert "Save-ServicePrincipalId -DryRun" in rest, "and so must the write path"


def test_the_object_id_reaches_the_container() -> None:
    """Recorded and then never passed is the same as not recorded. It is optional on purpose - a deployment that
    does not administer people has no reason to hold it - so it must not be fetched with Get-Output, which
    throws."""
    text = (SCRIPTS / "07-container-apps.ps1").read_text(encoding="utf-8")
    assert "ENTRA_SERVICE_PRINCIPAL_OBJECT_ID" in text, "step 07 does not pass the object id to the app"
    assert "Get-Output -Config $Config -Name 'entraServicePrincipalObjectId'" not in text, (
        "Get-Output throws on a missing value, which would break every deployment that does not use this feature")


def test_the_grant_script_is_not_part_of_the_automated_run() -> None:
    """provision-all.ps1 must not grant tenant-wide Graph permissions on somebody's behalf. It needs a directory
    role a subscription owner does not have, and it is a decision, not a step."""
    text = (SCRIPTS / "provision-all.ps1").read_text(encoding="utf-8")
    assert "Set-EntraGraphPermissions" not in text, (
        "granting User.ReadWrite.All and AppRoleAssignment.ReadWrite.All must stay an explicit, separate act")
