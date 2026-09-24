#Requires -Version 7.3
<#
.SYNOPSIS
    Step 10 - load test: query latency with and without ingestion running (scripts/loadtest.py).
.DESCRIPTION
    Runs `uv run python scripts/loadtest.py --base-url https://<chat-ui> <ExtraArgs>`.
    The pass criterion (Deployment.md section 10 - Load test) is p95 during ingestion <= 1.25 x the baseline p95.
    Run `uv run python scripts/loadtest.py --help` for the generator's own options (corpus size, query rate, duration).
    When dev auth is off, an Entra access token is acquired with `az account get-access-token` (never printed).
    Backfill playbook: raise search replicas first (Scale-SearchReplicas.ps1) and use Set-IngestionControls.ps1 to
    throttle or pause ingestion while measuring.
.EXAMPLE
    ./infra/scripts/10-loadtest.ps1 -Env dev
    ./infra/scripts/10-loadtest.ps1 -Env dev -ExtraArgs '--docs','10000','--qps','5','--duration','300'
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [string[]]$ExtraArgs = @()
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '10 load test'
$rg = $Config.Names.ResourceGroup
$keyVault = Get-Output -Config $Config -Name 'keyVaultName' -ProducedBy '02-identity-keyvault.ps1'
$searchName = Get-Output -Config $Config -Name 'searchName' -ProducedBy '04-search.ps1'
$baseUrl = Get-ChatUiUrl -Config $Config

$loadtest = Join-Path $Config.RepoRoot 'scripts/loadtest.py'
if (-not (Test-Path -LiteralPath $loadtest)) { throw "scripts/loadtest.py not found at $loadtest" }

Write-Step 'Capacity before the run'
$replicas = Invoke-Az @('search', 'service', 'show', '-g', $rg, '-n', $searchName, '--query', '{replicas:replicaCount, partitions:partitionCount}')
Write-Info "AI Search: $(Get-Value $replicas 'replicas') replicas x $(Get-Value $replicas 'partitions') partitions"
Write-Info "Workers: max $($Config.WorkerMaxReplicas) x INGEST_MAX_CONCURRENCY $($Config.IngestMaxConcurrency); embed-ingest max $($Config.GpuMaxReplicas)"
Write-Info "Raise capacity for large backfills: ./infra/scripts/Scale-SearchReplicas.ps1 -Env $($Config.EnvName) -Replicas 3"

Write-Step "scripts/loadtest.py against $baseUrl"
$tokenArgs = @()
if (-not $Config.DevAuthEnabled -and $Config.EntraApiScope -and $ExtraArgs -notcontains '--token') {
    Write-Info "Acquiring an Entra access token for $($Config.EntraApiScope)"
    $tok = (az account get-access-token --scope "$($Config.EntraApiScope)" --query accessToken -o tsv)
    if ($LASTEXITCODE -ne 0 -or -not $tok) { throw 'az account get-access-token failed: sign in with az login, or pass -ExtraArgs "--token <jwt> --admin-token <jwt>".' }
    $tokenArgs = @('--token', $tok, '--admin-token', $tok)  # the operator is expected to hold rag.admin
}
Push-Location $Config.RepoRoot
try {
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    & uv run python scripts/loadtest.py --base-url $baseUrl @tokenArgs @ExtraArgs
    $exit = $LASTEXITCODE
    Write-Info "Elapsed: $([int]$stopwatch.Elapsed.TotalMinutes) min"
}
finally {
    Remove-Variable tok, tokenArgs -ErrorAction SilentlyContinue
    Pop-Location
}
if ($exit -ne 0) { throw "Load test failed (exit $exit)." }
Write-Ok 'Load test passed.'
Write-Info 'Record the numbers (documents/minute, chunks/second per GPU, p50/p95 before and during ingestion) in README.md section 13 - Scaling and operations (the capacity model).'
