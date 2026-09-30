<#
.SYNOPSIS
    Grants the managed identity the Microsoft Graph application permissions Settings (Security) needs.

.DESCRIPTION
    Settings (Security) writes a person's department, region and clearance, and this application's app-role
    assignments, directly to Microsoft Entra ID. The API calls Graph as the user-assigned managed identity, so
    the permissions belong to that identity - not to the app registration, which is a different object with a
    different purpose. Set-EntraAppRegistration.ps1 handles the registration; this handles the identity.

    Read what it is about to do before running it:

    * AppRoleAssignment.ReadWrite.All CANNOT BE SCOPED to one application. It permits granting any app role on
      any service principal in the tenant, Microsoft Graph's own included, which means an identity holding it can
      grant itself Global Administrator. It lands on the same identity that already holds this deployment's
      Search, Blob and PostgreSQL data-plane roles, so a compromise of the API becomes a compromise of the
      tenant. What keeps it narrow is code, not Entra: the API only ever posts its own service principal as the
      resourceId and only ever uses appRoleIds read from its own registration.
    * Consenting to these needs PRIVILEGED ROLE ADMINISTRATOR (the least-privileged role that works) or
      GLOBAL ADMINISTRATOR. Application Administrator and Cloud Application Administrator are NOT enough, and
      the reason covers all three permissions rather than one of them: they are Microsoft Graph *app roles*, and
      those roles can consent to any API EXCEPT Microsoft Graph application permissions. So -SkipRoleAssignment
      reduces what you grant, not the role you need. Subscription Owner is Azure RBAC and grants nothing here.
      A role held through PIM must be ACTIVATED first; eligible-but-inactive fails the same way as no role.

      Note the contrast with Set-EntraAppRoleAssignment.ps1, which assigns THIS application's rag.* roles to a
      person through the same POST /servicePrincipals/{id}/appRoleAssignedTo call and needs only Application
      Administrator. The privilege required depends on which service principal is the resource, not on the call.
    * DEV_AUTH_ENABLED must be false wherever this is granted. Dev tokens are self-asserted and the access
      policy trusts them for roles, so with these permissions in hand anyone who can reach the API could grant
      themselves any role. The API refuses to start the directory adapter when dev auth is on.

    If you would rather not grant AppRoleAssignment.ReadWrite.All, leave it out with -SkipRoleAssignment. The
    attribute half of the page (department, region, clearance) then works and role assignment stays a terminal
    job for Set-EntraAppRoleAssignment.ps1.

    Idempotent, like every other step: an assignment that already exists is left alone. Re-run it as often as
    you like and it will report "already assigned" rather than stacking duplicates - Graph does not deduplicate,
    so that check is the whole reason this reads before it writes.

.PARAMETER Env
    Which infra/env/<Env>.psd1 to read. Defaults to dev.

.PARAMETER SkipRoleAssignment
    Do not grant AppRoleAssignment.ReadWrite.All. Attributes still work; app roles stay a script-only job.

.PARAMETER Remove
    Revoke the permissions this script grants, leaving the identity itself alone.

.PARAMETER List
    Report what the identity holds today and change nothing.

.PARAMETER DryRun
    Report what would change and write nothing.

.EXAMPLE
    ./infra/scripts/Set-EntraGraphPermissions.ps1 -Env dev -DryRun
    ./infra/scripts/Set-EntraGraphPermissions.ps1 -Env dev

    Then restart the API revision. The identity's Graph token is cached until it expires, so without a restart
    every write fails with 403 for up to an hour after the grant lands.
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [switch]$SkipRoleAssignment,
    [switch]$Remove,
    [switch]$List,
    [switch]$Help,
    [switch]$DryRun
)

if ($Help) { Get-Help $PSCommandPath -Detailed; return }

. (Join-Path $PSScriptRoot 'common.ps1')

$Config = Initialize-RagOsScript -Env $Env -Title 'Graph permissions for directory administration'

$principalId = Get-Output -Config $Config -Name 'identityPrincipalId' -ProducedBy '02-identity-keyvault.ps1'

