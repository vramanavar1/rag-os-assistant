#Requires -Version 7.3

<#
.SYNOPSIS
    Step 09 - smoke test of the deployed environment: infrastructure assertions + scripts/smoke.py.
.DESCRIPTION
    Checks here (Azure control plane):
      1. rag-api ingress is internal (external = false).
      2. Every app/job secret is a Key Vault reference - no literal secret values in the app definitions.
      3. The chat UI answers on its public URL.
      4. The internal API host is not resolvable/reachable from outside.
    Then runs `uv run python scripts/smoke.py --base-url https://<chat-ui>` for the functional checks (HR/EU answered with
    a citation, Sales/US refused and HR absent from their facets, usage present, upload -> tracking_id -> INDEXED).
    When dev auth is on, the dev signing key is read from Key Vault and passed as DEV_JWT_KEY. Otherwise two Entra
    access tokens for two different people are acquired with `az account get-access-token` against EntraApiScope and
    passed as --token-a/--token-b; pass -ExtraArgs to override. Nothing is printed.
.EXAMPLE
    ./infra/scripts/09-smoke.ps1 -Env dev
    ./infra/scripts/09-smoke.ps1 -Env dev -ExtraArgs '--verbose'
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [string[]]$ExtraArgs = @(),
    [switch]$SkipPython,
    # Run the smoke tests even when the embedding alignment check fails. The results are not trustworthy: a
    # mismatch means the answers are drawn from a vector space the queries do not share.
    [switch]$SkipAlignmentCheck
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '09 smoke test'
$rg = $Config.Names.ResourceGroup
$keyVault = Get-Output -Config $Config -Name 'keyVaultName' -ProducedBy '02-identity-keyvault.ps1'
$failures = [System.Collections.Generic.List[string]]::new()
function Test-Assert([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Ok $Name } else { Write-Host "    [FAIL] $Name $Detail" -ForegroundColor Red; $failures.Add($Name) }
}

Write-Step 'Infrastructure checks'
$apiExternal = Invoke-Az @('containerapp', 'show', '-g', $rg, '-n', 'rag-api', '--query', 'properties.configuration.ingress.external', '-o', 'tsv')
Test-Assert 'rag-api ingress is internal' ($apiExternal -eq 'false') "(external=$apiExternal)"

foreach ($app in @('rag-api', 'rag-ingest-worker', 'rag-chat-ui')) {
    $inline = @(Get-AzTsvValues @('containerapp', 'show', '-g', $rg, '-n', $app, '--query', 'properties.configuration.secrets[?keyVaultUrl==null].name'))
    Test-Assert "$app has only Key Vault secret references" ($inline.Count -eq 0) "(inline: $($inline -join ', '))"
}
foreach ($job in @('rag-scheduler', 'rag-bootstrap')) {
    $inline = @(Get-AzTsvValues @('containerapp', 'job', 'show', '-g', $rg, '-n', $job, '--query', 'properties.configuration.secrets[?keyVaultUrl==null].name'))
    Test-Assert "$job has only Key Vault secret references" ($inline.Count -eq 0) "(inline: $($inline -join ', '))"
}

$baseUrl = Get-ChatUiUrl -Config $Config
Write-Info "Chat UI: $baseUrl"
try {
    $response = Invoke-WebRequest -Uri $baseUrl -TimeoutSec 30 -SkipHttpErrorCheck
    Test-Assert 'chat UI responds' ($response.StatusCode -lt 400) "(HTTP $($response.StatusCode))"
}
catch { Test-Assert 'chat UI responds' $false $_.Exception.Message }

$apiFqdn = Invoke-Az @('containerapp', 'show', '-g', $rg, '-n', 'rag-api', '--query', 'properties.configuration.ingress.fqdn', '-o', 'tsv')
$apiReachable = $false
if ($apiFqdn) {
    try {
        $null = Invoke-WebRequest -Uri "https://$apiFqdn/api/healthz" -TimeoutSec 10 -SkipHttpErrorCheck
        $apiReachable = $true
    }
    catch { $apiReachable = $false }
}
Test-Assert 'rag-api is NOT reachable from outside' (-not $apiReachable) "($apiFqdn)"

