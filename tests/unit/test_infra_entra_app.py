"""The Entra app registration: the one prerequisite that is a directory object, and the one that failed silently.

Sign-in to a correctly provisioned deployment returned:

    AADSTS65005: The application 'api://<app-id>' asked for scope 'access_as_user' that doesn't exist.

Nothing in the repo was wrong. Creating the registration was a scripted `az ad app create`; exposing the scope was
a comment next to it saying to open the portal. So one step of the two was skippable without any signal - no
provisioning step created the scope, and none checked for it.

Two things are tested here, both pure functions in common.ps1 so they can be exercised without a tenant:

* `Get-EntraAppPatch` - the Graph body. Its hard requirement is read-modify-write: a Graph PATCH **replaces** a
  complex property, so emitting a bare `api` object would delete every other exposed scope, every pre-authorised
  client, `knownClientApplications` and `acceptMappedClaims`. Most of these tests are about what must NOT change.
* `Get-EntraAppChecks` - the pre-flight verdicts 00-prereqs.ps1 prints, which decide whether provisioning stops.

Three traps have their own tests because each fails quietly rather than loudly:

1. `delegatedPermissionIds` is the Microsoft **Graph** name; `permissionIds` is the Azure AD Graph / portal
   Manifest blade name. Graph accepts a body carrying the wrong one and discards the value.
2. `ConvertTo-Json` renders a one-element collection that came off the pipeline as a bare scalar, so a single
   redirect URI would reach Graph as a string rather than an array.
3. A re-run must not mint a new scope GUID. A changed scope id silently invalidates every pre-authorisation and
   consent grant pointing at the old one.
4. A scope cannot be pre-authorised in the same write that creates it. Graph validates
   `preAuthorizedApplications` against the *already-persisted* permission set, so referencing a brand-new scope id
   is rejected with `InvalidValue ... cannot be found in the AppPermissions sets` - atomically, taking the new
   scope down with it. This one is not theoretical: it is what the first version of this script did.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMMON = REPO / "infra" / "scripts" / "common.ps1"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")

APP = "72f70e5a-291a-4c27-a6c9-1a7d1fbe7f9e"
OTHER_APP = "99999999-8888-7777-6666-555555555555"
TID = "c2ff8ba6-8824-4dbb-85d6-b12c6fc80d0c"
AZ_CLI = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
FQDN = "rag-chat-ui.example.westus.azurecontainerapps.io"
CALLBACK = f"https://{FQDN}/auth/callback"
# An app whose scope Graph has already persisted. The pre-authorisation tests below need this: on an app
# without the scope the pre-authorisation is deliberately deferred to a second write, so the first body
# carries none and there would be nothing for them to assert.
SCOPE_EXISTS = "@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $true })"


def run_ps(snippet: str) -> object:
    """Dot-source common.ps1 and run `snippet`, which must write its result as JSON on the LAST line.

    common.ps1 sets `Set-StrictMode -Version Latest`, so this also exercises the strict-mode behaviour these
    functions have to survive - a Graph application object omits every property that is unset, and under strict
    mode reading a missing key is a terminating error rather than $null.
    """
    assert PWSH
    script = f"$ErrorActionPreference = 'Stop'\n. '{COMMON.as_posix()}'\n{snippet}\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snippet.ps1"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoProfile", "-File", str(path)],
                              capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"pwsh exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, f"no output\nSTDERR:\n{proc.stderr}"
    return json.loads(lines[-1])


def ps_app(*, scopes: str = "@()", pre_auth: str = "@()", version: str = "$null",
           identifier_uris: str | None = None, spa: str = "@()", app_roles: str = "@()",
           sign_in: str = "'AzureADMyOrg'", extra_api: str = "") -> str:
    """A PowerShell literal for an application object in the shape `az ad app show` returns."""
    uris = identifier_uris if identifier_uris is not None else f"@('api://{APP}')"
    return (f"@{{ id = 'obj1'; appId = '{APP}'; displayName = 'RAG-OS'; signInAudience = {sign_in}; "
            f"identifierUris = {uris}; appRoles = {app_roles}; "
            f"api = @{{ requestedAccessTokenVersion = {version}; oauth2PermissionScopes = {scopes}; "
            f"preAuthorizedApplications = {pre_auth}{extra_api} }}; "
            f"spa = @{{ redirectUris = {spa} }} }}")


def patch(app: str, *, redirects: str = "@()", pre_authorize: str = "@()", app_roles: str = "@()") -> dict:
    result = run_ps(f"""
