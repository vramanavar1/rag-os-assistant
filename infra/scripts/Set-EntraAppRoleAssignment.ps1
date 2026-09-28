#Requires -Version 7.3

<#
.SYNOPSIS
    Grant, list and revoke RAG-OS application roles for a person or a group.
.DESCRIPTION
    Who may upload, administer, edit the taxonomy or work the review queue is decided by Entra application
    roles. This script manages them for the registration named by EntraClientId in the psd1.

    TWO OBJECTS, AND CONFUSING THEM COSTS HOURS:

      App registration      -> defines the roles          -> -ListRoles
      Enterprise application -> records who holds them    -> -List

    Both are named RAG-OS in the portal, but a role assignment appears only under the second. Looking under
    'App registrations -> App roles' for a person's name will never find one, however long you look.

    Run with no arguments to see both. Nothing is written unless -Role and -To are given.

    Needs a DIRECTORY role - Application Administrator or Cloud Application Administrator - which a subscription
    Owner does not have. Assigning to a GROUP additionally needs an Entra ID P1 or P2 SKU on the tenant.
.PARAMETER Env
    Which infra/env/<Env>.psd1 to read. The app registration is the one named by its EntraClientId.
.PARAMETER Role
    One or more roles to grant or revoke. The 'rag.' prefix is optional, so -Role admin and -Role rag.admin are
    the same thing. Anything the access policy does not map is refused before any call is made.
.PARAMETER To
    Who to grant to or revoke from: a sign-in name (someone@example.com), an object id, a group display name,
    or 'me' for the signed-in account.
.PARAMETER Remove
    Revoke the named roles from -To instead of granting them.
.PARAMETER List
    Show who holds which role (the enterprise application's assignments).
.PARAMETER ListRoles
    Show which roles exist on the app registration, and flag any that are missing or disabled.
.PARAMETER Help
    Print this help - every parameter, its purpose and the examples below - and exit.
.PARAMETER DryRun
    Report what would change and write nothing.
.EXAMPLE
    ./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev
    Both listings: which roles exist, and who holds them. Writes nothing.
.EXAMPLE
    ./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev -ListRoles
    Are the four roles actually on the registration? Names any that are missing.
.EXAMPLE
    ./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev -Role contributor -To priya@contoso.com
    Let Priya upload documents. She must sign out and back in before her token carries it.
.EXAMPLE
    ./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev -Role rag.admin -To 'RAG-OS Admins'
    Grant to a group (needs Entra ID P1 on the tenant).
.EXAMPLE
    ./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev -Role admin -To priya@contoso.com -Remove
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [string[]]$Role,
    [string]$To,
    [switch]$Remove,
    [switch]$List,
    [switch]$ListRoles,
    [switch]$Help,
    # Report what would change, write nothing.
    [switch]$DryRun
)
if ($Help) { Get-Help $PSCommandPath -Detailed; return }
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title 'Entra application roles'

# ------------------------------------------------------------------------------------------- preconditions
# Checked before anything is read or written, because each has a different remedy and Graph's own errors for
# them are about ids and types rather than about what the operator should do next.
if (-not $Config.EntraClientId) {
    throw ("EntraClientId is empty in infra/env/$Env.psd1, so there is no app registration to assign roles on. " +
        'Set the four Entra* values first - see Deployment.md section 9.')
}
$granting = [bool]($Role -or $To)
if ($granting -and -not ($Role -and $To)) { throw 'Pass -Role and -To together, or neither.' }
if ($Remove -and -not $granting) { throw '-Remove needs -Role and -To: it revokes a specific role from a specific principal.' }
# Read-only by default: asking for nothing gets both listings rather than a surprise write. An EXPLICIT -List
# or -ListRoles is taken at its word, so asking one question does not answer the other as well.
if (-not $granting -and -not $List -and -not $ListRoles) { $List = $true; $ListRoles = $true }