# ------------------------------------------------------------------------------------ embedding alignment gate
# Before anything functional, because a mismatch does not make the smoke test fail cleanly - it makes the
# ingestion check fail by TIMEOUT, four minutes later, reading like a broken worker. And it throws rather than
# joining $failures: every assertion after this one would be measuring a system whose retrieval is meaningless,
# so there is nothing to learn by continuing.
if ($SkipAlignmentCheck) { Write-Warn 'Skipping the embedding alignment check (-SkipAlignmentCheck).' }
else {
    & (Join-Path $PSScriptRoot 'Test-EmbeddingAlignment.ps1') -Env $Env
    if ($LASTEXITCODE -ne 0) {
        throw ('Embedding alignment check failed - see above. The documents and the queries may not share a ' +
            'vector space, which makes every retrieval result meaningless rather than merely wrong. Fix it, or ' +
            'pass -SkipAlignmentCheck to run the smoke tests anyway.')
    }
}

# ---------------------------------------------------------------------------------------------- functional checks
if ($SkipPython) { Write-Info 'Skipping scripts/smoke.py (-SkipPython)' }
else {
    $smoke = Join-Path $Config.RepoRoot 'scripts/smoke.py'
    if (-not (Test-Path -LiteralPath $smoke)) { throw "scripts/smoke.py not found at $smoke" }
    Write-Step 'scripts/smoke.py'
    $devKey = $null
    if ($Config.DevAuthEnabled) { $devKey = Get-KeyVaultSecretValue -VaultName $keyVault -Name 'dev-jwt-signing-key' }
    $tokenArgs = @()
    if (-not $Config.DevAuthEnabled -and $Config.EntraApiScope -and $ExtraArgs -notcontains '--token-a') {
        # Entra-only: the operator's own access token stands in for a signed-in user. There is only one identity,
        # so --same-identity makes smoke.py skip the A/B separation checks outright. Without it they would compare
        # that identity against itself and report a pass or a failure that means nothing either way.
        Write-Info "Acquiring an Entra access token for $($Config.EntraApiScope)"
        $tok = (az account get-access-token --scope "$($Config.EntraApiScope)" --query accessToken -o tsv)
        if ($LASTEXITCODE -ne 0 -or -not $tok) { throw 'az account get-access-token failed: sign in with az login, or pass -ExtraArgs "--token-a <jwt> --token-b <jwt>".' }
        $tokenArgs = @('--token-a', $tok, '--token-b', $tok, '--same-identity')
        Write-Warn 'One identity only: the access-separation checks will be SKIPPED, not passed.'
        Write-Info 'To verify them properly, get access tokens for two real people with different attributes and'
        Write-Info '  pass -ExtraArgs "--token-a <jwt> --token-b <jwt> --admin-token <jwt>" (this also enables the'
        Write-Info '  upload -> INDEXED check, which needs an admin or contributor token).'
    }
    Push-Location $Config.RepoRoot
    try {
        if ($devKey) { $env:DEV_JWT_KEY = $devKey }
        & uv run python scripts/smoke.py --base-url $baseUrl @tokenArgs @ExtraArgs
        Test-Assert 'scripts/smoke.py' ($LASTEXITCODE -eq 0) "(exit $LASTEXITCODE)"
    }
    finally {
        Remove-Item Env:DEV_JWT_KEY -ErrorAction SilentlyContinue
        Remove-Variable devKey, tok, tokenArgs -ErrorAction SilentlyContinue
        Pop-Location
    }
}

Write-Step 'Result'
if ($failures.Count -gt 0) { throw "Smoke test FAILED: $($failures -join '; ')" }
Write-Ok 'All smoke checks passed.'
