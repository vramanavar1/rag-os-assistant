#Requires -Version 7.3

<#
.SYNOPSIS
    Configures the Entra app registration people sign in through, so the chat UI can request its API scope.
.DESCRIPTION
    This is the fix for:

        AADSTS65005: The application 'api://<app-id>' asked for scope 'access_as_user' that doesn't exist.

    Nothing is wrong in the repo when that appears. The chat UI asks for exactly the string in EntraApiScope
    (dev.psd1 -> ENTRA_API_SCOPE -> /api/public-config -> MSAL), and the string is right - but the app
    registration never exposed a scope by that name. Creating the registration was scripted; exposing the scope
    was a sentence telling you to open the portal, so it was the one step that could be skipped silently. This
    script makes it re-runnable instead.

    What it reconciles, all idempotently - a second run prints 'already correct' and sends nothing:

      1. identifierUris            contains the app id URI, or the scope resolves to no resource at all
                                   (that is AADSTS500011, a different error with a different hunt).
      2. api.oauth2PermissionScopes exposes the scope named in EntraApiScope, enabled, consentable by users.
      3. api.preAuthorizedApplications lists the app itself, so signing in never shows a consent prompt.
      4. api.requestedAccessTokenVersion is set EXPLICITLY, because its default of null means 1, and the token
                                   version decides both `iss` and `aud` - see the note below.
      5. spa.redirectUris          contains https://<chat-ui-fqdn>/auth/callback (AADSTS50011 without it).
      6. appRoles                  exposes rag.admin / rag.contributor / rag.sme / rag.reviewer, the values
                                   config/access-policy/access-policy.yaml maps to internal roles. Creating them
                                   is not enough on its own - somebody has to be ASSIGNED one, which is what
                                   -GrantAdminTo does. Until then every upload is refused with "uploading
                                   requires the contributor or admin role" and the admin console 403s.

    ON THE TOKEN VERSION. requestedAccessTokenVersion is not cosmetic, and null is not 'unset':

      version 1 (what null means)  iss = https://sts.windows.net/<tenant>/      aud = the requested api:// URI
      version 2                    iss = .../<tenant>/v2.0                      aud = the bare app id GUID

    So the directory decides which pair the API must expect. Leaving it at null while the API expects the v2.0
    issuer produces `401 untrusted issuer` for everyone - a failure that looks nothing like a configuration
    mismatch. This script sets 2 and says so; ENTRA_AUDIENCE must agree.

    Read-modify-write throughout, because a Microsoft Graph PATCH REPLACES a complex property: sending a bare
    api object would delete every other exposed scope, every other pre-authorised client,
    knownClientApplications and acceptMappedClaims. The body is built by Get-EntraAppPatch in common.ps1.

    Needs a DIRECTORY role, which Azure RBAC does not grant: Application Administrator or Cloud Application
    Administrator, or Application Developer plus ownership of this particular registration. Subscription Owner
    is not enough and the failure says so.
.PARAMETER Env
    Which infra/env/<Env>.psd1 to read. Everything below is applied to the registration it names.
.PARAMETER Help
    Print this help - every parameter, its purpose and the examples below - and exit.
.PARAMETER ChatUiFqdn
    The chat UI host, for the SPA redirect URI. Defaults to what step 07 recorded; omit it before the first
    deployment and re-run this script afterwards - redirect URIs can be added to a live registration at any time.
.PARAMETER GrantAdminTo
    Assigns the rag.admin role to a person: a UPN, a user object id, or 'me'. Creating a role grants nobody
    anything, so a first deployment needs this (or the same action in the portal) before anyone can administer.
    NOTE the new role is NOT in a token that has already been issued - sign out and back in afterwards.
.PARAMETER SkipUserAttributes
    Skip creating the three directory extensions (department, region, clearance) and registering them as
    access-token optional claims. Use it when directory schema is governed separately, or when this account
    lacks the directory role. Without them a caller signs in successfully and reads NOTHING, because department
    and region are required and never arrive - see "Entra user attributes and claims" in README.md.
.PARAMETER PreAuthorizeAzureCli
    Also pre-authorise Microsoft's Azure CLI (04b07795-8ddb-461a-bbee-02f9e1bf7b46) for the scope, which is what
    makes `az account get-access-token --scope <the scope>` work - the token that Deployment.md section 9.5 and
    09-smoke.ps1 need on a deployment with DevAuthEnabled = $false. OFF by default: it lets anyone in the tenant
    who can run az mint a token for this API. The API still applies its own attribute filtering to that caller.
