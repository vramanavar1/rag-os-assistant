#Requires -Version 7.3
<#
.SYNOPSIS
    Step 00 - tools, login, subscription, resource providers, region/quota checks. Prints a checklist.
.DESCRIPTION
    - pwsh >= 7.3, az >= 2.60; installs/upgrades the containerapp and application-insights az extensions
      (and rdbms-connect with -InstallRdbmsConnect).
    - Logs in to the configured tenant when needed and selects the subscription.
    - Registers the resource providers used by RAG-OS and waits until they are Registered.
    - Checks the region: Container Apps workload profiles (D4/D8 + serverless T4), Foundry models + quota,
      AI Search availability, and the deployer's RBAC rights.
    FAIL items stop provisioning; WARN items have a documented fallback (see Deployment.md section 1 - Prerequisites).
.EXAMPLE
    ./infra/scripts/00-prereqs.ps1 -Env dev
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [switch]$InstallRdbmsConnect
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '00 prerequisites' -SkipAzContext
$loc = $Config.Location
$checks = [System.Collections.Generic.List[object]]::new()
function Add-Check([string]$Item, [ValidateSet('PASS', 'WARN', 'FAIL')][string]$Status, [string]$Detail) {
    $checks.Add([pscustomobject]@{ Status = $Status; Item = $Item; Detail = $Detail })
}

# ---------------------------------------------------------------------------------------------- tools
Write-Step 'Tools'
$psv = $PSVersionTable.PSVersion
Add-Check 'PowerShell >= 7.3' (([version]"$($psv.Major).$($psv.Minor)" -ge [version]'7.3') ? 'PASS' : 'FAIL') "$psv"

$azVersion = (Invoke-Az @('version'))['azure-cli']
Add-Check 'Azure CLI >= 2.60' ([version]$azVersion -ge [version]'2.60.0' ? 'PASS' : 'FAIL') $azVersion

foreach ($ext in @('containerapp', 'application-insights')) {
    Write-Info "az extension add --name $ext --upgrade"
    $null = Invoke-Az @('extension', 'add', '--name', $ext, '--upgrade', '--yes', '-o', 'none')
    $v = Invoke-Az @('extension', 'show', '--name', $ext, '--query', 'version', '-o', 'tsv')
    Add-Check "az extension $ext" 'PASS' $v
}
if ($InstallRdbmsConnect) {
    try {
        $null = Invoke-Az @('extension', 'add', '--name', 'rdbms-connect', '--upgrade', '--yes', '-o', 'none')
        Add-Check 'az extension rdbms-connect (optional)' 'PASS' 'installed'
    }
    catch { Add-Check 'az extension rdbms-connect (optional)' 'WARN' 'install failed - use psql instead' }
}
$uv = Get-Command uv -ErrorAction SilentlyContinue
Add-Check 'uv (smoke/load tests, local CLI)' ($uv ? 'PASS' : 'WARN') ($uv ? $uv.Source : 'not found - needed only for 09/10 and local discovery')

# ---------------------------------------------------------------------------------------------- login + subscription
Write-Step 'Login and subscription'
$account = Get-AzAccountOrNull -Query @('--query', '{tenant:tenantId, sub:id}')
if (-not $account -or $account.tenant -ne $Config.TenantId) {
    Write-Info "Logging in to tenant $($Config.TenantId) (interactive)..."
    $null = Invoke-Az -Stream @('login', '--tenant', $Config.TenantId, '-o', 'none')
}
$null = Invoke-Az @('account', 'set', '--subscription', $Config.SubscriptionId, '-o', 'none')
$sub = Invoke-Az @('account', 'show', '--query', '{name:name, id:id, state:state}')
Add-Check 'Subscription selected' ($sub.state -eq 'Enabled' ? 'PASS' : 'FAIL') "$($sub.name) ($($sub.id)) state=$($sub.state)"

try {
    $deployer = Get-DeployerPrincipal
    Add-Check 'Deployer identity readable (Entra)' 'PASS' "$($deployer.Name) [$($deployer.PrincipalType)] $($deployer.ObjectId)"
}
catch {
    Add-Check 'Deployer identity readable (Entra)' 'FAIL' 'az ad signed-in-user show failed: need permission to read the signed-in user.'
    $deployer = $null
}

