#Requires -Version 7.3

<#
.SYNOPSIS
    Runs steps 00 -> 08 in order, stopping at the first failure, and prints the elapsed time per step.
.DESCRIPTION
    A full first run takes roughly 45-80 minutes; PostgreSQL, AI Search and the image builds dominate.
    Every step is idempotent, so re-running after a failure is safe. Use -From/-To to resume at a step.

    The Entra app registration is reconciled as part of the run, twice, because its two halves have different
    prerequisites. The scope, the application roles and the token version need nothing from Azure and must exist
    BEFORE step 00 validates them - step 00 fails a registration with no roles, so running it first would abort
    a deployment on something the next action was about to create. The SPA redirect URI is the opposite: it needs
    the chat UI FQDN, which only exists once step 07 has recorded it. The script is idempotent, so the second
    pass writes only that URI.
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
$entraScript = 'Set-EntraAppRegistration.ps1'
# Sign-in is configured only when all four values are set. Leave them empty and this is the documented dev-auth
# deployment, where there is no registration to reconcile and the Entra passes are skipped rather than warned at.
$provisionConfig = Import-RagOsConfig -Env $Env
$entraConfigured = [bool]($provisionConfig.EntraTenantId -and $provisionConfig.EntraClientId `
        -and $provisionConfig.EntraAudience -and $provisionConfig.EntraApiScope)

$results = [System.Collections.Generic.List[object]]::new()
$total = [Diagnostics.Stopwatch]::StartNew()
$failedAt = $null
$failedScript = ''
$errorText = ''

function Invoke-ProvisionStep {
    <#
    .SYNOPSIS
        Runs one script, records its elapsed time, and on failure captures what the summary needs before rethrowing.
    .DESCRIPTION
        Extracted so the Entra passes get exactly the same treatment as a numbered step - a row in the elapsed
        table and a failure the summary can describe - rather than being invoked off to one side where a failure
        would abort the run without ever appearing in it.
    .PARAMETER ResumeFrom
        The step number to print in the "-From N" resume hint. The Entra passes are not numbered steps, so each
        borrows the number of the step it is pinned to; resuming there re-runs it, which is safe because it is
        idempotent.
    #>
    param(
        [Parameter(Mandatory)][string]$Script,
        [Parameter(Mandatory)][string]$Heading,
        [Parameter(Mandatory)][int]$ResumeFrom,
        # A HASHTABLE, not an array: array splatting passes elements positionally, so @('-Brief') would
        # bind the literal string '-Brief' to the next positional parameter and leave the switch false.
        # Verified, because the silent failure mode here is a redirect URI of https://-Brief/auth/callback.
        [hashtable]$Arguments = @{}
    )
    Write-Host ''
    Write-Host ("=" * 100) -ForegroundColor DarkGray
    Write-Host $Heading -ForegroundColor Magenta
    Write-Host ("=" * 100) -ForegroundColor DarkGray
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    try {
        & (Join-Path $PSScriptRoot $Script) -Env $Env @Arguments
        $script:results.Add([pscustomobject]@{ Step = $Script; Minutes = [math]::Round($stopwatch.Elapsed.TotalMinutes, 1); Result = 'OK' })
    }
    catch {
        $script:results.Add([pscustomobject]@{ Step = $Script; Minutes = [math]::Round($stopwatch.Elapsed.TotalMinutes, 1); Result = 'FAILED' })
        $script:failedAt = $ResumeFrom
        $script:failedScript = $Script
        $script:errorText = "$($_.Exception.Message)"
        throw
    }
}

Write-Host ''
Write-Host "RAG-OS provisioning | env=$Env | steps $From..$To" -ForegroundColor Magenta
Write-Host "Every step is idempotent, so re-running after a failure is safe." -ForegroundColor DarkGray
# Two different reasons the Entra passes might not run, and conflating them sends the reader to the wrong place:
# empty settings is a dev-token deployment with nothing to reconcile, while a resume that starts after step 0 has
# a registration to configure and simply is not reaching it. Saying neither is worse still - the header announces
# the range, every selected step reports OK, and a silently unconfigured registration looks like a finished one.
if (-not $entraConfigured) {
    Write-Host "Entra* settings are empty: skipping the app registration (dev-token sign-in only)." -ForegroundColor DarkGray
}
else {
    if ($From -gt 0) {
        Write-Host "Skipping Set-EntraAppRegistration.ps1 (scope, application roles): it runs before step 00, and -From $From starts after it." -ForegroundColor Yellow
        Write-Host "    Run it on its own:  ./infra/scripts/Set-EntraAppRegistration.ps1 -Env $Env" -ForegroundColor Yellow
    }
    if ($From -gt 7 -or $To -lt 7) {
        Write-Host "Skipping Set-EntraAppRegistration.ps1 (SPA redirect URI): it follows step 07, which is outside $From..$To." -ForegroundColor DarkGray
    }
}

try {
    # Before step 00, which validates what this creates. -Brief drops the standalone closing guidance, which
    # would otherwise tell the operator to re-run step 07 on a pass that happens before step 07 has run.
    if ($entraConfigured -and $From -le 0) {
        Invoke-ProvisionStep -Script $entraScript -ResumeFrom 0 -Arguments @{ Brief = $true } `
            -Heading "STEP 00-pre : $entraScript (scope, application roles, token version)"
    }

    foreach ($number in $steps.Keys) {
        if ($number -lt $From -or $number -gt $To) { continue }
        Invoke-ProvisionStep -Script $steps[$number] -ResumeFrom $number -Heading "STEP $number of $To : $($steps[$number])"

        # Step 07 has just recorded chatUiFqdn, so the SPA redirect URI can finally be registered. Kept here
        # rather than after the loop so the dependency reads in the order it actually happens.
        if ($number -eq 7 -and $entraConfigured) {
            Invoke-ProvisionStep -Script $entraScript -ResumeFrom 7 -Arguments @{ Brief = $true } `
                -Heading "STEP 07-post : $entraScript (SPA redirect URI)"
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
        Write-Host "Step $failedAt ($failedScript) failed:" -ForegroundColor Red
        Write-Host (($errorText -split "`r?`n" | ForEach-Object { "    $_" }) -join [Environment]::NewLine) -ForegroundColor Red
        Write-Host ''
        Write-Host "Provisioning stopped at step $failedAt ($failedScript). Fix the error above, then resume with:" -ForegroundColor Yellow
        Write-Host "    ./infra/scripts/provision-all.ps1 -Env $Env -From $failedAt" -ForegroundColor Yellow
        Write-Host "Earlier steps are not repeated. To re-run just the one step: ./infra/scripts/$failedScript -Env $Env" -ForegroundColor Yellow
        if ($errorText -match '(?i)MissingSubscriptionRegistration|not registered to use namespace') {
            Write-Host "That error is an unregistered Azure resource provider, which step 00 registers. Run it first:" -ForegroundColor Yellow
            Write-Host "    ./infra/scripts/00-prereqs.ps1 -Env $Env" -ForegroundColor Yellow
        }
        if ($failedScript -eq $entraScript) {
            Write-Host "Writing an app registration needs a DIRECTORY role (Application Administrator or Cloud" -ForegroundColor Yellow
            Write-Host "Application Administrator); a subscription Owner does not have it. See Deployment.md section 9." -ForegroundColor Yellow
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