# Which permissions this run is about. The catalogue lives in common.ps1 so the docs can be pinned against it.
$wanted = @($script:RagOsGraphPermissions | Where-Object {
        $_ -and -not ($SkipRoleAssignment -and $_.Value -eq 'AppRoleAssignment.ReadWrite.All')
    })

Write-Step 'Resolving Microsoft Graph'
# Graph's own service principal holds its app roles (permissions). The appId assertion inside
# Get-EntraServicePrincipal holds for it like any other app, so no special case is needed - but -NoCreate is not
# optional: `az ad sp create` must never run against Microsoft Graph.
$graphSp = Get-EntraServicePrincipal -ClientId $script:RagOsGraphAppId -NoCreate
if (-not $graphSp) {
    throw ("Microsoft Graph has no service principal in tenant $($Config.EntraTenantId), which should be " +
        'impossible. Check that you are signed in to the right tenant: az account show.')
}
$graphSpId = [string](Get-Value $graphSp 'id')
Write-Info "graph service principal  $graphSpId"
Write-Info "grantee (managed identity) $principalId"

# Read from the IDENTITY's side. Asking Graph's service principal who holds its roles returns every app
# permission consented anywhere in the tenant - thousands of rows, paged - and the "is it already assigned?"
# check would be answered from page one and post a duplicate on every run.
$held = @(Get-EntraIdentityRoleAssignment -PrincipalId $principalId)
$graphHeld = @($held | Where-Object { [string](Get-Value $_ 'resourceId') -eq $graphSpId })

$valueById = @{}
foreach ($role in @(Get-Value $graphSp 'appRoles')) {
    if ($role) { $valueById[[string](Get-Value $role 'id')] = [string](Get-Value $role 'value') }
}

if ($List -or (-not $Remove -and -not $wanted)) {
    Write-Step 'Graph permissions held by this identity'
    if (-not $graphHeld) { Write-Info 'None.' }
    foreach ($a in $graphHeld) {
        $id = [string](Get-Value $a 'appRoleId')
        $name = $valueById.ContainsKey($id) ? $valueById[$id] : "(unknown role $id)"
        Write-Info ("  {0,-34} {1}" -f $name, [string](Get-Value $a 'id'))
    }
    return
}

if ($Remove) {
    Write-Step 'Revoking'
    foreach ($perm in $wanted) {
        $roleId = Get-EntraAppRoleId -Application $graphSp -Value $perm.Value
        $match = @($graphHeld | Where-Object { [string](Get-Value $_ 'appRoleId') -eq $roleId })
        if (-not $match) { Write-Ok "not held: $($perm.Value)"; continue }
        foreach ($a in $match) {
            if ($DryRun) { Write-Host "    would revoke $($perm.Value)" -ForegroundColor Yellow; continue }
            # Revoked from the identity's own appRoleAssignments collection, which is where it was read from.
            $null = Invoke-AzRest -Method delete -Url ("https://graph.microsoft.com/v1.0/servicePrincipals/" +
                "$principalId/appRoleAssignments/$([string](Get-Value $a 'id'))")
            Write-Ok "revoked: $($perm.Value)"
        }
    }
    if ($DryRun) { Write-Info 'Nothing was written. Re-run without -DryRun to apply.' }
    else { Write-Info 'Restart the API revision so it stops presenting a token that still carries them.' }
    return
}