.EXAMPLE
    ./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev -Help
.EXAMPLE
    ./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev -DryRun
.EXAMPLE
    ./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev
.EXAMPLE
    ./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev -PreAuthorizeAzureCli -IncludeLocalhostRedirect
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [string]$ChatUiFqdn,
    # Add http://localhost:8080/auth/callback too, for running the UI locally against this registration.
    [switch]$IncludeLocalhostRedirect,
    [switch]$PreAuthorizeAzureCli,
    # Application roles to expose. These are what the token's `roles` claim carries, and nothing else in the
    # deployment creates them - without one, nobody can upload or open the admin console.
    [string[]]$AppRoles = @('rag.admin', 'rag.contributor', 'rag.sme', 'rag.reviewer'),
    # Assign rag.admin to somebody: a UPN, a user object id, or 'me' for the signed-in account. Defaults to
    # EntraGrantAdminTo in the psd1, which is how provisioning reaches it - provision-all.ps1 invokes every step
    # as `& $script -Env $Env` and passes nothing else, so a command-line-only flag could never fire there.
    [string]$GrantAdminTo,
    # Drop the standalone closing guidance. Provisioning runs this as a step, where "re-run 07-container-apps.ps1"
    # is nonsense on the pass that happens before step 07 has run at all.
    [switch]$Brief,
    # Skip the directory extensions and optional claims, for tenants where directory schema is governed
    # separately. The rest of the registration still reconciles - but without them a caller arrives with no
    # department and no region, and since both are required, reads nothing.
    [switch]$SkipUserAttributes,
    # Print the diff and write nothing.
    [switch]$DryRun,
    # Print the help above and exit.
    [switch]$Help
)
# Before the dot-source: asking what the parameters are should not need a subscription or a psd1.
if ($Help) { Get-Help $PSCommandPath -Detailed; return }
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title 'Entra app registration'
# A param default cannot read $Config - it is not loaded when the param block is bound - so the fallback lives
# here. An explicit -GrantAdminTo still wins, which keeps the one-off `-GrantAdminTo me` working.
if (-not $GrantAdminTo) { $GrantAdminTo = [string]$Config.EntraGrantAdminTo }

# Microsoft's own first-party Azure CLI client. Fixed across every tenant, which is why it can be a constant.
$AzureCliAppId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'

# ------------------------------------------------------------------------------------------- what the psd1 asks for
Write-Step 'Configured sign-in settings'
if (-not $Config.EntraClientId -or -not $Config.EntraApiScope) {
    Write-Warn 'EntraClientId or EntraApiScope is empty - there is no registration to reconcile.'
    Write-Info "Set all four Entra* values in infra/env/$Env.psd1 first; see Deployment.md section 9."
    return
}

$scopeName = Get-EntraScopeName -Scope $Config.EntraApiScope
if (-not $scopeName) {
    throw ("EntraApiScope '$($Config.EntraApiScope)' names no scope. It must be a resource followed by a scope " +
        "name, e.g. api://$($Config.EntraClientId)/access_as_user. As written it is a resource on its own, so " +
        'there is nothing for the browser to request.')
}
$scopeResource = Get-EntraScopeResource -Scope $Config.EntraApiScope
$appIdUri = if ("$($Config.EntraAudience)".StartsWith('api://')) { "$($Config.EntraAudience)" } else { "api://$($Config.EntraClientId)" }

# Caught here rather than by Entra, because AADSTS500011 three steps later reads as a missing resource rather
# than as two psd1 values that disagree with each other.
if ($scopeResource -ne $appIdUri) {
    throw ("EntraApiScope and the app id URI disagree: the scope asks for resource '$scopeResource' but this " +
        "registration is '$appIdUri'. No token can ever satisfy both. Fix EntraApiScope to " +
        "'$appIdUri/$scopeName' in infra/env/$Env.psd1.")
}

Write-Info "app (client) id  $($Config.EntraClientId)"
Write-Info "app id URI       $appIdUri"
Write-Info "scope            $scopeName   (from EntraApiScope)"
Write-Info "ENTRA_AUDIENCE   $($Config.EntraAudience)"