$p = Get-EntraAppPatch -App {app} -ClientId '{APP}' -ScopeName 'access_as_user' -AppIdUri 'api://{APP}' `
    -RedirectUris {redirects} -PreAuthorizeAppIds {pre_authorize} -AppRoles {app_roles} -AccessTokenVersion 2
@{{ Json = ($p.Body | ConvertTo-Json -Depth 30 -Compress)
    Changes = @($p.Changes | ForEach-Object {{ $_.What }})
    ScopeId = $p.ScopeId; ScopeExisted = $p.ScopeExisted; PreAuthDeferred = $p.PreAuthDeferred }} |
    ConvertTo-Json -Depth 10 -Compress
""")
    assert isinstance(result, dict)
    result["Body"] = json.loads(result["Json"])
    return result


# ----------------------------------------------------------------- reading the operator's configured scope string
@needs_pwsh
@pytest.mark.parametrize("scope,name", [
    (f"api://{APP}/access_as_user", "access_as_user"),
    (f"api://{TID}/{APP}/access_as_user", "access_as_user"),   # the api://<tenant-id>/<app-id> URI form
    ("https://contoso.com/productsapi/read", "read"),
    # A resource with no scope on the end. Returning the app id here would create a scope named after a GUID,
    # which is why this case is called out rather than left to fall through.
    (f"api://{APP}", None),
    ("", None),
    ("not-a-uri", None),
])
def test_the_scope_name_is_read_from_the_configured_identifier(scope: str, name: str | None) -> None:
    assert run_ps(f"(Get-EntraScopeName -Scope '{scope}') | ConvertTo-Json -Compress") == name


@needs_pwsh
def test_the_scope_resource_is_the_part_before_the_name() -> None:
    assert run_ps(f"(Get-EntraScopeResource -Scope 'api://{APP}/access_as_user') | ConvertTo-Json -Compress") \
        == f"api://{APP}"


@needs_pwsh
def test_a_value_is_unioned_case_insensitively_and_stays_a_collection() -> None:
    """Entra treats these identifiers case-insensitively, so a differently-cased duplicate must not be appended.

    `Values` is asserted to be an array even when it holds one item, which is what the `@()` wrapping is for. Note
    that `$array | ConvertTo-Json` collapses a one-element array to a scalar no matter how it was built - piping
    unrolls it - so array-ness is checked on the type here, and the JSON shape is checked where it actually
    matters, in test_a_single_redirect_uri_is_serialised_as_an_array, which serialises the enclosing object.
    """
    out = run_ps("""
@{ same     = (Add-UniqueValue -Existing @('https://A/cb') -Value 'https://a/cb').Added
   added    = (Add-UniqueValue -Existing @('https://a/cb') -Value 'https://b/cb').Added
   oneIsArr = ((Add-UniqueValue -Existing @() -Value 'https://a/cb').Values -is [array])
   oneCount = ((Add-UniqueValue -Existing @() -Value 'https://a/cb').Values).Count
   fromNull = ((Add-UniqueValue -Existing $null -Value 'x').Values).Count
   blanksGo = ((Add-UniqueValue -Existing @('a', '', $null) -Value 'b').Values).Count
} | ConvertTo-Json -Compress""")
    assert isinstance(out, dict)
    assert out["same"] is False, "a differently-cased duplicate was appended"
    assert out["added"] is True
    assert out["oneIsArr"] is True and out["oneCount"] == 1
    assert out["fromNull"] == 1
    assert out["blanksGo"] == 2, "empty entries must be dropped, not sent to Graph"


# ------------------------------------------------------------------------------------ building the Graph body
@needs_pwsh
def test_the_missing_scope_is_added_to_an_app_that_has_none() -> None:
    """The actual failure: an app created with `az ad app create` has no `api` content at all."""
    result = patch(ps_app(), redirects=f"@('{CALLBACK}')")
    scopes = result["Body"]["api"]["oauth2PermissionScopes"]
    assert [s["value"] for s in scopes] == ["access_as_user"]
    assert scopes[0]["isEnabled"] is True and scopes[0]["type"] == "User"
    assert scopes[0]["adminConsentDisplayName"] and scopes[0]["userConsentDisplayName"]
    assert result["ScopeExisted"] is False


@needs_pwsh
def test_the_body_uses_the_graph_spelling_of_the_permission_list() -> None:
    """`permissionIds` is the portal Manifest blade's name. Graph takes a body carrying it and drops the value,
    so the app would come back with an empty pre-authorisation list and a successful-looking call."""
    json_text = patch(ps_app(scopes=SCOPE_EXISTS, version="2"))["Json"]
    assert "delegatedPermissionIds" in json_text
    assert '"permissionIds"' not in json_text
    assert '"oauth2Permissions"' not in json_text   # the other Azure AD Graph spelling


@needs_pwsh
def test_a_brand_new_scope_is_not_pre_authorised_in_the_same_write() -> None:
    """The defect that reached the tenant. Graph answers that body with HTTP 400:

        InvalidValue: Property api.preAuthorizedApplications.delegatedPermissionIds has a Permission Id
        that cannot be found in the AppPermissions sets.

    `preAuthorizedApplications` is validated against the permission set **already persisted** on the application,
    not against the `oauth2PermissionScopes` in the same request body. A scope being created here therefore does
    not exist yet as far as that validation is concerned, and referencing its id rejects the whole PATCH
    atomically - taking the scope down with it, which is the part that actually matters.

    So the pre-authorisation is deferred to a second write, once the scope is persisted.
    """
    result = patch(ps_app(), pre_authorize=f"@('{AZ_CLI}')")
    assert result["ScopeExisted"] is False
    assert result["PreAuthDeferred"] is True, "a newly created scope must defer its pre-authorisation"
    # The scope itself must still be in this write - that is the whole point of the first PATCH.
    assert "access_as_user" in [s["value"] for s in result["Body"]["api"]["oauth2PermissionScopes"]]
    # And nothing in this body may point at the id this same body mints.
    for entry in result["Body"]["api"].get("preAuthorizedApplications", []):
        assert result["ScopeId"] not in entry["delegatedPermissionIds"], (
            f"{entry['appId']} is pre-authorised for the scope id this same PATCH creates - Graph rejects that")


@needs_pwsh
def test_existing_pre_authorisations_survive_the_deferral() -> None:
    """Deferring must not become omitting. `api` is replaced wholesale, so dropping the key would delete every
    pre-authorised client on the app - silently, and for whoever else depended on them."""
    app = ps_app(pre_auth=f"@(@{{ appId = '{OTHER_APP}'; delegatedPermissionIds = @('some-other-permission') }})")
    result = patch(app)
    entries = result["Body"]["api"]["preAuthorizedApplications"]
    assert [e["appId"] for e in entries] == [OTHER_APP]
    assert entries[0]["delegatedPermissionIds"] == ["some-other-permission"]


@needs_pwsh
def test_the_second_pass_pre_authorises_using_the_persisted_id() -> None:
    """What the script does after the scope is written: feed the re-read application back in. The id then comes
    from the directory rather than from this process, so the second write cannot reference one Graph does not
    have."""
    app = ps_app(scopes=("@(@{ id = 'persisted-by-graph'; value = 'access_as_user'; type = 'User'; "
                         "isEnabled = $true })"), version="2")
    result = patch(app, pre_authorize=f"@('{AZ_CLI}')")
    assert result["ScopeExisted"] is True and result["PreAuthDeferred"] is False
    entries = result["Body"]["api"]["preAuthorizedApplications"]
    assert sorted(e["appId"] for e in entries) == sorted([APP, AZ_CLI])
    for entry in entries:
        assert entry["delegatedPermissionIds"] == ["persisted-by-graph"]
    # The scopes are restated even though they did not change, because `api` is replaced as a whole.
    assert [s["value"] for s in result["Body"]["api"]["oauth2PermissionScopes"]] == ["access_as_user"]


@needs_pwsh
def test_the_access_token_version_is_set_explicitly() -> None:
    """Left at null it means 1, and the token version decides both `iss` and `aud`. Not setting it is what put a
    401 `untrusted issuer` behind the AADSTS65005."""
    assert patch(ps_app())["Body"]["api"]["requestedAccessTokenVersion"] == 2


@needs_pwsh
def test_a_single_redirect_uri_is_serialised_as_an_array() -> None:
    body = patch(ps_app(), redirects=f"@('{CALLBACK}')")["Body"]
    assert isinstance(body["spa"]["redirectUris"], list)
    assert body["spa"]["redirectUris"] == [CALLBACK]


@needs_pwsh
def test_the_client_is_pre_authorised_for_its_own_scope() -> None:
    result = patch(ps_app(scopes=SCOPE_EXISTS, version="2"))
    entries = result["Body"]["api"]["preAuthorizedApplications"]
    assert [e["appId"] for e in entries] == [APP]
    # The id must be the scope's own, not a fresh GUID: a pre-authorisation pointing at a non-existent permission
    # is accepted by Graph and simply never applies, so the consent prompt would come back with nothing to show why.
    assert entries[0]["delegatedPermissionIds"] == [result["ScopeId"]]


@needs_pwsh
def test_the_azure_cli_is_pre_authorised_only_when_asked() -> None:
    """Opt-in on purpose: it lets anyone in the tenant who can run az obtain a token for this API. It exists
    because the documented verification command and 09-smoke.ps1 both need a token and cannot get one otherwise."""
    app = ps_app(scopes=SCOPE_EXISTS, version="2")
    without = patch(app)["Body"]["api"]["preAuthorizedApplications"]
    assert AZ_CLI not in [e["appId"] for e in without]
    with_cli = patch(app, pre_authorize=f"@('{AZ_CLI}')")["Body"]["api"]["preAuthorizedApplications"]
    assert sorted(e["appId"] for e in with_cli) == sorted([APP, AZ_CLI])


@needs_pwsh
def test_the_app_id_uri_is_added_when_absent() -> None:
    result = patch(ps_app(identifier_uris="@()"))
    assert result["Body"]["identifierUris"] == [f"api://{APP}"]
    assert "identifierUris" in result["Changes"]


# ------------------------------------------------------------------------------ application roles
# Nothing in the deployment created these, which is why a freshly signed-in user is told
# "uploading requires the contributor or admin role" and cannot reach the admin console either. The value is
# what Entra puts in the `roles` claim; access-policy.yaml maps it to an internal role.
ALL_ROLES = "@('rag.admin','rag.contributor','rag.sme','rag.reviewer')"
# The same four, as they come back from `az ad app show`: exposed and enabled on the registration.
EXPOSED_ROLES = ("@(@{ value = 'rag.admin'; id = 'r1'; isEnabled = $true }, "
                 "@{ value = 'rag.contributor'; id = 'r2'; isEnabled = $true }, "
                 "@{ value = 'rag.sme'; id = 'r3'; isEnabled = $true }, "
                 "@{ value = 'rag.reviewer'; id = 'r4'; isEnabled = $true })")


@needs_pwsh
def test_the_app_roles_are_created_with_the_shape_entra_needs() -> None:
    body = patch(ps_app(), app_roles=ALL_ROLES)["Body"]
    roles = body["appRoles"]
    assert sorted(r["value"] for r in roles) == ["rag.admin", "rag.contributor", "rag.reviewer", "rag.sme"]
    for role in roles:
        assert role["allowedMemberTypes"] == ["User"], "must be assignable to people, not only to applications"
        assert role["isEnabled"] is True, "Graph requires isEnabled true on create"
        assert role["displayName"] and role["description"], "both are shown in the assignment UI"
        assert role["id"], "a new app role needs a GUID of its own"


@needs_pwsh
def test_an_existing_role_is_rewritten_without_its_read_only_origin() -> None:
    """The trap, and the reason this is tested rather than assumed.

    `origin` says whether a role is defined on the application or on the service principal. It is read-only, and
    the Graph reference is explicit that it "must *not* be included in any POST or PATCH requests" — but it comes
    back on every read. So the obvious implementation, echoing what was read, fails with a 400 that names the
    whole `appRoles` collection rather than the offending key. Exactly the class of unmodelled server rule that
    produced the preAuthorizedApplications failure.
    """
    existing = ("@(@{ id = 'keep-me'; value = 'some.other.role'; displayName = 'Other'; description = 'd'; "
                "allowedMemberTypes = @('User'); isEnabled = $true; origin = 'Application' })")
    result = patch(ps_app(app_roles=existing), app_roles="@('rag.admin')")
    assert '"origin"' not in result["Json"], "a read-only property was echoed back; Graph rejects the whole write"
    values = [r["value"] for r in result["Body"]["appRoles"]]
    assert "some.other.role" in values, "an unrelated role must survive the wholesale replacement"
    assert "rag.admin" in values


@needs_pwsh
def test_an_existing_role_keeps_its_id() -> None:
    """A regenerated id silently invalidates every assignment pointing at the old one — people would keep their
    assignment and lose the role, with nothing to show why."""
    existing = ("@(@{ id = 'assigned-to-people'; value = 'rag.admin'; displayName = 'Admin'; description = 'd'; "
                "allowedMemberTypes = @('User'); isEnabled = $true })")
    result = patch(ps_app(app_roles=existing), app_roles="@('rag.admin')")
    assert "appRoles" not in result["Body"], "nothing changed, so appRoles must not be re-sent at all"
    assert "appRoles" not in result["Changes"]


@needs_pwsh
def test_a_role_the_policy_does_not_map_is_refused() -> None:
    """access-policy.yaml turns a value into an internal role. Creating one it does not map produces a role that
    can be assigned, appears in the token, and gates nothing — worse than not having it, because it looks done."""
    with pytest.raises(AssertionError, match="Unknown application role"):
        patch(ps_app(), app_roles="@('rag.auditor')")


@needs_pwsh
def test_the_role_catalogue_matches_the_access_policy() -> None:
    """The one guard that keeps the script honest: common.ps1 holds the list because PowerShell has no YAML
    reader, so nothing but this test stops it drifting from the policy that gives the values meaning."""
    policy = (REPO / "config" / "access-policy" / "access-policy.yaml").read_text(encoding="utf-8")
    in_policy = sorted(set(re.findall(r"rag\.[a-z]+", policy)))
    common = COMMON.read_text(encoding="utf-8")
    block = common.split("$script:RagOsEntraAppRoles = @(")[1].split("\n)")[0]
    in_script = sorted(set(re.findall(r"rag\.[a-z]+", block)))
    assert in_policy == in_script, (
        f"access-policy.yaml maps {in_policy} but common.ps1 offers {in_script} - a value in one and not the "
        "other is either a role nobody can be granted or a role that gates nothing")


@needs_pwsh
def test_the_documented_role_values_match_the_access_policy() -> None:
    """A permissions table that has drifted from the policy is worse than none: somebody creates the role it
    names, assigns it, and it gates nothing. README.md and README.html are hand-maintained copies of each other,
    which is exactly how a value ends up documented in one place and real in neither.

    Scoped to the table rows on purpose. A blanket sweep for `rag.*` also finds the `rag.auditor` counter-example
    in the "inventing a role gates nothing" warning, the deliberately misspelled `rag.contrbutor`, and the
    `rag.chat` / `rag.tokens` metric names - none of which are claims about what exists.
    """
    policy = (REPO / "config" / "access-policy" / "access-policy.yaml").read_text(encoding="utf-8")
    in_policy = sorted(set(re.findall(r"rag\.[a-z]+", policy)))
    assert in_policy, "no role values found in access-policy.yaml - this test would pass vacuously"

    md_rows = re.findall(r"^\|\s*`(rag\.[a-z]+)`", (REPO / "README.md").read_text(encoding="utf-8"), re.M)
    assert sorted(set(md_rows)) == in_policy, (
        f"README.md's role table lists {sorted(set(md_rows))}, access-policy.yaml maps {in_policy}")

    html = (REPO / "README.html").read_text(encoding="utf-8")
    html_rows = re.findall(r"<tr><td><code>(rag\.[a-z]+)</code></td>", html)
    assert sorted(set(html_rows)) == in_policy, (
        f"README.html's role table lists {sorted(set(html_rows))}, access-policy.yaml maps {in_policy}")


# ---------------------------------------------------------------------- what must survive the read-modify-write
@needs_pwsh
def test_an_unrelated_scope_and_the_rest_of_the_api_object_survive() -> None:
    """A Graph PATCH replaces `api` outright. Everything asserted here would be deleted by a naive write, and
    nothing would report it - the app would simply stop working for whoever depended on it."""
    app = ps_app(
        scopes=("@(@{ id = 'keep'; value = 'other.read'; type = 'User'; isEnabled = $true }, "
                "@{ id = 'existing'; value = 'access_as_user'; type = 'Admin'; isEnabled = $false; "
                "adminConsentDisplayName = 'Wording set by hand' })"),
        pre_auth=f"@(@{{ appId = '{OTHER_APP}'; delegatedPermissionIds = @('keep') }})",
        version="2", spa="@('http://localhost:8080/auth/callback')",
        extra_api=("; acceptMappedClaims = $true; "
                   "knownClientApplications = @('aaaabbbb-0000-cccc-1111-dddd2222eeee')"))
    result = patch(app, redirects=f"@('{CALLBACK}')")
    api = result["Body"]["api"]

    assert api["acceptMappedClaims"] is True
    assert api["knownClientApplications"] == ["aaaabbbb-0000-cccc-1111-dddd2222eeee"]
    assert "other.read" in [s["value"] for s in api["oauth2PermissionScopes"]]
    assert OTHER_APP in [e["appId"] for e in api["preAuthorizedApplications"]]
    assert "http://localhost:8080/auth/callback" in result["Body"]["spa"]["redirectUris"]
    assert CALLBACK in result["Body"]["spa"]["redirectUris"]


@needs_pwsh
def test_an_existing_scope_keeps_its_id_and_its_wording() -> None:
    """Minting a new GUID would invalidate every pre-authorisation and consent grant pointing at the old id, and
    rewriting the consent text would report a change on every single run."""
    app = ps_app(scopes=("@(@{ id = 'existing'; value = 'access_as_user'; type = 'Admin'; isEnabled = $false; "
                         "adminConsentDisplayName = 'Wording set by hand' })"), version="2")
    result = patch(app)
    assert result["ScopeId"] == "existing" and result["ScopeExisted"] is True
    scope = next(s for s in result["Body"]["api"]["oauth2PermissionScopes"] if s["value"] == "access_as_user")
    assert scope["adminConsentDisplayName"] == "Wording set by hand"
    # But the two fields that decide whether it can be requested at all are forced.
    assert scope["isEnabled"] is True and scope["type"] == "User"


@needs_pwsh
def test_an_already_correct_registration_produces_no_body_at_all() -> None:
    """The idempotency property, and the reason the script must not PATCH when Changes is empty: re-sending an
    identical body would report success whether or not this function computed anything sensible."""
    app = ps_app(scopes=("@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $true })"),
                 pre_auth=f"@(@{{ appId = '{APP}'; delegatedPermissionIds = @('sid') }})",
                 version="2", spa=f"@('{CALLBACK}')")
    result = patch(app, redirects=f"@('{CALLBACK}')")
    assert result["Changes"] == [] and result["Body"] == {}
    assert result["ScopeId"] == "sid"


@needs_pwsh
def test_a_failed_graph_write_reports_the_body_not_a_deleted_temp_file() -> None:
    """When Graph rejects a body, the body is the only useful evidence - and it was the one thing not shown.

    `az` echoes the command it ran, which names the temp file the body was written to, and `Invoke-AzRest` deletes
    that file in its `finally` block. So the first run of this against a real tenant produced an error pointing at
    `--body @...\tmp4dphhmpt.tmp`, a path that no longer existed by the time anyone read it.
    """
    out = run_ps("""
function Invoke-Az {
    param([string[]]$Arguments, [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    throw 'az command failed (exit 1): az rest --method patch --body @/tmp/tmpdeleted.tmp ERROR: Bad Request'
}
$plain = ''
try {
    $null = Invoke-AzRest -Method patch -Url 'https://graph.microsoft.com/v1.0/applications/x' `
        -Body @{ api = @{ requestedAccessTokenVersion = 2 } }
}
catch { $plain = $_.Exception.Message }
$hidden = ''
try { $null = Invoke-AzRest -Method patch -Url 'https://x' -Body @{ token = 'do-not-print-me' } -Sensitive }
catch { $hidden = $_.Exception.Message }
@{ Plain = $plain; Hidden = $hidden } | ConvertTo-Json -Compress""")
    assert isinstance(out, dict)
    assert "Request body sent:" in out["Plain"]
    assert '"requestedAccessTokenVersion":2' in out["Plain"], out["Plain"]
    # -Sensitive is how a caller says the body is a credential; then nothing is appended.
    assert "Request body sent:" not in out["Hidden"]
    assert "do-not-print-me" not in out["Hidden"]


# ------------------------------------------------------------------------------- the 00-prereqs pre-flight verdicts
def checks(*, audience: str = APP, app: str = "$null", fqdn: str = FQDN,
           scope: str | None = None, admin_assignments: int = -1) -> dict[str, str]:
    scope_str = scope if scope is not None else f"api://{APP}/access_as_user"
    out = run_ps(f"""
