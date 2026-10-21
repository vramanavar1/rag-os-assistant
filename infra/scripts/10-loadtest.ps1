#Requires -Version 7.3

<#
.SYNOPSIS
    Step 10 - load test: query latency with and without ingestion running (scripts/loadtest.py).
.DESCRIPTION
    Runs `uv run python scripts/loadtest.py run --base-url https://<chat-ui> <ExtraArgs>`.
    The pass criterion (Deployment.md section 10 - Load test) is p95 during ingestion <= 1.25 x the baseline p95.
    Run `uv run python scripts/loadtest.py run --help` for the load options (--rps, --baseline-s, --during-s).
    The synthetic corpus is generated separately: uv run python scripts/loadtest.py generate --docs 10000.
    When dev auth is off, an Entra access token is acquired with `az account get-access-token` (never printed).
    Backfill playbook: raise search replicas first (Scale-SearchReplicas.ps1) and use Set-IngestionControls.ps1 to
    throttle or pause ingestion while measuring.

    Comparing two deployments - the Dedicated vs Consumption plan switch is what this is for:
      -SaveBaseline       writes this run's percentiles to infra/env/<env>.loadtest-baseline.json
      -CompareToBaseline  compares this run against that file: FAIL when retrieval p95 in either phase exceeds
                          -MaxRegression x the saved value, or the error rate rose. Use the same ExtraArgs both times;
                          a run with different load parameters is refused rather than compared.
.EXAMPLE
    ./infra/scripts/10-loadtest.ps1 -Env dev
    ./infra/scripts/10-loadtest.ps1 -Env dev -ExtraArgs '--rps','5','--baseline-s','120','--during-s','300'
    ./infra/scripts/10-loadtest.ps1 -Env dev -SaveBaseline          # before switching QueryPlan/IngestPlan
    ./infra/scripts/10-loadtest.ps1 -Env dev -CompareToBaseline     # after step 07 has moved the workloads
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [string[]]$ExtraArgs = @(),
    [switch]$SaveBaseline,
    [switch]$CompareToBaseline,
    [ValidateRange(1.0, 3.0)][double]$MaxRegression = 1.10
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
$baselinePath = Join-Path (Split-Path -Parent $Config.OutputsPath) "$($Config.EnvName).loadtest-baseline.json"
$compareArgs = @()
if ($SaveBaseline -or $CompareToBaseline) {
    $imageTag = Get-Value (Get-Outputs -Config $Config) 'imageTag'
    $label = "query=$($Config.QueryPlan) ingest=$($Config.IngestPlan) image=$imageTag"
    Write-Info "Label: $label"
    if ($CompareToBaseline) {
        if (-not (Test-Path -LiteralPath $baselinePath)) { throw "No baseline at $baselinePath. Run with -SaveBaseline first, before changing the deployment." }
        $compareArgs += @('--compare-to', $baselinePath, '--max-regression', [string]$MaxRegression)
        # A comparison run must not replace the baseline it is judged against; it is kept next to it instead.
        $compareArgs += @('--results-json', ($baselinePath -replace 'baseline\.json$', 'latest.json'), '--label', $label)
    }
    else {
        if (Test-Path -LiteralPath $baselinePath) {
            $previous = Get-Content -LiteralPath $baselinePath -Raw | ConvertFrom-Json -AsHashtable
            Write-Warn "Replacing the baseline taken $(Get-Value $previous 'taken_at') ($(Get-Value $previous 'label')). Copy $baselinePath first to keep it."
        }
        $compareArgs += @('--results-json', $baselinePath, '--label', $label)
    }
}
Push-Location $Config.RepoRoot
try {
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    & uv run python scripts/loadtest.py run --base-url $baseUrl @tokenArgs @compareArgs @ExtraArgs
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