# ------------------------------------------------------------------------------------------- the registration today
Write-Step 'Reading the app registration'
# One read returns the whole Graph application object - api, spa, identifierUris and signInAudience together.
$app = Invoke-Az @('ad', 'app', 'show', '--id', $Config.EntraClientId) -AllowNotFound
if (-not $app) {
    throw ("No app registration with app id '$($Config.EntraClientId)' in tenant $($Config.EntraTenantId). " +
        "Create it with: az ad app create --display-name 'RAG-OS'   then put its appId in " +
        "infra/env/$Env.psd1 (see Deployment.md section 9.1) and re-run this script.")
}
$objectId = [string](Get-Value $app 'id')
if (-not $objectId) { throw "The app registration came back without an object id; cannot PATCH it." }
Write-Info "$(Get-Value $app 'displayName')  (object id $objectId)"
Write-Info "signInAudience   $(Get-Value $app 'signInAudience')"

$redirects = @()
$fqdn = if ($ChatUiFqdn) { $ChatUiFqdn } else { [string](Get-Value (Get-Outputs -Config $Config) 'chatUiFqdn') }
if ($fqdn) { $redirects += "https://$fqdn/auth/callback" }   # the path is fixed at chat-ui/src/entra.ts:23
else {
    Write-Warn 'No chat UI FQDN known, so the SPA redirect URI is left alone.'
    Write-Info 'Expected before sign-in works. Re-run this script after step 07, or pass -ChatUiFqdn.'
}
if ($IncludeLocalhostRedirect) { $redirects += 'http://localhost:8080/auth/callback' }

$preAuth = @()
if ($PreAuthorizeAzureCli) { $preAuth += $AzureCliAppId }

# Assigning rag.admin is its own step because creating a role grants nobody anything, and because it has to run
# on BOTH paths: a registration configured last week reconciles to zero changes, and `-GrantAdminTo me` against
# it is exactly how somebody becomes an administrator. Leaving it after the write would have made the flag a
# no-op in the one case people will use it for.
function Save-ServicePrincipalId {
    <#
    .SYNOPSIS
        Records the enterprise application's OBJECT id, which the API needs to read and write app-role assignments.
    .DESCRIPTION
        Not the app (client) id, and not the app registration's object id: assignments hang off the service
        principal, and nothing recorded this until Settings (Security) needed it. Best-effort - a deployment that
        does not administer people does not need it, so a missing enterprise application is not a failure here.
    #>
    param([switch]$DryRun)
    try {
        # -NoCreate on a dry run: this must never bring an enterprise application into existence as a side effect.
        $sp = Get-EntraServicePrincipal -ClientId $Config.EntraClientId -NoCreate:$DryRun
        $spId = [string](Get-Value $sp 'id')
        if (-not $spId) { Write-Info 'No enterprise application yet, so there is no object id to record.'; return }
        Save-Outputs -Config $Config -Values @{ entraServicePrincipalObjectId = $spId }
        Write-Ok "enterprise application object id: $spId"
    }
    catch {
        Write-Warn "Could not record the enterprise application object id: $($_.Exception.Message)"
        Write-Info '  Settings (Security) needs ENTRA_SERVICE_PRINCIPAL_OBJECT_ID; everything else is unaffected.'
    }
}


function Grant-AdminRole {
    <# .SYNOPSIS  Assigns rag.admin to -GrantAdminTo. Best-effort: reports and returns, never throws. #>
    param([Parameter(Mandatory)][AllowNull()][object]$Application)
    if (-not $GrantAdminTo) { return }
    Write-Step "Granting rag.admin to $GrantAdminTo"
    try {
        # All four steps live in common.ps1 so this script and Set-EntraAppRoleAssignment.ps1 cannot drift apart
        # on what "assign a role" means - and so the service-principal and role-id lookups exist once.
        $principal = Resolve-EntraPrincipal -Reference $GrantAdminTo
        $sp = Get-EntraServicePrincipal -ClientId $Config.EntraClientId
        $spId = [string](Get-Value $sp 'id')

        $roleId = Get-EntraAppRoleId -Application $Application -Value 'rag.admin'
        if (-not $roleId) { throw 'rag.admin is not exposed on the registration, so it cannot be assigned' }

        $existing = @(Get-EntraRoleAssignment -ServicePrincipalId $spId -Application $Application)
        $null = Grant-EntraRoleAssignment -ServicePrincipalId $spId -PrincipalId $principal.Id -RoleId $roleId `
            -Label "rag.admin -> $($principal.Display)" -Existing $existing -DryRun:$DryRun
    }
    catch {
        # Best-effort on purpose: this needs AppRoleAssignment.ReadWrite.All on top of a directory role, and if it
        # is missing the roles still exist and somebody can finish in the portal. Failing the run here would
        # report nothing achieved for a registration that is in fact ready.
        Write-Warn 'Could not assign rag.admin - the ROLES THEMSELVES ARE CREATED, only the grant is missing.'
        Write-Info '  Assign it by hand: Entra admin center -> Enterprise applications -> RAG-OS -> Users and'
        Write-Info '  groups -> Add user/group -> pick the person and the RAG-OS administrator role.'
        Write-Info "  Graph said: $((($_.Exception.Message -split "`n") | Where-Object { $_.Trim() } | Select-Object -Last 1).Trim())"
    }
}