# Split on commas as well as taking the array: `-Role a,b` binds as a real array from a PowerShell
# prompt, but arrives as the single string 'a,b' through `pwsh -File`, and both are ways people run this.
$wanted = @(@($Role) | Where-Object { $_ } | ForEach-Object { $_ -split ',' } |
        Where-Object { $_.Trim() } | ForEach-Object { Resolve-EntraRoleValue -Name $_.Trim() })

Write-Step 'Reading the app registration'
$app = Invoke-Az @('ad', 'app', 'show', '--id', $Config.EntraClientId) -AllowNotFound
if (-not $app) {
    throw ("No app registration with app id '$($Config.EntraClientId)' in tenant $($Config.EntraTenantId). " +
        "Check EntraClientId in infra/env/$Env.psd1, or create it - see Deployment.md section 9.1.")
}
Write-Info "$(Get-Value $app 'displayName')  (app id $($Config.EntraClientId))"

# The enterprise application. Created only when something is about to be assigned to it - a read-only run on a
# deployment that has never granted anything should not have the side-effect of creating a directory object.
$sp = Get-EntraServicePrincipal -ClientId $Config.EntraClientId -NoCreate:(-not $granting -or $DryRun)
$spId = $sp ? [string](Get-Value $sp 'id') : $null

# ------------------------------------------------------------------------------------------- which roles exist
if ($ListRoles) {
    Write-Step 'Roles defined on the app registration'
    Write-Info '(App registrations -> RAG-OS -> App roles)'
    $assignments = $spId ? @(Get-EntraRoleAssignment -ServicePrincipalId $spId -Application $app) : @()
    $defined = @(@(Get-Value $app 'appRoles') | Where-Object { $_ })
    if ($defined.Count -eq 0) {
        Write-Fail 'None. Nobody can upload or open the admin console until these exist.'
        Write-Info "  Create them: ./infra/scripts/Set-EntraAppRegistration.ps1 -Env $Env"
    }
    # NOT $role: PowerShell variable names are case-insensitive, so that is the -Role PARAMETER, typed
    # [string[]] - assigning a hashtable to it silently coerces to a string array and blanks every column.
    foreach ($definition in $defined) {
        $value = [string](Get-Value $definition 'value')
        $enabled = [bool](Get-Value $definition 'isEnabled')
        # "no service principal" and "nobody holds it" are different facts; reporting the second for the first
        # would be a confident lie about a number this run never looked up.
        $held = $spId ? "$(@($assignments | Where-Object { $_.Role -eq $value }).Count) holder(s)" : 'holders unknown'
        Write-Host ("    [{0}] {1,-18} {2,-34} {3}" -f ($enabled ? 'on ' : 'OFF'), $value,
            [string](Get-Value $definition 'displayName'), $held) -ForegroundColor ($enabled ? 'Green' : 'Red')
    }
    $missing = @(@($script:RagOsEntraAppRoles | ForEach-Object { $_.Value }) |
            Where-Object { $v = $_; -not (@($defined | Where-Object { [string](Get-Value $_ 'value') -eq $v })) })
    if ($missing.Count -gt 0) {
        Write-Warn "Not defined: $($missing -join ', ') - nobody can hold these."
        Write-Info "  Add them: ./infra/scripts/Set-EntraAppRegistration.ps1 -Env $Env"
    }
    $disabled = @($defined | Where-Object { -not (Get-Value $_ 'isEnabled') } |
            ForEach-Object { [string](Get-Value $_ 'value') })
    if ($disabled.Count -gt 0) {
        Write-Warn "Disabled: $($disabled -join ', ') - present but grant nothing until re-enabled."
    }
}

