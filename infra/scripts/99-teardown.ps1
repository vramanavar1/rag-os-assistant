#Requires -Version 7.3
<#
.SYNOPSIS
    Deletes the whole environment: resource group, then purges the soft-deleted Key Vault and Foundry account.
.DESCRIPTION
    Deleting the resource group destroys EVERYTHING: PostgreSQL (and its data), the search index, blobs, images and logs.
    Key Vault and the Foundry account are then soft-deleted and keep their names reserved, so they are purged too.
    Purge protection (enabled on the vault by 02) blocks the vault purge: the name stays reserved for
    KeyVaultRetentionDays, and a later 02 run recovers that vault automatically.
.EXAMPLE
    ./infra/scripts/99-teardown.ps1 -Env dev
    ./infra/scripts/99-teardown.ps1 -Env dev -Force -KeepOutputs
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [switch]$Force,
    [switch]$KeepOutputs
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '99 TEARDOWN'
$n = $Config.Names
$rg = $n.ResourceGroup
$loc = $Config.Location

Write-Host ''
Write-Host "This DELETES the resource group '$rg' in subscription $($Config.SubscriptionId) and everything in it:" -ForegroundColor Red
Write-Host "  PostgreSQL $($n.Postgres) (all ingestion state), Search $($n.Search) (all indexes), Storage $($n.Storage) (raw documents)," -ForegroundColor Red
Write-Host "  Service Bus, Container Apps, ACR images, Key Vault $($n.KeyVault), Foundry $($n.Foundry), logs," -ForegroundColor Red
Write-Host "  and the managed identity $($n.Identity) with all eight of its role assignments." -ForegroundColor Red
if ($null -ne $Config.NameOverrides -and $Config.NameOverrides.ContainsKey('Identity')) {
    # A named identity is usually someone else's. Deleting it can break deployments outside this one.
    Write-Warn "$($n.Identity) was named explicitly via NameOverrides.Identity - if it is shared with another"
    Write-Info 'environment or owned by a platform team, deleting this resource group destroys it for them too.'
}
if (-not $Force) {
    $answer = Read-Host "Type the resource group name '$rg' to confirm"
    if ($answer -ne $rg) { Write-Info 'Cancelled.'; return }
}

Write-Step 'Deleting the budget'
try {
    $null = Invoke-AzRest -Method delete -Url "https://management.azure.com$($Config.ResourceGroupId)/providers/Microsoft.Consumption/budgets/$($n.Budget)?api-version=2023-11-01" -AllowNotFound
    Write-Ok "budget $($n.Budget)"
}
catch { Write-Warn "Budget not deleted: $($_.Exception.Message.Split("`n")[0])" }

Write-Step "Deleting resource group $rg (this can take 20+ minutes)"
if (Test-AzResource @('group', 'show', '-n', $rg, '--query', 'id', '-o', 'tsv')) {
    $null = Invoke-Az @('group', 'delete', '-n', $rg, '--yes', '-o', 'none')
    Write-Ok "resource group $rg deleted"
}
else { Write-Ok "resource group $rg (already gone)" }

Write-Step "Purging Key Vault $($n.KeyVault)"
# list-deleted exits 0 with an empty result when there is nothing to purge; show-deleted exits 1 with prose
# ("No deleted Vault or HSM was found with name X") that the not-found pattern cannot read. Same fix as 02.
if (Invoke-Az @('keyvault', 'list-deleted', '--query', "[?name=='$($n.KeyVault)'] | [0].id", '-o', 'tsv')) {
    try {
        $null = Invoke-Az @('keyvault', 'purge', '--name', $n.KeyVault, '--location', $loc, '-o', 'none')
        Write-Ok "vault $($n.KeyVault) purged"
    }
    catch {
        Write-Warn "Vault $($n.KeyVault) could NOT be purged (purge protection). It stays soft-deleted for $($Config.KeyVaultRetentionDays) days; 02-identity-keyvault.ps1 recovers it on the next deployment."
    }
}
else { Write-Ok "vault $($n.KeyVault) (nothing to purge)" }

Write-Step "Purging Foundry account $($n.Foundry)"
try {
    $null = Invoke-Az @('cognitiveservices', 'account', 'purge', '-g', $rg, '-n', $n.Foundry, '-l', $loc, '-o', 'none') -AllowNotFound
    Write-Ok "account $($n.Foundry) purged (or nothing to purge)"
}
catch { Write-Warn "Foundry account not purged: $($_.Exception.Message.Split("`n")[0])" }

if (-not $KeepOutputs -and (Test-Path -LiteralPath $Config.OutputsPath)) {
    $backup = "$($Config.OutputsPath).bak"
    Move-Item -LiteralPath $Config.OutputsPath -Destination $backup -Force
    Write-Ok "outputs moved to $(Split-Path -Leaf $backup)"
}
Write-Step 'Teardown finished'
Write-Info 'Log Analytics and Application Insights stay soft-deleted for 14 days; re-creating them with the same name recovers them.'
Write-Info "Re-deploy with: ./infra/scripts/provision-all.ps1 -Env $($Config.EnvName)"
