#Requires -Version 7.3

<#
.SYNOPSIS
    Step 05 - Microsoft Foundry: AIServices account (project management enabled) + project + model deployments.
.DESCRIPTION
    - Account: kind AIServices, sku S0, custom sub-domain = account name, system identity, allowProjectManagement.
    - Project: <FoundryProject> (default proj-<prefix>-<env>), connected to Application Insights for tracing (best effort).
    - Deployments: one per role that names an 'aoai' provider (the two roles share a deployment when they name the
      same model), text-embedding-3-small (DeployAoaiEmbedding), and Claude for any role naming a 'claude'
      provider (on failure the portal steps are printed and the script continues).
    - Roles for the managed identity and the deployer: Cognitive Services OpenAI User (Azure OpenAI endpoint) and
      Cognitive Services User (Anthropic endpoint https://<account>.services.ai.azure.com/anthropic).
    Keyless: apps use Entra tokens, so no API key is stored.
.EXAMPLE
    ./infra/scripts/05-foundry.ps1 -Env dev
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [switch]$ApplyChanges
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '05 Microsoft Foundry'
$n = $Config.Names
$rg = $n.ResourceGroup
$loc = $Config.Location
$tags = Get-TagArgs -Config $Config
$deployer = Get-DeployerPrincipal
$miName = Get-Output -Config $Config -Name 'identityName' -ProducedBy '02-identity-keyvault.ps1'
$miPrincipalId = Get-Output -Config $Config -Name 'identityPrincipalId' -ProducedBy '02-identity-keyvault.ps1'
$acct = $n.Foundry
$apiVersion = '2025-06-01'   # pinned ARM api-version for the az rest fallbacks below
$acctUrl = "https://management.azure.com$($Config.ResourceGroupId)/providers/Microsoft.CognitiveServices/accounts/$acct"

# ============================================================================================== account
Write-Step "Foundry account $acct"
$account = Get-AzResourceOrNull @('cognitiveservices', 'account', 'show', '-g', $rg, '-n', $acct)
if (-not $account) {
    $deleted = @(Get-AzTsvValues @('cognitiveservices', 'account', 'list-deleted', '--query', "[?name=='$acct'].name"))
    if ($deleted.Count -gt 0) {
        Write-Info "Recovering soft-deleted account $acct ..."
        $null = Invoke-Az @('cognitiveservices', 'account', 'recover', '-g', $rg, '-n', $acct, '-l', $loc, '-o', 'none')
    }
    else {
        Write-Info "Creating AIServices account $acct ..."
        $null = Invoke-Az (@('cognitiveservices', 'account', 'create', '-g', $rg, '-n', $acct, '-l', $loc, '--kind', 'AIServices', '--sku', 'S0',
                '--custom-domain', $acct, '--assign-identity', '--allow-project-management', 'true', '--yes') + $tags)
    }
    $account = Invoke-Az @('cognitiveservices', 'account', 'show', '-g', $rg, '-n', $acct)
    Write-Ok "account $acct ($($deleted.Count -gt 0 ? 'recovered' : 'created'), state=$(Get-Value $account 'properties.provisioningState'))"
}
else {
    $null = Sync-AzTags -Description "account $acct" -Config $Config -Resource $account
    Write-Ok "account $acct (exists, state=$(Get-Value $account 'properties.provisioningState'))"
}

if ((Get-Value $account 'properties.allowProjectManagement') -ne $true) {
    Write-Info 'Enabling project management on the account'
    try {
        $null = Invoke-Az @('cognitiveservices', 'account', 'update', '-g', $rg, '-n', $acct, '--allow-project-management', 'true', '-o', 'none')
    }
    catch {
        Write-Info 'CLI flag unavailable; using az rest PATCH'
        $null = Invoke-AzRest -Method patch -Url "$($acctUrl)?api-version=$apiVersion" -Body @{ properties = @{ allowProjectManagement = $true } }
    }
    Write-Ok 'allowProjectManagement = true'
}

# ============================================================================================== project
$project = $n.FoundryProject
Write-Step "Foundry project $project"
$projectUrl = "$acctUrl/projects/$($project)?api-version=$apiVersion"
$existingProject = Invoke-AzRest -Method get -Url $projectUrl -AllowNotFound
if ($existingProject) {
    Write-Ok "project $project (exists)"
}
else {
    try {
        $null = Invoke-Az @('cognitiveservices', 'account', 'project', 'create', '-g', $rg, '-n', $acct, '--project-name', $project, '-l', $loc,
            '--display-name', "RAG-OS $($Config.Env)", '--description', 'RAG-OS knowledge assistant', '-o', 'none')
    }
    catch {
        Write-Info 'CLI project create failed; using az rest PUT'
        $null = Invoke-AzRest -Method put -Url $projectUrl -Body @{
            location   = $loc
            identity   = @{ type = 'SystemAssigned' }
            properties = @{ displayName = "RAG-OS $($Config.Env)"; description = 'RAG-OS knowledge assistant' }
        }
    }
    Write-Ok "project $project (created)"
}

# Tracing: connect Application Insights to the project (best effort; the portal can do the same in Project > Tracing).
try {
    $appiId = Get-Output -Config $Config -Name 'appInsightsId' -ProducedBy '01-foundation.ps1'
    $connUrl = "$acctUrl/projects/$project/connections/appinsights?api-version=$apiVersion"
    if (Invoke-AzRest -Method get -Url $connUrl -AllowNotFound) {
        Write-Ok 'project connection appinsights (exists)'
    }
    else {
        $appiName = Get-Output -Config $Config -Name 'appInsightsName' -ProducedBy '01-foundation.ps1'
        $appiConn = Invoke-Az @('monitor', 'app-insights', 'component', 'show', '--app', $appiName, '-g', $rg, '--query', 'connectionString', '-o', 'tsv')
        $null = Invoke-AzRest -Method put -Url $connUrl -Sensitive -Body @{
            properties = @{
                category      = 'AppInsights'
                target        = $appiId
                authType      = 'ApiKey'
                isSharedToAll = $true
                credentials   = @{ key = $appiConn }
                metadata      = @{ ApiType = 'Azure'; ResourceId = $appiId }
            }
        }
        Remove-Variable appiConn
        Write-Ok 'project connection appinsights (created)'
    }
}
catch {
    Write-Warn "Could not connect Application Insights to the project: $($_.Exception.Message.Split("`n")[0])"
    Write-Info 'Non-fatal: RAG-OS exports its own traces directly, so observability still works. Only the Foundry'
    Write-Info 'portal Tracing tab stays empty. Connect it by hand under the project > Tracing if you want it.'
}

# ============================================================================================== deployments
function Set-ModelDeployment {
    param([string]$Deployment, [string]$Format, [string]$Model, [string]$Version, [string]$Sku, [int]$Capacity)
    # 'deployment list' filtered by name rather than 'deployment show': list exits 0 with an empty result when the
    # deployment is absent, so a failure here can never be mistaken for "not deployed yet". That distinction
    # matters because $null means "create", and create is a PUT that would re-apply sku and capacity.
    $current = Invoke-Az @('cognitiveservices', 'account', 'deployment', 'list', '-g', $rg, '-n', $acct, '--query',
        "[?name=='$Deployment'] | [0].{model:properties.model.name, version:properties.model.version, sku:sku.name, capacity:sku.capacity}")
    if ($current -and (Get-Value $current 'model') -eq $Model -and (Get-Value $current 'version') -eq $Version -and (Get-Value $current 'sku') -eq $Sku -and [int](Get-Value $current 'capacity') -eq $Capacity) {
        Write-Ok "deployment $Deployment ($Format/$Model $Version, $Sku x $Capacity) (exists)"
        return
    }
    Write-Info "$($current ? 'Updating' : 'Creating') deployment $Deployment ($Format/$Model $Version, $Sku x $Capacity) ..."
    # 'deployment create' is a PUT: it also updates version/sku/capacity of an existing deployment.
    Invoke-WithRetry -Activity "deployment $Deployment" -MaxAttempts 4 -DelaySeconds 30 -RetryOn '(?i)(conflict|another operation|in progress)' -ScriptBlock {
        $null = Invoke-Az @('cognitiveservices', 'account', 'deployment', 'create', '-g', $rg, '-n', $acct, '--deployment-name', $Deployment,
            '--model-format', $Format, '--model-name', $Model, '--model-version', $Version, '--sku-name', $Sku, '--sku-capacity', [string]$Capacity, '-o', 'none')
    }
    Write-Ok "deployment $Deployment ($($current ? 'updated' : 'created'): $Format/$Model $Version, $Sku x $Capacity)"
}

Write-Step 'Model deployments'
$available = @()
try { $available = @(Invoke-Az @('cognitiveservices', 'account', 'list-models', '-g', $rg, '-n', $acct, '--query', '[].{format:format, name:name, version:version}')) }
catch {
    Write-Warn 'Could not list deployable models; deploying without the pre-check.'
    Write-Info 'A wrong model name or version will now fail at deployment time instead of here, with a less obvious'
    Write-Info 'error. If that happens, check AnswerModelName/AnswerModelVersion in the psd1 against the catalogue.'
}
function Assert-ModelAvailable([string]$Format, [string]$Model, [string]$Version) {
    if ($available.Count -eq 0) { return }
    $match = @($available | Where-Object { $_.format -eq $Format -and $_.name -eq $Model })
    if ($match.Count -eq 0) { throw "Model $Format/$Model is not deployable on $acct in $loc." }
    if (@($match | Where-Object { $_.version -eq $Version }).Count -eq 0) {
        throw "Model $Format/$Model version '$Version' is not available. Available: $(@($match.version | Sort-Object -Unique) -join ', '). Update the psd1."
    }
}

# The two roles share a deployment unless they name different models, so the common case creates one.
$answerDeployment = ''
$utilityDeployment = ''
if ($Config.AnswerModelProvider -eq 'aoai') {
    Assert-ModelAvailable 'OpenAI' $Config.AnswerModelName $Config.AnswerModelVersion
    Set-ModelDeployment -Deployment $Config.AnswerModelName -Format 'OpenAI' -Model $Config.AnswerModelName -Version $Config.AnswerModelVersion `
        -Sku $Config.AnswerModelSku -Capacity $Config.AnswerModelCapacity
    $answerDeployment = $Config.AnswerModelName
}
if ($Config.UtilityModelProvider -eq 'aoai') {
    if ($Config.UtilityModelName -eq $answerDeployment) {
        Write-Info "Utility role shares the answer deployment ($answerDeployment)."
        $utilityDeployment = $answerDeployment
    }
    else {
        Assert-ModelAvailable 'OpenAI' $Config.UtilityModelName $Config.UtilityModelVersion
        Set-ModelDeployment -Deployment $Config.UtilityModelName -Format 'OpenAI' -Model $Config.UtilityModelName -Version $Config.UtilityModelVersion `
            -Sku $Config.UtilityModelSku -Capacity $Config.UtilityModelCapacity
        $utilityDeployment = $Config.UtilityModelName
    }
}

# Throws when the profile needs a deployment that will not be created; warns when one is created for nothing.
$null = Test-EmbeddingDeploymentConfig -Config $Config

if ($Config.DeployAoaiEmbedding) {
    Assert-ModelAvailable 'OpenAI' $Config.EmbeddingModelName $Config.EmbeddingModelVersion
    Set-ModelDeployment -Deployment $Config.EmbeddingDeploymentName -Format 'OpenAI' -Model $Config.EmbeddingModelName `
        -Version $Config.EmbeddingModelVersion -Sku $Config.EmbeddingModelSku -Capacity $Config.EmbeddingModelCapacity
}

# Claude is deployed only for the roles that actually ask for it - there is no separate enable flag.
$claudeWanted = @()
if ($Config.AnswerModelProvider -eq 'claude') { $claudeWanted += $Config.ClaudeAnswerModelName }
if ($Config.UtilityModelProvider -eq 'claude') { $claudeWanted += $Config.ClaudeUtilityModelName }
$claudeWanted = @($claudeWanted | Where-Object { $_ } | Sort-Object -Unique)
$claudeDeployed = $claudeWanted.Count -eq 0   # nothing wanted is not a failure
foreach ($claudeModel in $claudeWanted) {
    Write-Step "Claude on Foundry ($claudeModel)"
    try {
        $version = $Config.ClaudeModelVersion
        if (-not $version) {
            $version = (@($available | Where-Object { $_.format -eq 'Anthropic' -and $_.name -eq $claudeModel } | ForEach-Object { $_.version }) | Sort-Object | Select-Object -Last 1)
            if (-not $version) { throw "Anthropic/$claudeModel is not in the deployable model list for $acct." }
        }
        Set-ModelDeployment -Deployment $claudeModel -Format 'Anthropic' -Model $claudeModel -Version $version `
            -Sku $Config.ClaudeSku -Capacity $Config.ClaudeCapacity
        $claudeDeployed = $true
    }
    catch {
        $claudeDeployed = $false
        Write-Warn "Claude deployment via CLI failed: $($_.Exception.Message.Split("`n")[0])"
        Write-Host @"
    Deploy Claude in the portal instead (the apps only need the deployment to exist under this name):
      1. https://ai.azure.com -> select project '$project' (account '$acct').
      2. Model catalog -> search '$claudeModel' -> Deploy (accept the Anthropic marketplace terms if prompted).
      3. Deployment name: $claudeModel   (must match the psd1)   Deployment type: Global Standard.
      4. Re-run 07-container-apps.ps1.
"@ -ForegroundColor Yellow
    }
}

# Renaming a model in the psd1 does not move a deployment, it creates a second one - and the old deployment
# keeps its share of the region's TPM quota until somebody deletes it. Nothing used to say so. Reporting only:
# a provisioning script does not delete, and a deployment may well be in use by something outside RAG-OS.
$expected = @(@($answerDeployment, $utilityDeployment) + @($Config.DeployAoaiEmbedding ? $Config.EmbeddingDeploymentName : '') + $claudeWanted |
        Where-Object { $_ } | Sort-Object -Unique)
try {
    $onAccount = @(Get-AzTsvValues @('cognitiveservices', 'account', 'deployment', 'list', '-g', $rg, '-n', $acct, '--query', '[].name'))
    $orphans = @($onAccount | Where-Object { $expected -notcontains $_ })
    if ($orphans.Count -gt 0) {
        Write-Warn "$($orphans.Count) deployment(s) on $acct are not named in the psd1 and still hold quota: $($orphans -join ', ')"
        foreach ($orphan in $orphans) { Write-Info "  az cognitiveservices account deployment delete -g $rg -n $acct --deployment-name $orphan" }
    }
}
catch { Write-Info "Could not list deployments to check for orphans: $($_.Exception.Message.Split("`n")[0])" }

# ============================================================================================== roles
Write-Step 'Foundry roles'
foreach ($role in @('Cognitive Services OpenAI User', 'Cognitive Services User')) {
    Grant-Role -PrincipalId $miPrincipalId -PrincipalType 'ServicePrincipal' -Role $role -Scope $account.id -PrincipalLabel $miName
    Grant-Role -PrincipalId $deployer.ObjectId -PrincipalType $deployer.PrincipalType -Role $role -Scope $account.id -PrincipalLabel "deployer $($deployer.Name)"
}

# Keys this configuration no longer produces are removed rather than left behind. The empty-value guard in
# Save-Outputs deliberately keeps a stored value when a run has nothing for it, which is right for a probe but
# wrong here: turning DeployAoaiEmbedding off used to leave the old deployment name in place, so 07 kept
# injecting AOAI_EMBED_DEPLOYMENT for a deployment this step no longer manages.
$stale = @()
if (-not $Config.DeployAoaiEmbedding) { $stale += 'embeddingDeployment' }
if (-not $answerDeployment) { $stale += 'chatDeployment' }
if (-not $utilityDeployment) { $stale += 'utilityDeployment' }
$values = @{
    foundryName     = $acct
    foundryId       = Get-Value $account 'id'
    foundryProject  = $project
    foundryEndpoint = "https://$acct.services.ai.azure.com"
    aoaiEndpoint    = "https://$acct.openai.azure.com"
    claudeDeployed  = $claudeDeployed
}
if ($answerDeployment) { $values.chatDeployment = $answerDeployment }
if ($utilityDeployment) { $values.utilityDeployment = $utilityDeployment }
if ($Config.DeployAoaiEmbedding) { $values.embeddingDeployment = $Config.EmbeddingDeploymentName }
Save-Outputs -Config $Config -Values $values -Clear $stale
Write-Ok 'Foundry ready.'
Write-Info "Verify: az cognitiveservices account deployment list -g $rg -n $acct --query '[].{name:name, model:properties.model.name, version:properties.model.version, state:properties.provisioningState}' -o table"