# ------------------------------------------------------------------------------- user attributes
# Separate from the patch above because these are different Graph resources - extensionProperties are created
# one at a time, and optionalClaims is a property of the app. Without them sign-in works and every caller
# arrives with no department and no region; both are required, so they read nothing, and nothing says why.
function Set-UserAttributes {
    param([Parameter(Mandatory)][object]$App)
    if ($SkipUserAttributes) {
        Write-Info 'Skipped the user attributes (-SkipUserAttributes). Callers will have no department or region.'
        return
    }
    Write-Step 'User attributes (directory extensions + optional claims)'
    try {
        $claims = Set-EntraUserAttributes -App $App -ObjectId $objectId -ClientId $Config.EntraClientId -DryRun:$DryRun
        Write-Info 'The access token will carry these, and access-policy.yaml reads them as extn.<name>:'
        foreach ($c in $claims) { Write-Info "    $c" }
        Write-Info 'Set a value on somebody before they sign in - there is no portal UI for this:'
        # Single-quoted: the body is JSON, and a double-quoted PowerShell string would need every quote in it
        # escaped with a backtick - which is exactly the sort of line that gets copied out wrong.
        Write-Info '    az rest --method patch --url https://graph.microsoft.com/v1.0/users/<upn> --body ''{"extension_<appid>_department": "HR"}'''
    }
    catch {
        $message = $_.Exception.Message
        if ($message -match '(?i)(Authorization_RequestDenied|Insufficient privileges|Forbidden|403)') {
            Write-Warn 'Not allowed to define directory extensions on this registration.'
            Write-Info '  This needs Application Administrator, or ownership of the registration.'
            Write-Info "  Re-run with -SkipUserAttributes to finish the rest, then ask an identity administrator."
            Write-Info '  See "Entra user attributes and claims" in README.md for the manual steps.'
            return
        }
        throw
    }
}