if ($deployer) {
    $roles = @(Get-AzTsvValues @('role', 'assignment', 'list', '--assignee-object-id', $deployer.ObjectId, '--all', '--include-inherited', '--include-groups',
            '--fill-principal-name', 'false', '--query', "[?scope=='/subscriptions/$($Config.SubscriptionId)' || starts_with(scope, '$($Config.ResourceGroupId)')].roleDefinitionName"))
    $canAssign = ($roles -contains 'Owner') -or ($roles -contains 'User Access Administrator') -or ($roles -contains 'Role Based Access Control Administrator')
    $canCreate = ($roles -contains 'Owner') -or ($roles -contains 'Contributor')
    $status = ($canAssign -and $canCreate) ? 'PASS' : 'WARN'
    Add-Check 'Deployer RBAC (Owner, or Contributor + User Access Administrator)' $status (($roles | Sort-Object -Unique) -join ', ')
}

# ---------------------------------------------------------------------------------------------- resource providers
Write-Step 'Resource providers'
$providers = @('Microsoft.App', 'Microsoft.OperationalInsights', 'Microsoft.Insights', 'Microsoft.CognitiveServices', 'Microsoft.Search',
    'Microsoft.ServiceBus', 'Microsoft.DBforPostgreSQL', 'Microsoft.ContainerRegistry', 'Microsoft.KeyVault', 'Microsoft.Storage',
    'Microsoft.ManagedIdentity', 'Microsoft.Consumption')
foreach ($p in $providers) {
    $state = Invoke-Az @('provider', 'show', '--namespace', $p, '--query', 'registrationState', '-o', 'tsv')
    if ($state -ne 'Registered') {
        Write-Info "Registering $p ($state)"
        $null = Invoke-Az @('provider', 'register', '--namespace', $p, '-o', 'none')
    }
}
$deadline = (Get-Date).AddMinutes(15)
foreach ($p in $providers) {
    do {
        $state = Invoke-Az @('provider', 'show', '--namespace', $p, '--query', 'registrationState', '-o', 'tsv')
        if ($state -eq 'Registered' -or (Get-Date) -gt $deadline) { break }
        Start-Sleep -Seconds 10
    } while ($true)
    Add-Check "Provider $p" ($state -eq 'Registered' ? 'PASS' : 'FAIL') $state
}

# ---------------------------------------------------------------------------------------------- region checks
Write-Step "Region checks: $loc"
$region = Invoke-Az @('account', 'list-locations', '--query', "[?name=='$loc'] | [0].{name:name, display:displayName}")
if (-not $region) { Add-Check "Region '$loc' exists" 'FAIL' 'unknown location name'; $regionDisplay = $loc }
else { Add-Check "Region '$loc' exists" 'PASS' $region.display; $regionDisplay = $region.display }