Write-Step 'Granting'
$granted = 0
$failed = @()
foreach ($perm in $wanted) {
    $roleId = Get-EntraAppRoleId -Application $graphSp -Value $perm.Value
    if (-not $roleId) {
        $failed += "$($perm.Value): Microsoft Graph does not expose a permission by that name"
        Write-Fail "unknown Graph permission: $($perm.Value)"
        continue
    }
    if (@($graphHeld | Where-Object { [string](Get-Value $_ 'appRoleId') -eq $roleId })) {
        Write-Ok "already granted: $($perm.Value)"
        continue
    }
    Write-Info "  $($perm.Value) - $($perm.Why)"
    if ($DryRun) { Write-Host "    would grant $($perm.Value)" -ForegroundColor Yellow; continue }
    try {
        # resourceId is Graph's service principal (the resource exposing the permission); principalId is the
        # identity receiving it. Posted to the RESOURCE's appRoleAssignedTo, which is where a consent lives.
        $null = Invoke-AzRest -Method post `
            -Url "https://graph.microsoft.com/v1.0/servicePrincipals/$graphSpId/appRoleAssignedTo" `
            -Body @{ principalId = $principalId; resourceId = $graphSpId; appRoleId = $roleId }
        Write-Ok "granted: $($perm.Value)"
        $granted++
    }
    catch {
        $message = $_.Exception.Message
        $failed += "$($perm.Value): $message"
        if ($message -match '(?i)(Authorization_RequestDenied|Insufficient privileges|Forbidden|403)') {
            Write-Fail "refused: $($perm.Value)"
            # Precise on purpose: the obvious guesses all fail, and each fails for a different reason.
            Write-Info '  These are Microsoft Graph APP ROLES (application permissions), and consenting to those'
            Write-Info '  needs PRIVILEGED ROLE ADMINISTRATOR (least privilege) or GLOBAL ADMINISTRATOR.'
            Write-Info '    - Application Administrator / Cloud Application Administrator are NOT enough: they can'
            Write-Info '      consent to any API except Microsoft Graph application permissions, which is these.'
            Write-Info '    - Subscription Owner is Azure RBAC, not a directory role, and grants nothing here.'
            Write-Info '    - If the role came through PIM, ACTIVATE it - eligible-but-inactive fails identically.'
            Write-Info '  What is active right now:'
            Write-Info '    az rest --method get --url "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.directoryRole?`$select=displayName"'
        }
        else { Write-Fail "$($perm.Value): $message" }
    }
}

if ($DryRun) {
    Write-Info ''
    Write-Info 'Nothing was written. Re-run without -DryRun to apply.'
    return
}

Write-Step 'Next'
if ($failed) {
    Write-Warn "$($failed.Count) permission(s) were not granted:"
    foreach ($f in $failed) { Write-Info "  $f" }
    # NOT a portal job, though it looks like one: there is no UI anywhere in the portal for granting a Graph
    # APPLICATION permission to a managed identity. Sending someone to Enterprise applications -> Permissions
    # here wastes their afternoon - that blade is read-only for this.
    Write-Info '  The portal CANNOT finish this. No UI exists for granting a Graph application permission to a'
    Write-Info '  managed identity, so this script (or Graph directly) is the only route. Have somebody holding'
    Write-Info '  Privileged Role Administrator re-run it.'
    Write-Info '  To check what is held at any time, without granting anything:'
    Write-Info "    ./infra/scripts/Set-EntraGraphPermissions.ps1 -Env $Env -List"
    Write-Info '  The portal shows the same list read-only, under Entra ID -> Enterprise applications (set the'
    Write-Info '  Application type filter to "Managed Identities", which the default hides) -> Permissions.'
}
if ($granted -gt 0) {
    # The step people skip, and then spend an afternoon on: the identity's Graph token is cached until it
    # expires, so the running container keeps presenting one minted before the grant and every write 403s.
    Write-Warn 'RESTART THE API BEFORE TRYING THE PAGE. The identity caches its Graph token until it expires,'
    Write-Info '  so a container started before this grant keeps presenting a token without these permissions and'
    Write-Info '  every directory write fails with 403 for up to an hour. Re-running step 07 restarts it:'
    Write-Info "    ./infra/scripts/07-container-apps.ps1 -Env $Env -Only rag-api"
}
Write-Info ''
Write-Info "Set these in infra/env/$Env.psd1, then re-run 07-container-apps.ps1:"
Write-Info "  ExtraAppSettings = @{ DIRECTORY = 'graph' }"
Write-Info '  DevAuthEnabled   = $false      - mandatory: dev tokens are self-asserted and trusted for roles,'
Write-Info '                                   and the API refuses to start the directory adapter while it is on'
Write-Info 'ENTRA_SERVICE_PRINCIPAL_OBJECT_ID is taken from the outputs file that Set-EntraAppRegistration.ps1 writes.'