# ------------------------------------------------------------------------------------------- reconcile
Write-Step 'Reconciling'
$patch = Get-EntraAppPatch -App $app -ClientId $Config.EntraClientId -ScopeName $scopeName -AppIdUri $appIdUri `
    -RedirectUris $redirects -PreAuthorizeAppIds $preAuth -AppRoles $AppRoles -AccessTokenVersion 2

if ($patch.Changes.Count -eq 0) {
    Write-Ok "Already correct - scope '$scopeName' is exposed (id $($patch.ScopeId)) and nothing needs changing."
    Write-Info 'If sign-in still fails, the cached token is the likely cause: sign out of the chat UI, or open it'
    Write-Info 'in a private window. Entra keeps issuing a cached access token for up to an hour after a change.'
    # The roles are already there on this path, so the grant can go ahead without any write.
    Grant-AdminRole -Application $app
    # Also on this path: an app whose scope and roles are already correct can still be missing the attributes,
    # which is exactly the state a deployment made before this step existed is in.
    Set-UserAttributes -App $app
    Save-ServicePrincipalId -DryRun:$DryRun
    # Recorded here too: this path returns early, so without it a deployment whose registration was already
    # correct would leave the outputs file with no record of the object or scope id at all.
    Save-Outputs -Config $Config -Values @{
        entraAppObjectId        = $objectId
        entraScopeId            = $patch.ScopeId
        entraAccessTokenVersion = "$(Get-Value $app 'api.requestedAccessTokenVersion')"
    }
    return
}

foreach ($change in $patch.Changes) {
    # One line per side rather than a padded two-column layout: the values here are GUIDs and FQDNs, long enough
    # that any column width that fits 'What' throws the arrow off the end of the line.
    Write-Host "    $($change.What)" -ForegroundColor DarkGray
    Write-Host "        $($change.Before)  ->  $($change.After)" -ForegroundColor Yellow
}
if ($PreAuthorizeAzureCli) {
    Write-Warn "Pre-authorising the Azure CLI ($AzureCliAppId): anyone in the tenant who can run az will be able"
    Write-Info '  to obtain a token for this API. Their own attributes still decide what the API returns.'
}

if ($DryRun) {
    $writes = $patch.PreAuthDeferred ? 'two writes' : 'one write'
    Write-Host "    would patch $($patch.Changes.Count) setting(s) on $objectId in $writes" -ForegroundColor Yellow
    if ($patch.PreAuthDeferred) {
        Write-Info '  Two, because Graph will not accept a pre-authorisation for a scope that does not exist yet.'
    }
    Write-Info 'Nothing was written. Re-run without -DryRun to apply.'
    return
}

# ------------------------------------------------------------------------------------------- write
Write-Step 'Writing to Microsoft Graph'
# No -AllowNotFound here, deliberately: RagOsNotFoundPattern matches 'does not exist', so a Graph body error such
# as "Property 'x' does not exist" would be swallowed into $null and this script would report success on a write
# that never happened.
try {
    $null = Invoke-AzRest -Method patch -Url "https://graph.microsoft.com/v1.0/applications/$objectId" -Body $patch.Body
}
catch {
    $message = $_.Exception.Message
    if ($message -match '(?i)(Authorization_RequestDenied|Insufficient privileges|Forbidden|403)') {
        throw ("Not allowed to update app registration $objectId. Writing one needs a DIRECTORY role, which " +
            'Azure RBAC does not grant - subscription Owner is not enough. Ask for Application Administrator ' +
            'or Cloud Application Administrator, or to be made an owner of this registration (Entra admin ' +
            "center -> App registrations -> RAG-OS -> Owners). Graph said: $message")
    }
    if ($message -match '(?i)(identifierUri|already in use|must be unique)') {
        throw ("The app id URI '$appIdUri' was rejected - most often because another registration in this " +
            "tenant already claims it. Find it with: az ad app list --identifier-uri $appIdUri   " +
            "Graph said: $message")
    }
    if ($message -match $script:RagOsGraphPermissionIdPattern) {
        # This write does not reference the scope it creates - Get-EntraAppPatch defers that precisely so this
        # cannot happen. So the offending id belongs to a pre-authorisation that was ALREADY on the app and points
        # at a permission the app no longer defines. It is carried forward untouched here because dropping it
        # would delete every pre-authorised client, and because an id there may name an app role rather than a
        # scope, so it cannot be filtered safely either. It has to be removed by hand.
        throw ("An existing pre-authorised client on this registration references a permission id that the app " +
            'no longer exposes, so Graph rejects any write that carries it forward. Remove the stale entry in ' +
            'the Entra admin center (App registrations -> RAG-OS -> Expose an API -> Authorized client ' +
            "applications) and re-run. Current entries: $(@(@(Get-Value $app 'api.preAuthorizedApplications') |
                ForEach-Object { [string](Get-Value $_ 'appId') }) -join ', ')   Graph said: $message")
    }
    throw
}

# ------------------------------------------------------------------------------------------- confirm
Write-Step 'Confirming'
# Directory writes replicate, so a read straight back can still show the old object. This is the layer built for
# exactly that (minutes, not seconds) - see the two-retry-layer note in common.ps1.
# It is also the barrier before the second write: the application object it returns is what the pre-authorisation
# is computed from, so that write can only ever reference the scope id Graph itself reported.
$confirmed = Invoke-WithRetry -Activity "scope '$scopeName'" -MaxAttempts 10 -DelaySeconds 6 `
    -RetryOn '(?i)(not (yet )?(exposed|found)|does not exist|replicat)' -ScriptBlock {
    $fresh = Invoke-Az @('ad', 'app', 'show', '--id', $Config.EntraClientId)
    $found = @(Get-Value $fresh 'api.oauth2PermissionScopes' |
            Where-Object { $_ -and ([string](Get-Value $_ 'value')) -eq $scopeName -and (Get-Value $_ 'isEnabled') })
    if ($found.Count -eq 0) { throw "scope '$scopeName' is not exposed yet on $($Config.EntraClientId)" }
    return @{ ScopeId = [string](Get-Value $found[0] 'id'); App = $fresh
        Version = (Get-Value $fresh 'api.requestedAccessTokenVersion') }
}
Write-Ok "scope '$scopeName' exposed and enabled (id $($confirmed.ScopeId))"
Write-Ok "api.requestedAccessTokenVersion = $($confirmed.Version)"

