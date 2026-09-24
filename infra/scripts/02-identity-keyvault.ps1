#Requires -Version 7.3
<#
.SYNOPSIS
    Step 02 - user-assigned managed identity + Key Vault (RBAC, soft delete, purge protection) + generated secrets.
.DESCRIPTION
    Roles:   deployer -> Key Vault Secrets Officer ; managed identity -> Key Vault Secrets User.
    Secrets (created only when absent; -RotateSecrets regenerates the random ones):
      dev-jwt-signing-key            64 random bytes, base64 (dev tokens; only honoured when DEV_AUTH_ENABLED=true)
      appinsights-connection-string  from Application Insights (created in 01)
    Claude on Foundry and Azure OpenAI use Entra tokens (managed identity), so no API-key secret is created.
    Optional fallback only: az keyvault secret set --vault-name <kv> --name foundry-api-key --file <key file>
    Secret values are never printed.
.EXAMPLE
    ./infra/scripts/02-identity-keyvault.ps1 -Env dev
    ./infra/scripts/02-identity-keyvault.ps1 -Env dev -RotateSecrets   # then restart revisions (Deployment.md section 4 - Key Vault secrets)
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [switch]$RotateSecrets,
    # Applies drift that disrupts the service (a Key Vault SKU change). Drift is always reported either way.
    [switch]$ApplyChanges
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '02 identity + Key Vault'
$n = $Config.Names
$rg = $n.ResourceGroup
$loc = $Config.Location
$tags = Get-TagArgs -Config $Config
$deployer = Get-DeployerPrincipal

Write-Step "User-assigned managed identity $($n.Identity)"
# An explicit NameOverrides.Identity means "bind to this one, it already exists". The lookup below is scoped to
# THIS deployment's resource group, so an identity living anywhere else is invisible - and Ensure-AzResource
# would then create a same-named identity here carrying none of the grants the real one has. That is silent
# until something returns 403 much later, so refuse up front instead.
$identityOverridden = $null -ne $Config.NameOverrides -and $Config.NameOverrides.ContainsKey('Identity')
if ($identityOverridden -and -not (Test-AzResource @('identity', 'show', '-g', $rg, '-n', $n.Identity, '--query', 'id', '-o', 'tsv'))) {
    throw ("NameOverrides.Identity names '$($n.Identity)', but no managed identity by that name exists in $rg. " +
        'RAG-OS can only use an identity in its own resource group: the lookup, all eight role assignments and ' +
        "teardown are scoped there. Either create it in $rg, or drop the override and let this step create " +
        'id-<Prefix>-<Env>.')
}
$mi = Ensure-AzResource -Description "identity $($n.Identity)" -Config $Config -SyncTags `
    -Show @('identity', 'show', '-g', $rg, '-n', $n.Identity) `
    -Create (@('identity', 'create', '-g', $rg, '-n', $n.Identity, '-l', $loc) + $tags)

