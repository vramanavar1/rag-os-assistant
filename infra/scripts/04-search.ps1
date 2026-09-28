#Requires -Version 7.3

<#
.SYNOPSIS
    Step 04 - Azure AI Search (S1 by default): system-assigned identity, RBAC only (API keys disabled), semantic ranker.
.DESCRIPTION
    Roles for the managed identity and the deployer: Search Index Data Contributor + Search Service Contributor
    (the bootstrap job creates/updates indexes; workers write documents; the API queries).
    Re-runs reconcile the service against the psd1: local auth and the semantic ranker are applied, replicas and
    partitions are reported and applied only with -ApplyChanges (use Scale-SearchReplicas.ps1 for temporary
    backfill capacity), and a changed SearchSku is reported as the immutable change it is.
.EXAMPLE
    ./infra/scripts/04-search.ps1 -Env dev
    ./infra/scripts/04-search.ps1 -Env dev -ApplyChanges   # also resize replicas/partitions to the psd1
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    # -ApplyCapacity is the original name, kept so existing docs and runbooks keep working.
    [Alias('ApplyCapacity')][switch]$ApplyChanges
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '04 AI Search'
$n = $Config.Names
$rg = $n.ResourceGroup
$tags = Get-TagArgs -Config $Config
$deployer = Get-DeployerPrincipal
$miName = Get-Output -Config $Config -Name 'identityName' -ProducedBy '02-identity-keyvault.ps1'
$miPrincipalId = Get-Output -Config $Config -Name 'identityPrincipalId' -ProducedBy '02-identity-keyvault.ps1'

# The header names what the psd1 asks for, not what exists - it is printed before anything is read. Saying so
# matters because the SKU cannot be changed in place, so this line used to claim a SKU the service did not have.
Write-Step "AI Search $($n.Search) - configured: $($Config.SearchSku), $($Config.SearchReplicas) replicas x $($Config.SearchPartitions) partitions"
$search = Get-AzResourceOrNull @('search', 'service', 'show', '-g', $rg, '-n', $n.Search)
if (-not $search) {
    Write-Info 'Creating search service (S1 typically takes 5-15 minutes) ...'
    # --disable-local-auth true = RBAC only. It cannot be combined with --auth-options, so that flag is not passed.
    $null = Invoke-Az (@('search', 'service', 'create', '-g', $rg, '-n', $n.Search, '-l', $Config.Location,
            '--sku', $Config.SearchSku, '--replica-count', [string]$Config.SearchReplicas, '--partition-count', [string]$Config.SearchPartitions,
            '--identity-type', 'SystemAssigned', '--disable-local-auth', 'true', '--semantic-search', $Config.SearchSemantic) + $tags)
    $search = Invoke-Az @('search', 'service', 'show', '-g', $rg, '-n', $n.Search)
    Write-Ok "search $($n.Search) (created)"
}
else {
    Write-Info "search $($n.Search) is $(Get-Value $search 'sku.name'), $(Get-Value $search 'replicaCount') replicas x $(Get-Value $search 'partitionCount') partitions, status=$(Get-Value $search 'status')"
    $null = Sync-AzTags -Description "search $($n.Search)" -Config $Config -Resource $search
    $drift = Sync-AzResource -Description "search $($n.Search)" -Resource $search -ApplyChanges:$ApplyChanges `
        -Update @('search', 'service', 'update', '-g', $rg, '-n', $n.Search) -Desired @(
        (New-DesiredProperty -Path 'disableLocalAuth' -Desired $true -Arg '--disable-local-auth' -Label 'local auth disabled'),
        # ARM reports a disabled ranker as null rather than 'disabled', which Test-ValueMatches treats as equal to
        # an unset desired value - otherwise this fired a pointless update on every single run.
        (New-DesiredProperty -Path 'semanticSearch' -Desired (($Config.SearchSemantic -eq 'disabled') ? '' : $Config.SearchSemantic) -Arg '--semantic-search' -Label 'semantic ranker'),
        # Replicas and partitions are billed per unit and rebalancing moves index data, so they are gated. This
        # also stops a routine re-run undoing extra capacity added by Scale-SearchReplicas.ps1 mid-backfill.
        (New-DesiredProperty -Path 'replicaCount' -Desired $Config.SearchReplicas -Arg '--replica-count' -Label 'replicas' -Class 'gated'),
        (New-DesiredProperty -Path 'partitionCount' -Desired $Config.SearchPartitions -Arg '--partition-count' -Label 'partitions' -Class 'gated'),
        (New-DesiredProperty -Path 'sku.name' -Desired $Config.SearchSku -Label 'sku' -Class 'immutable' `
                -Remediation "A search service SKU is fixed for its lifetime. Create a new service (change Prefix/NameSuffix or NameOverrides.Search), re-run steps 04 and 08, and re-ingest - the index has to be rebuilt.")
    )
    if ($drift.Applied.Count -gt 0) { $search = Invoke-Az @('search', 'service', 'show', '-g', $rg, '-n', $n.Search) }
}

Write-Step 'Search roles'
foreach ($role in @('Search Index Data Contributor', 'Search Service Contributor')) {
    Grant-Role -PrincipalId $miPrincipalId -PrincipalType 'ServicePrincipal' -Role $role -Scope $search.id -PrincipalLabel $miName
    Grant-Role -PrincipalId $deployer.ObjectId -PrincipalType $deployer.PrincipalType -Role $role -Scope $search.id -PrincipalLabel "deployer $($deployer.Name)"
}

$principalId = Get-Value $search 'identity.principalId'
Save-Outputs -Config $Config -Values @{
    searchName        = $n.Search
    searchId          = $search.id
    searchEndpoint    = "https://$($n.Search).search.windows.net"
    searchPrincipalId = ($principalId ? $principalId : '')
}
Write-Ok 'Search ready.'
Write-Info "Verify: az search service show -g $rg -n $($n.Search) --query '{status:status, sku:sku.name, replicas:replicaCount, partitions:partitionCount, localAuthDisabled:disableLocalAuth, semantic:semanticSearch}'"