# ------------------------------------------------------------------------------------------- second write
# Deferred by Get-EntraAppPatch because Graph validates pre-authorisations against the permissions already on the
# app. Now that the scope is one of them, the same function computes the delta - using the id read back above.
#
# Best-effort on purpose. Sign-in works from here: the scope exists and the redirect URI is registered. A missing
# pre-authorisation costs one consent prompt per user and nothing else, which is why Get-EntraAppChecks grades it
# WARN. Throwing here would report total failure for a deployment that is in fact usable, and that is exactly how
# the cosmetic half of this script came to block the half that matters.
if ($patch.PreAuthDeferred) {
    Write-Step 'Pre-authorising the client'
    $second = Get-EntraAppPatch -App $confirmed.App -ClientId $Config.EntraClientId -ScopeName $scopeName `
        -AppIdUri $appIdUri -RedirectUris $redirects -PreAuthorizeAppIds $preAuth -AppRoles $AppRoles `
        -AccessTokenVersion 2
    if ($second.Changes.Count -eq 0 -or $second.Body.Count -eq 0) {
        Write-Ok 'Already pre-authorised - nothing more to write.'
    }
    else {
        foreach ($change in $second.Changes) {
            Write-Host "    $($change.What)" -ForegroundColor DarkGray
            Write-Host "        $($change.Before)  ->  $($change.After)" -ForegroundColor Yellow
        }
        try {
            # A scope Graph has just reported is still not instantly visible to the validator that checks
            # pre-authorisations, so this waits for that rather than treating the first 400 as final.
            Invoke-WithRetry -Activity 'pre-authorisation' -MaxAttempts 5 -DelaySeconds 6 `
                -RetryOn $script:RagOsGraphPermissionIdPattern -ScriptBlock {
                $null = Invoke-AzRest -Method patch -Url "https://graph.microsoft.com/v1.0/applications/$objectId" `
                    -Body $second.Body
            }
            Write-Ok "pre-authorised for scope id $($confirmed.ScopeId) - no consent prompt"
        }
        catch {
            Write-Warn 'Could not pre-authorise the client, but SIGN-IN IS NOT BLOCKED by this.'
            Write-Info '  The scope exists, so people can sign in; each will be asked to consent once the first'
            Write-Info '  time. Re-running this script will retry only this step.'
            Write-Info "  Graph said: $((($_.Exception.Message -split "`n") | Where-Object { $_.Trim() } | Select-Object -Last 1).Trim())"
        }
    }
}

Save-Outputs -Config $Config -Values @{
    entraAppObjectId        = $objectId
    entraScopeId            = $confirmed.ScopeId
    entraAccessTokenVersion = "$($confirmed.Version)"
}

Grant-AdminRole -Application $confirmed.App
Set-UserAttributes -App $confirmed.App
Save-ServicePrincipalId -DryRun:$DryRun

if (-not $Brief) {
    Write-Step 'Next'
    # Naming the audience rule here is the point: this script has just changed which `aud` Entra will stamp, and a
    # deployment still carrying the other form 401s on every request with a token that is otherwise perfectly valid.
    $expected = "$($Config.EntraClientId)"
    if ("$($Config.EntraAudience)" -ne $expected) {
        Write-Warn "ENTRA_AUDIENCE is '$($Config.EntraAudience)', but version 2 tokens carry aud = '$expected'."
        Write-Info "  Set EntraAudience = '$expected' in infra/env/$Env.psd1 and re-run 07-container-apps.ps1,"
        Write-Info '  unless this deployment already accepts both spellings (rag-api built after the validator change).'
    }
    Write-Info 'Then: sign out of the chat UI (or use a private window) before trying again - a cached access token'
    Write-Info 'from before this change stays valid for up to an hour and will keep failing the same way.'
    if (-not $GrantAdminTo) {
        Write-Info ''
        Write-Warn 'No role is assigned to anyone, so nobody can upload or open the admin console yet.'
        Write-Info "  Name one as EntraGrantAdminTo in infra/env/$Env.psd1, or grant it now with:"
        Write-Info "  ./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env $Env -Role admin -To me"
    }
    else {
        Write-Info 'A role is never added to a token that has already been issued, so sign out and back in before'
        Write-Info 'testing the upload - GET /api/me should then show "roles": ["admin", "contributor"].'
    }
}