Write-Step "Key Vault $($n.KeyVault)"
$kv = Get-AzResourceOrNull @('keyvault', 'show', '-g', $rg, '-n', $n.KeyVault)
if (-not $kv) {
    # list-deleted, not show-deleted: 'show-deleted' answers "No deleted Vault or HSM was found with name X" and
    # exits 1, which is an az sentence no not-found pattern reads correctly, so the ordinary "nothing to recover"
    # case became fatal on a fresh subscription. The query form returns an empty string at exit 0 instead. A 403
    # still throws, as it should - not being allowed to look is not the same as it not being there.
    $deleted = Invoke-Az @('keyvault', 'list-deleted', '--query', "[?name=='$($n.KeyVault)'] | [0].id", '-o', 'tsv')
    if ($deleted) {
        # Purge protection keeps a deleted vault for KeyVaultRetentionDays; recover it instead of failing on the name.
        Write-Info "Recovering soft-deleted vault $($n.KeyVault) ..."
        $null = Invoke-Az @('keyvault', 'recover', '--name', $n.KeyVault, '-o', 'none')
    }
    else {
        Write-Info "Creating Key Vault $($n.KeyVault) ..."
        $null = Invoke-Az (@('keyvault', 'create', '-g', $rg, '-n', $n.KeyVault, '-l', $loc, '--sku', $Config.KeyVaultSku,
                '--enable-rbac-authorization', 'true', '--enable-purge-protection', 'true',
                '--retention-days', [string]$Config.KeyVaultRetentionDays, '--public-network-access', 'Enabled') + $tags)
    }
    $kv = Invoke-Az @('keyvault', 'show', '-g', $rg, '-n', $n.KeyVault)
    Write-Ok "Key Vault $($n.KeyVault) ($($deleted ? 'recovered' : 'created'))"
    if ($deleted) {
        Write-Warn 'A recovered vault keeps the SKU, retention and network settings it had when it was deleted.'
        Write-Info 'They are reconciled against the psd1 below; its old secrets are also back.'
    }
}
else {
    $kvDesired = @(
        (New-DesiredProperty -Path 'properties.publicNetworkAccess' -Desired 'Enabled' -Arg '--public-network-access' -Label 'public network access'),
        # A SKU change is billed per operation and premium keys are HSM-backed, so it waits to be asked for.
        # 'az keyvault update' has no --sku; the vault SKU is only reachable through the generic --set.
        (New-DesiredProperty -Path 'properties.sku.name' -Desired $Config.KeyVaultSku -Label 'sku' -Class 'gated' `
                -Arg '--set' -ArgTemplate 'properties.sku.name={0}')
    )
    # Retention can be raised but never lowered once purge protection is on, so the two directions are different
    # properties as far as the operator is concerned.
    $currentRetention = [int](Get-Value $kv 'properties.softDeleteRetentionInDays')
    if ($currentRetention -gt [int]$Config.KeyVaultRetentionDays) {
        $kvDesired += (New-DesiredProperty -Path 'properties.softDeleteRetentionInDays' -Desired $Config.KeyVaultRetentionDays -Label 'retention days' -Class 'immutable' `
                -Remediation "Purge protection prevents lowering retention. Keep KeyVaultRetentionDays at $currentRetention, or use a new vault name.")
    }
    else {
        $kvDesired += (New-DesiredProperty -Path 'properties.softDeleteRetentionInDays' -Desired $Config.KeyVaultRetentionDays -Arg '--retention-days' -Label 'retention days')
    }
    $null = Sync-AzTags -Description "Key Vault $($n.KeyVault)" -Config $Config -Resource $kv
    $null = Sync-AzResource -Description "Key Vault $($n.KeyVault)" -Resource $kv -Desired $kvDesired -ApplyChanges:$ApplyChanges `
        -Update @('keyvault', 'update', '-g', $rg, '-n', $n.KeyVault)
    $kv = Invoke-Az @('keyvault', 'show', '-g', $rg, '-n', $n.KeyVault)
}
if (-not (Get-Value $kv 'properties.enableRbacAuthorization')) { throw "Key Vault $($n.KeyVault) is not in RBAC mode. Enable it: az keyvault update -n $($n.KeyVault) --enable-rbac-authorization true" }
# Purge protection was only ever printed. On a vault this script did not create it can be off, which silently
# removes the recovery path the create branch above depends on - so say so rather than tucking it into an [ok].
if (-not (Get-Value $kv 'properties.enablePurgeProtection')) {
    Write-Warn "Key Vault $($n.KeyVault) does NOT have purge protection. A deleted vault could be purged and its secrets lost permanently."
    Write-Info "  Enable it (one-way): az keyvault update -n $($n.KeyVault) -g $rg --enable-purge-protection true"
}
Write-Info "Key Vault $($n.KeyVault): RBAC mode, purge protection=$([bool](Get-Value $kv 'properties.enablePurgeProtection')), sku=$(Get-Value $kv 'properties.sku.name')"

Write-Step 'Key Vault roles'
Grant-Role -PrincipalId $deployer.ObjectId -PrincipalType $deployer.PrincipalType -Role 'Key Vault Secrets Officer' -Scope $kv.id -PrincipalLabel "deployer $($deployer.Name)"
Grant-Role -PrincipalId $mi.principalId -PrincipalType 'ServicePrincipal' -Role 'Key Vault Secrets User' -Scope $kv.id -PrincipalLabel $n.Identity

Write-Step 'Secrets'
# Listing names only (never values). Retries cover the minutes it takes a fresh role assignment to apply.
$existingNames = Invoke-WithRetry -Activity 'List Key Vault secrets' -MaxAttempts 12 -DelaySeconds 10 `
    -RetryOn '(?i)(Forbidden|not authorized|Unauthorized|ForbiddenByRbac)' -ScriptBlock {
    @(Get-AzTsvValues @('keyvault', 'secret', 'list', '--vault-name', $n.KeyVault, '--query', '[].name'))
}
$existingNames = @($existingNames)

$random = @('dev-jwt-signing-key')
foreach ($name in $random) {
    if ($existingNames -contains $name -and -not $RotateSecrets) { Write-Ok "$name (exists)"; continue }
    Set-KeyVaultSecretValue -VaultName $n.KeyVault -Name $name -Value (New-RandomSecret -Bytes 64)
    Write-Ok "$name ($(($existingNames -contains $name) ? 'rotated - new version' : 'created'))"
}
if ($RotateSecrets) {
}

$appiName = Get-Output -Config $Config -Name 'appInsightsName' -ProducedBy '01-foundation.ps1'
if ($existingNames -contains 'appinsights-connection-string' -and -not $RotateSecrets) {
    Write-Ok 'appinsights-connection-string (exists)'
}
else {
    $conn = Invoke-Az @('monitor', 'app-insights', 'component', 'show', '--app', $appiName, '-g', $rg, '--query', 'connectionString', '-o', 'tsv')
    Set-KeyVaultSecretValue -VaultName $n.KeyVault -Name 'appinsights-connection-string' -Value $conn
    Remove-Variable conn
    Write-Ok 'appinsights-connection-string (set from Application Insights)'
}

Save-Outputs -Config $Config -Values @{
    identityName        = $n.Identity
    identityId          = $mi.id
    identityClientId    = $mi.clientId
    identityPrincipalId = $mi.principalId
    keyVaultName        = $n.KeyVault
    keyVaultId          = $kv.id
    keyVaultUri         = (Get-Value $kv 'properties.vaultUri').TrimEnd('/')
    deployerObjectId    = $deployer.ObjectId
    deployerType        = $deployer.PrincipalType
    deployerName        = $deployer.Name
}
Write-Ok "Identity + Key Vault ready. Verify: az keyvault secret list --vault-name $($n.KeyVault) --query [].name -o tsv"
