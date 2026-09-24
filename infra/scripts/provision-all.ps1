#Requires -Version 7.3
<#
.SYNOPSIS
    Runs steps 00 -> 08 in order, stopping at the first failure, and prints the elapsed time per step.
.DESCRIPTION
    A full first run takes roughly 45-80 minutes; PostgreSQL, AI Search and the image builds dominate.
    Every step is idempotent, so re-running after a failure is safe. Use -From/-To to resume at a step.
.EXAMPLE
    ./infra/scripts/provision-all.ps1 -Env dev
    ./infra/scripts/provision-all.ps1 -Env dev -From 6          # rebuild images and redeploy
    ./infra/scripts/provision-all.ps1 -Env dev -From 1 -To 5    # infrastructure only
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [ValidateRange(0, 8)][int]$From = 0,
    [ValidateRange(0, 8)][int]$To = 8
)
. (Join-Path $PSScriptRoot 'common.ps1')

$steps = [ordered]@{
    0 = '00-prereqs.ps1'
    1 = '01-foundation.ps1'
    2 = '02-identity-keyvault.ps1'
    3 = '03-data.ps1'
    4 = '04-search.ps1'
    5 = '05-foundry.ps1'
    6 = '06-registry-build.ps1'
    7 = '07-container-apps.ps1'
    8 = '08-bootstrap.ps1'
}
$results = [System.Collections.Generic.List[object]]::new()
$total = [Diagnostics.Stopwatch]::StartNew()
$failedAt = $null
$errorText = ''
Write-Host ''
Write-Host "RAG-OS provisioning | env=$Env | steps $From..$To" -ForegroundColor Magenta
Write-Host "Every step is idempotent, so re-running after a failure is safe." -ForegroundColor DarkGray

try {
    foreach ($number in $steps.Keys) {
        if ($number -lt $From -or $number -gt $To) { continue }
        $script = Join-Path $PSScriptRoot $steps[$number]
        Write-Host ''
        Write-Host ("=" * 100) -ForegroundColor DarkGray
        Write-Host "STEP $number of $To : $($steps[$number])" -ForegroundColor Magenta
        Write-Host ("=" * 100) -ForegroundColor DarkGray
        $stopwatch = [Diagnostics.Stopwatch]::StartNew()
        try {
            & $script -Env $Env
            $results.Add([pscustomobject]@{ Step = $steps[$number]; Minutes = [math]::Round($stopwatch.Elapsed.TotalMinutes, 1); Result = 'OK' })
        }
        catch {
            $results.Add([pscustomobject]@{ Step = $steps[$number]; Minutes = [math]::Round($stopwatch.Elapsed.TotalMinutes, 1); Result = 'FAILED' })
            $failedAt = $number
            $errorText = "$($_.Exception.Message)"
            throw
        }
    }
}
finally {
    Write-Host ''
    Write-Host "Elapsed per step (total $([math]::Round($total.Elapsed.TotalMinutes, 1)) min):" -ForegroundColor Magenta
    $results | Format-Table -AutoSize | Out-String | Write-Host
    if ($null -ne $failedAt) {
        # PowerShell runs finally WHILE the exception is still propagating, so everything printed here lands
        # before PowerShell renders the error - guidance saying "fix the error above" would point at nothing.
        # Print the captured text ourselves. It appears twice as a result, which is the right trade: re-throwing
        # is what gives the run a non-zero exit code.
        Write-Host "Step $failedAt ($($steps[$failedAt])) failed:" -ForegroundColor Red
        Write-Host (($errorText -split "`r?`n" | ForEach-Object { "    $_" }) -join [Environment]::NewLine) -ForegroundColor Red
        Write-Host ''
        Write-Host "Provisioning stopped at step $failedAt ($($steps[$failedAt])). Fix the error above, then resume with:" -ForegroundColor Yellow
        Write-Host "    ./infra/scripts/provision-all.ps1 -Env $Env -From $failedAt" -ForegroundColor Yellow
        Write-Host "Earlier steps are not repeated. To re-run just the one step: ./infra/scripts/$($steps[$failedAt]) -Env $Env" -ForegroundColor Yellow
        if ($errorText -match '(?i)MissingSubscriptionRegistration|not registered to use namespace') {
            Write-Host "That error is an unregistered Azure resource provider, which step 00 registers. Run it first:" -ForegroundColor Yellow
            Write-Host "    ./infra/scripts/00-prereqs.ps1 -Env $Env" -ForegroundColor Yellow
        }
    }
}

# Only a run that reached step 8 has actually deployed anything - anything less must not claim it did, because
# output.txt does not exist yet and 09-smoke.ps1 would fail against apps that were never created.
if ($To -lt 8) {
    $remaining = @($steps.Keys | Where-Object { $_ -gt $To } | ForEach-Object { $steps[$_] })
    Write-Ok "Steps $From..$To finished."
    Write-Info "NOT run: $($remaining -join ', ')"
    Write-Info "Continue with: ./infra/scripts/provision-all.ps1 -Env $Env -From $($To + 1)"
}
else {
    Write-Ok 'Provisioning finished.'
    Write-Info "Wiring sheet (secret -> env var mapping, no values): $(Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'output.txt')"
    Write-Info "Next: ./infra/scripts/09-smoke.ps1 -Env $Env"
}