$cfg = @{{ EntraTenantId = '{TID}'; EntraClientId = '{APP}'; EntraAudience = '{audience}'
           EntraApiScope = '{scope_str}'; DevAuthEnabled = $false }}
$r = @{{}}
foreach ($c in (Get-EntraAppChecks -Config $cfg -App {app} -ChatUiFqdn '{fqdn}' `
            -AdminAssignments {admin_assignments} -Env 'dev')) {{ $r[$c.Item] = $c.Status }}
$r | ConvertTo-Json -Compress -Depth 5""")
    assert isinstance(out, dict)
    return {str(k): str(v) for k, v in out.items()}


@needs_pwsh
def test_the_missing_scope_is_a_hard_failure_before_anything_is_provisioned() -> None:
    """The whole point of the pre-flight: this is the state the deployment was actually in, and provisioning
    succeeded anyway. FAIL is what makes 00-prereqs.ps1 throw."""
    verdicts = checks(app=ps_app(), audience=f"api://{APP}")
    assert verdicts["Scope 'access_as_user' exposed"] == "FAIL"


@needs_pwsh
def test_a_disabled_scope_fails_too() -> None:
    app = ps_app(scopes="@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $false })")
    assert checks(app=app)["Scope 'access_as_user' exposed"] == "FAIL"


@needs_pwsh
def test_a_correctly_configured_registration_has_no_failures() -> None:
    """Note what "correctly configured" now includes: the application roles. A registration with a working scope
    and no roles signs people in and then refuses every upload, which is not a configured deployment."""
    app = ps_app(scopes="@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $true })",
                 pre_auth=f"@(@{{ appId = '{APP}'; delegatedPermissionIds = @('sid') }})",
                 app_roles=EXPOSED_ROLES,
                 version="2", spa=f"@('{CALLBACK}')")
    assert "FAIL" not in checks(app=app, admin_assignments=1).values()


@needs_pwsh
@pytest.mark.parametrize("version,audience,expected", [
    # rag-api derives both audience spellings from ENTRA_AUDIENCE, so either pairing is legitimate and neither
    # may be reported as broken - a check that forced one would fail correct deployments.
    ("2", APP, "PASS"),
    ("2", f"api://{APP}", "PASS"),
    ("$null", f"api://{APP}", "PASS"),
    ("$null", APP, "PASS"),
    ("1", f"api://{APP}", "PASS"),
    # No app id in the audience at all, so nothing can derive the bare form version 2 will stamp.
    ("2", "https://contoso.com/productsapi", "FAIL"),
])
def test_the_audience_and_token_version_pairing(version: str, audience: str, expected: str) -> None:
    app = ps_app(scopes="@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $true })",
                 version=version, identifier_uris=f"@('api://{APP}', 'https://contoso.com/productsapi')")
    assert checks(app=app, audience=audience)["ENTRA_AUDIENCE matches the token version"] == expected


@needs_pwsh
def test_an_audience_naming_a_different_application_always_fails() -> None:
    app = ps_app(scopes="@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $true })",
                 version="2")
    assert checks(app=app, audience=f"api://{OTHER_APP}")["ENTRA_AUDIENCE names this app"] == "FAIL"


@needs_pwsh
def test_personal_accounts_require_token_version_two() -> None:
    """Stated as a requirement in the manifest reference, not a preference."""
    app = ps_app(scopes="@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $true })",
                 sign_in="'AzureADandPersonalMicrosoftAccount'", version="$null")
    assert checks(app=app, audience=f"api://{APP}")["signInAudience vs token version"] == "FAIL"


@needs_pwsh
def test_no_application_roles_is_a_hard_failure() -> None:
    """The state that produced "uploading requires the contributor or admin role" after a successful sign-in.
    Nothing reported it until somebody tried to upload; now provisioning stops."""
    assert checks(app=ps_app())["Application roles exposed"] == "FAIL"


@needs_pwsh
def test_a_partial_set_of_roles_is_a_warning_not_a_failure() -> None:
    """Some roles present means the deployment works for whoever holds them - it is incomplete, not broken."""
    partial = ps_app(app_roles="@(@{ value = 'rag.admin'; id = 'r1'; isEnabled = $true })")
    assert checks(app=partial)["Application roles exposed"] == "WARN"


@needs_pwsh
def test_a_full_set_of_enabled_roles_passes() -> None:
    assert checks(app=ps_app(app_roles=EXPOSED_ROLES))["Application roles exposed"] == "PASS"


@needs_pwsh
def test_a_disabled_role_does_not_count_as_exposed() -> None:
    """A disabled role is present in the manifest and grants nothing, which reads as configured but is not."""
    disabled = ps_app(app_roles="@(@{ value = 'rag.admin'; id = 'r1'; isEnabled = $false })")
    assert checks(app=disabled)["Application roles exposed"] == "FAIL"


@needs_pwsh
@pytest.mark.parametrize("assignments,expected", [(0, "WARN"), (1, "PASS"), (3, "PASS")])
def test_a_role_nobody_holds_is_reported(assignments: int, expected: str) -> None:
    """Creating a role grants nobody anything. A deployment where no one holds rag.admin is administrable by
    nobody - recoverable at any time, so a warning rather than a stop."""
    app = ps_app(app_roles=EXPOSED_ROLES)
    assert checks(app=app, admin_assignments=assignments)["Someone holds rag.admin"] == expected


@needs_pwsh
def test_the_assignment_check_is_skipped_when_it_could_not_be_looked_up() -> None:
    """Listing assignments needs a Graph call this function deliberately does not make, and an app with no
    service principal yet is the normal state. Guessing would report a problem that may not exist."""
    app = ps_app(app_roles=EXPOSED_ROLES)
    assert "Someone holds rag.admin" not in checks(app=app, admin_assignments=-1)


@needs_pwsh
def test_a_missing_app_registration_is_reported_once_and_nothing_else_is_guessed() -> None:
    verdicts = checks(app="$null")
    assert verdicts == {"Entra app registration exists": "FAIL"}


@needs_pwsh
def test_the_redirect_uri_is_not_checked_before_the_chat_ui_exists() -> None:
    """Before step 07 there is no FQDN, and warning about it then would train the operator to ignore the check."""
    app = ps_app(scopes="@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $true })",
                 version="2")
    assert "SPA redirect URI" not in checks(app=app, fqdn="")
    assert checks(app=app, fqdn=FQDN)["SPA redirect URI"] == "WARN"


@needs_pwsh
def test_a_scope_resource_that_is_not_an_identifier_uri_fails() -> None:
    """This is AADSTS500011 rather than 65005 - a different error message for the same omission, so it gets its
    own check instead of being left to look like a missing scope."""
    app = ps_app(scopes="@(@{ id = 'sid'; value = 'access_as_user'; type = 'User'; isEnabled = $true })",
                 identifier_uris=f"@('api://{OTHER_APP}')", version="2")
    assert checks(app=app)["Scope resource is an identifierUri"] == "FAIL"


@needs_pwsh
def test_a_scope_setting_with_no_scope_name_fails() -> None:
    assert checks(app=ps_app(), scope=f"api://{APP}")["EntraApiScope names a scope"] == "FAIL"