# ------------------------------------------------------------------------------------------- who holds what
if ($List) {
    Write-Step 'Who holds which role'
    Write-Info '(Enterprise applications -> RAG-OS -> Users and groups)'
    if (-not $spId) {
        Write-Info 'No enterprise application exists yet, so nothing has ever been assigned.'
    }
    else {
        $current = @(Get-EntraRoleAssignment -ServicePrincipalId $spId -Application $app)
        if ($current.Count -eq 0) { Write-Info 'Nobody holds any role on this application.' }
        foreach ($a in ($current | Sort-Object Role, Principal)) {
            Write-Host ("    {0,-18} {1,-34} {2}" -f $a.Role, $a.Principal, $a.PrincipalType)
        }
    }
}

if (-not $granting) { return }

# ------------------------------------------------------------------------------------------- grant or revoke
$principal = Resolve-EntraPrincipal -Reference $To
Write-Step "$($Remove ? 'Revoking from' : 'Granting to') $($principal.Display) [$($principal.Type)]"

# Checked here rather than left to Graph: it rejects an unknown permission id with a message about ids, which
# says nothing about the roles not having been created yet.
$roleIds = @{}
foreach ($value in $wanted) {
    $id = Get-EntraAppRoleId -Application $app -Value $value -RequireEnabled
    if (-not $id) {
        throw ("Role '$value' is not defined and enabled on this registration, so it cannot be assigned. " +
            "Create the roles first: ./infra/scripts/Set-EntraAppRegistration.ps1 -Env $Env")
    }
    $roleIds[$value] = $id
}

# Only reachable under -DryRun, which deliberately does not create the enterprise application: a dry run that
# created a directory object would not be dry. Report and stop rather than passing $null into a mandatory
# parameter, which fails with a binder error that says nothing about what is actually missing.
if (-not $spId) {
    Write-Host '    would create the enterprise application first (none exists yet)' -ForegroundColor Yellow
    foreach ($value in $wanted) { Write-Host "    would assign $value -> $($principal.Display)" -ForegroundColor Yellow }
    Write-Info 'Nothing was written. Re-run without -DryRun to apply.'
    return
}
$existing = @(Get-EntraRoleAssignment -ServicePrincipalId $spId -Application $app)
$changed = 0
try {
    foreach ($value in $wanted) {
        $label = "$value -> $($principal.Display)"
        if ($Remove) {
            $match = @($existing | Where-Object { $_.PrincipalId -eq $principal.Id -and $_.RoleId -eq $roleIds[$value] })
            if ($match.Count -eq 0) { Write-Ok "not assigned, nothing to revoke: $label"; continue }
            foreach ($a in $match) {
                if (Revoke-EntraRoleAssignment -ServicePrincipalId $spId -AssignmentId $a.Id -Label $label -DryRun:$DryRun) { $changed++ }
            }
        }
        elseif (Grant-EntraRoleAssignment -ServicePrincipalId $spId -PrincipalId $principal.Id `
                -RoleId $roleIds[$value] -Label $label -Existing $existing -DryRun:$DryRun) { $changed++ }
    }
}
catch {
    $message = $_.Exception.Message
    if ($message -match '(?i)(Authorization_RequestDenied|Insufficient privileges|Forbidden|403)') {
        throw ("Not allowed to change role assignments on this application. This needs a DIRECTORY role - " +
            'Application Administrator or Cloud Application Administrator - which a subscription Owner does ' +
            "not have. Graph said: $message")
    }
    if ($principal.Type -eq 'Group') {
        # Deliberately hedged: I could not establish the exact error a tenant without P1 returns, and a
        # confident wrong diagnosis would send the reader to buy a licence they may already have.
        throw ("Could not assign to the group '$($principal.Display)'. Assigning an application role to a group " +
            'needs an Entra ID P1 or P2 SKU on the tenant; if yours is on the free tier, assign the users ' +
            "individually instead. Graph said: $message")
    }
    throw
}

if ($DryRun) {
    Write-Info 'Nothing was written. Re-run without -DryRun to apply.'
    return
}
if ($changed -gt 0 -and -not $Remove) {
    Write-Step 'Next'
    Write-Info 'A role is never added to a token that has already been issued, so that person must sign out and'
    Write-Info 'back in. GET /api/me then shows the roles their token actually carries.'
}
