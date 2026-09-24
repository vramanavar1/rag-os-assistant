#Requires -Version 7.3
<#
.SYNOPSIS
    Temporarily change the AI Search replica count (backfill playbook).
.DESCRIPTION
    More replicas = more indexing and query throughput while a large backfill runs. Scaling is billed per replica and
    takes a few minutes; scale back down afterwards (the psd1 value is the steady state).
    Limits: replicas 1-12, replicas x partitions <= 36; >= 2 replicas are needed for the read SLA, 3 for read-write.
.EXAMPLE
    ./infra/scripts/Scale-SearchReplicas.ps1 -Env dev -Replicas 3    # before a backfill
    ./infra/scripts/Scale-SearchReplicas.ps1 -Env dev -Replicas 2    # after it
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [Parameter(Mandatory)][ValidateRange(1, 12)][int]$Replicas
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title 'Scale AI Search replicas'
$rg = $Config.Names.ResourceGroup
$searchName = Get-Output -Config $Config -Name 'searchName' -ProducedBy '04-search.ps1'

$current = Invoke-Az @('search', 'service', 'show', '-g', $rg, '-n', $searchName, '--query', '{replicas:replicaCount, partitions:partitionCount, sku:sku.name, status:status}')
$partitions = [int](Get-Value $current 'partitions')
Write-Info "Current: $(Get-Value $current 'replicas') replicas x $partitions partitions ($(Get-Value $current 'sku'), status=$(Get-Value $current 'status'))"
if ($Replicas * $partitions -gt 36) { throw "replicas x partitions must be <= 36 (requested $Replicas x $partitions)." }
if ([int](Get-Value $current 'replicas') -eq $Replicas) { Write-Ok "Already at $Replicas replicas."; return }

Write-Step "Scaling $searchName to $Replicas replicas (this takes several minutes)"
$null = Invoke-Az @('search', 'service', 'update', '-g', $rg, '-n', $searchName, '--replica-count', [string]$Replicas, '-o', 'none')
$after = Invoke-Az @('search', 'service', 'show', '-g', $rg, '-n', $searchName, '--query', '{replicas:replicaCount, status:status, provisioning:provisioningState}')
Write-Ok "Now: $(Get-Value $after 'replicas') replicas (status=$(Get-Value $after 'status'), provisioning=$(Get-Value $after 'provisioning'))"
Write-Info 'Remember to scale back down when the backfill finishes - replicas are billed hourly.'