# Container Apps workload profiles (D4, D8, serverless T4)
# Only the az call is guarded. The checks below are our own logic, and a defect in them must not be reported as
# 'this region may not offer the profile' - that is precisely how a broken -split here hid as a WARN.
$supported = $null
try { $supported = @(Get-AzTsvValues @('containerapp', 'env', 'workload-profile', 'list-supported', '-l', $loc, '--query', '[].name')) }
catch { Add-Check 'Container Apps workload profiles' 'WARN' "could not list: $($_.Exception.Message.Split("`n")[0])" }
if ($null -ne $supported) {
    foreach ($t in @($Config.QueryProfileType, $Config.IngestProfileType)) {
        Add-Check "Container Apps profile $t" ($supported -contains $t ? 'PASS' : 'FAIL') ($supported -contains $t ? 'supported' : 'not offered in region')
    }
    if ($Config.EnableGpu) {
        $gpuOk = $supported -contains $Config.GpuProfileType
        Add-Check "Container Apps profile $($Config.GpuProfileType)" ($gpuOk ? 'PASS' : 'WARN') ($gpuOk ? 'supported (quota is checked when 07 adds it)' : 'not offered - rag-embed-ingest will run on CPU (ingest profile)')
    }
}

# AI Search
$searchRegions = @(Get-AzTsvValues @('provider', 'show', '--namespace', 'Microsoft.Search', '--query', "resourceTypes[?resourceType=='searchServices'] | [0].locations"))
$searchOk = $searchRegions -contains $regionDisplay
Add-Check "AI Search in region ($($Config.SearchSku), semantic=$($Config.SearchSemantic))" ($searchOk ? 'PASS' : 'FAIL') ($searchOk ? 'available (S1 capacity/semantic ranker: verify in portal if creation fails)' : 'not available')

# Foundry models
function Test-Model([string]$Label, [string]$Format, [string]$Name, [string]$Version, [string]$Sku, [switch]$Optional) {
    $q = "[?model.format=='$Format' && model.name=='$Name'].{v:model.version, skus:model.skus[].name}"
    $found = @(Invoke-Az @('cognitiveservices', 'model', 'list', '-l', $loc, '--query', $q))
    $sev = $Optional ? 'WARN' : 'FAIL'
    if ($found.Count -eq 0) { Add-Check $Label $sev "$Format/$Name not offered in $loc"; return }
    $versions = @($found | ForEach-Object { $_.v }) | Sort-Object -Unique
    if ($Version -and $versions -notcontains $Version) { Add-Check $Label $sev "version $Version not offered; available: $($versions -join ', ')"; return }
    $skus = @($found | ForEach-Object { $_.skus } | Where-Object { $_ }) | Sort-Object -Unique
    if ($Sku -and $skus.Count -gt 0 -and $skus -notcontains $Sku) { Add-Check $Label 'WARN' "sku $Sku not listed; listed: $($skus -join ', ')"; return }
    Add-Check $Label 'PASS' "versions: $($versions -join ', ')"
}
try {
    if ($Config.AnswerModelProvider -eq 'aoai') {
        Test-Model 'Foundry answer model' 'OpenAI' $Config.AnswerModelName $Config.AnswerModelVersion $Config.AnswerModelSku
    }
    # Only checked when the utility role has a deployment of its own; sharing one is the common case.
    if ($Config.UtilityModelProvider -eq 'aoai' -and $Config.UtilityModelName -ne $Config.AnswerModelName) {
        Test-Model 'Foundry utility model' 'OpenAI' $Config.UtilityModelName $Config.UtilityModelVersion $Config.UtilityModelSku
    }
    if ($Config.DeployAoaiEmbedding) { Test-Model 'Foundry embedding model' 'OpenAI' $Config.EmbeddingModelName $Config.EmbeddingModelVersion $Config.EmbeddingModelSku }
    # DeployAoaiEmbedding deploys a model; EmbeddingProfile selects one. Setting either alone has a failure mode.
    try {
        $embedProvider = Test-EmbeddingDeploymentConfig -Config $Config
        if ($embedProvider) { Add-Check 'Embedding provider' 'PASS' "$($Config.EmbeddingProfile) -> $embedProvider" }
    }
    catch { Add-Check 'Embedding provider' 'FAIL' ($_.Exception.Message -split "`n")[0] }
    if ($Config.AnswerModelProvider -eq 'claude') {
        Test-Model 'Claude answer model' 'Anthropic' $Config.ClaudeAnswerModelName $Config.ClaudeModelVersion '' -Optional
    }
    if ($Config.UtilityModelProvider -eq 'claude') {
        Test-Model 'Claude utility model' 'Anthropic' $Config.ClaudeUtilityModelName $Config.ClaudeModelVersion '' -Optional
    }
    $usage = @(Invoke-Az @('cognitiveservices', 'usage', 'list', '-l', $loc, '--query',
            "[?contains(name.value, '$($Config.AnswerModelName)')].{name:name.value, used:currentValue, limit:limit}"))
    foreach ($u in $usage) {
        $free = [double]$u.limit - [double]$u.used
        $need = if ($u.name -like "*$($Config.AnswerModelSku)*") { [double]$Config.AnswerModelCapacity } else { 0 }
        Add-Check "Quota $($u.name)" (($need -gt 0 -and $free -lt $need) ? 'WARN' : 'PASS') "used $($u.used) / limit $($u.limit)"
    }
}
catch { Add-Check 'Foundry model catalog / quota' 'WARN' "could not query: $($_.Exception.Message.Split("`n")[0])" }

Save-Outputs -Config $Config -Values @{ subscriptionId = $Config.SubscriptionId; tenantId = $Config.TenantId; location = $loc }

# ---------------------------------------------------------------------------------------------- checklist
Write-Step 'Checklist'
$checks | Format-Table -AutoSize -Wrap | Out-String -Width 220 | Write-Host
Write-Info 'Manual checks (not scriptable): request serverless GPU quota for Container Apps if the T4 profile fails in 07;'
Write-Info 'request Claude access in the Foundry portal (Model catalog) if you plan LLM_ANSWER=claude.'
$failed = @($checks | Where-Object Status -eq 'FAIL')
if ($failed.Count -gt 0) { throw "$($failed.Count) prerequisite check(s) FAILED - fix them (see Deployment.md section 1 - Prerequisites) and re-run." }
Write-Ok 'Prerequisites satisfied.'
