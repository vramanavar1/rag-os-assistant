#Requires -Version 7.3

<#
.SYNOPSIS
    Step 07 - Container Apps environment (workload profiles query/ingest/gpu-t4) + every app and job from
    infra/containerapps/*.yaml.tmpl.
.DESCRIPTION
    - Environment with workload profiles, logs to Log Analytics.
    - Profiles: query (D4), ingest (D8), gpu-t4 (Consumption-GPU-NC8as-T4). If the GPU profile cannot be added
      (quota/region) rag-embed-ingest runs the CPU image on the ingest profile instead.
    - Renders the templates ({{TOKENS}}), then az containerapp create|update --yaml and az containerapp job create|update --yaml.
      Images are deployed by digest from infra/env/<env>.images.json (written by 06), or by -Tag.
    - Every workload uses the user-assigned identity for ACR pulls, Key Vault secret references and Azure SDK calls
      (AZURE_CLIENT_ID). Secrets are keyvaultref -> secretRef only.
    Rendered YAML (no secret values) is kept in the temp folder printed below for troubleshooting.
.EXAMPLE
    ./infra/scripts/07-container-apps.ps1 -Env dev
    ./infra/scripts/07-container-apps.ps1 -Env dev -Tag 3f2a1bc              # deploy (or roll back to) a specific tag
    ./infra/scripts/07-container-apps.ps1 -Env dev -Only rag-api,rag-chat-ui
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [string]$Tag,
    [ValidateSet('rag-embed-query', 'rag-embed-ingest', 'rag-api', 'rag-ingest-worker', 'rag-chat-ui', 'rag-scheduler', 'rag-bootstrap')]
    [string[]]$Only,
    # Deploy even when the database has not had this checkout's migrations applied. Only for deliberately
    # putting an image out ahead of its migration; the default refuses, because the accident is silent.
    [switch]$SkipSchemaCheck
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '07 Container Apps'
$n = $Config.Names
$rg = $n.ResourceGroup
$loc = $Config.Location
$o = Get-Outputs -Config $Config
$required = @{
    identityId = '02'; identityClientId = '02'; keyVaultUri = '02'; blobEndpoint = '03'; stateDbUrl = '03'; serviceBusFqdn = '03'
    serviceBusName = '03'; searchEndpoint = '04'; aoaiEndpoint = '05'; foundryName = '05'; acrLoginServer = '06'
    acrName = '06'; logAnalyticsName = '01'; logAnalyticsCustomerId = '01'
}
# There is no Azure OpenAI chat deployment when the answer role is served by Claude, so demanding one made a
# Claude-only configuration fail here with 'Run step 05 first' - pointing at a step that had in fact succeeded.
if ($Config.AnswerModelProvider -eq 'aoai') { $required.chatDeployment = '05' }
foreach ($key in $required.Keys) { $null = Get-Output -Config $Config -Name $key -ProducedBy "step $($required[$key])" }
$templatesDir = Join-Path $Config.InfraDir 'containerapps'

# ======================================================================================= schema pre-flight
# This step redefines the rag-bootstrap job with the new image but never starts it, so nothing here applies a
# migration. Rolling an image forward past its migration takes out every query against the changed tables.
if (-not $SkipSchemaCheck) {
    Write-Step 'Checking the database schema matches this checkout'
    if (-not (Test-SchemaUpToDate -BaseUrl (Get-ChatUiUrl -Config $Config) -RepoRoot $Config.RepoRoot -Env $Env)) {
        throw 'Database schema is behind this checkout. Run 08-bootstrap.ps1 first, or pass -SkipSchemaCheck.'
    }
}

# ============================================================================================== images
Write-Step 'Images'
# A remote embedding profile never calls the TEI pools, so demanding their images would block the deploy on
# images that will never be pulled. $null (profile unreadable) keeps today's behaviour: require everything.
$embedProvider = Get-EmbeddingProfileProvider -Config $Config
$useTei = (-not $embedProvider) -or $embedProvider -eq 'tei'
$repos = @('rag-api', 'rag-chat-ui')
if ($useTei) { $repos += @('rag-embedder-cpu', 'rag-embedder-turing') }
$imageRefs = @{}
if ($Tag) {
    foreach ($repo in $repos) {
        $digest = Invoke-Az @('acr', 'repository', 'show', '-n', $o.acrName, '--image', "$($repo):$Tag", '--query', 'digest', '-o', 'tsv') -AllowNotFound
        if ($digest) { $imageRefs[$repo] = "$($o.acrLoginServer)/$repo@$digest" }
    }
}
elseif (Test-Path -LiteralPath $Config.ImagesPath) {
    $manifest = Get-Content -LiteralPath $Config.ImagesPath -Raw | ConvertFrom-Json -AsHashtable
    foreach ($repo in $repos) { $ref = Get-Value $manifest "images.$repo.ref"; if ($ref) { $imageRefs[$repo] = $ref } }
}
foreach ($repo in $repos) {
    if (-not $imageRefs.ContainsKey($repo)) { throw "No image for $repo (tag '$Tag'). Run 06-registry-build.ps1 first." }
    Write-Info "$repo -> $($imageRefs[$repo])"
}

# ============================================================================================== environment
Write-Step "Container Apps environment $($n.ContainerEnv)"
$cae = Get-AzResourceOrNull @('containerapp', 'env', 'show', '-g', $rg, '-n', $n.ContainerEnv)
if (-not $cae) {
    Write-Info 'Creating environment (workload profiles, Log Analytics) ...'
    $lawKey = Invoke-Az -Sensitive @('monitor', 'log-analytics', 'workspace', 'get-shared-keys', '-g', $rg, '-n', $o.logAnalyticsName, '--query', 'primarySharedKey', '-o', 'tsv')
    $null = Invoke-Az -Sensitive (@('containerapp', 'env', 'create', '-g', $rg, '-n', $n.ContainerEnv, '-l', $loc,
            '--enable-workload-profiles', 'true', '--logs-destination', 'log-analytics',
            '--logs-workspace-id', $o.logAnalyticsCustomerId, '--logs-workspace-key', $lawKey) + (Get-TagArgs -Config $Config))
    Remove-Variable lawKey
    $cae = Invoke-Az @('containerapp', 'env', 'show', '-g', $rg, '-n', $n.ContainerEnv)
    Write-Ok "environment $($n.ContainerEnv) (created)"
}
else {
    $null = Sync-AzTags -Description "environment $($n.ContainerEnv)" -Config $Config -Resource $cae
    Write-Ok "environment $($n.ContainerEnv) (exists)"
}

Write-Step 'Workload profiles'
function Get-Profiles {
    $list = Invoke-Az @('containerapp', 'env', 'show', '-g', $rg, '-n', $n.ContainerEnv, '--query', 'properties.workloadProfiles')
    $map = @{}
    foreach ($p in @($list)) { if ($p) { $map[$p.name] = $p } }
    return $map
}
$profiles = Get-Profiles
$dedicated = @(
    @{ Name = 'query'; Type = $Config.QueryProfileType; Min = $Config.QueryMinNodes; Max = $Config.QueryMaxNodes }
    @{ Name = 'ingest'; Type = $Config.IngestProfileType; Min = $Config.IngestMinNodes; Max = $Config.IngestMaxNodes }
)
foreach ($p in $dedicated) {
    $existing = $profiles[$p.Name]
    if (-not $existing) {
        $null = Invoke-Az @('containerapp', 'env', 'workload-profile', 'add', '-g', $rg, '-n', $n.ContainerEnv, '--workload-profile-name', $p.Name,
            '--workload-profile-type', $p.Type, '--min-nodes', [string]$p.Min, '--max-nodes', [string]$p.Max, '-o', 'none')
        Write-Ok "profile $($p.Name) ($($p.Type), $($p.Min)-$($p.Max) nodes) (added)"
    }
    else {
        # A profile's VM type is fixed once it is added, and only the node counts were ever compared - so the
        # '(exists)' line printed the type the environment actually has next to the node counts the psd1 asks
        # for, and a D4->D8 edit looked applied when nothing had changed.
        $actualType = "$(Get-Value $existing 'workloadProfileType')"
        if ($actualType -and $actualType -ne $p.Type) {
            Write-Warn "profile $($p.Name) is '$actualType' but the psd1 asks for '$($p.Type)'. A profile's VM type cannot be changed."
            Write-Info "  Add a profile of the new type and move the apps onto it, or delete this one first:"
            Write-Info "  az containerapp env workload-profile delete -g $rg -n $($n.ContainerEnv) --workload-profile-name $($p.Name)"
        }
        if ([int](Get-Value $existing 'minimumCount') -ne [int]$p.Min -or [int](Get-Value $existing 'maximumCount') -ne [int]$p.Max) {
            $null = Invoke-Az @('containerapp', 'env', 'workload-profile', 'update', '-g', $rg, '-n', $n.ContainerEnv, '--workload-profile-name', $p.Name,
                '--min-nodes', [string]$p.Min, '--max-nodes', [string]$p.Max, '-o', 'none')
            Write-Ok "profile $($p.Name) (updated: nodes $(Get-Value $existing 'minimumCount')-$(Get-Value $existing 'maximumCount')->$($p.Min)-$($p.Max))"
        }
        else { Write-Ok "profile $($p.Name) (exists: $actualType, $($p.Min)-$($p.Max) nodes)" }
    }
}

$useGpu = [bool]$Config.EnableGpu
if ($useGpu -and -not $profiles.ContainsKey('gpu-t4')) {
    try {
        $null = Invoke-Az @('containerapp', 'env', 'workload-profile', 'add', '-g', $rg, '-n', $n.ContainerEnv, '--workload-profile-name', 'gpu-t4',
            '--workload-profile-type', $Config.GpuProfileType, '-o', 'none')
        Write-Ok "profile gpu-t4 ($($Config.GpuProfileType)) (added)"
    }
    catch {
        Write-Warn "GPU profile could not be added (quota or region): $($_.Exception.Message.Split("`n") | Where-Object { $_.Trim() } | Select-Object -Last 1)"
        Write-Warn 'rag-embed-ingest will run the CPU image on the ingest profile. Request serverless GPU quota and re-run 07 to switch.'
        $useGpu = $false
    }
}
elseif ($useGpu) {
    $gpuType = "$(Get-Value $profiles['gpu-t4'] 'workloadProfileType')"
    if ($gpuType -and $gpuType -ne $Config.GpuProfileType) {
        Write-Warn "profile gpu-t4 is '$gpuType' but GpuProfileType asks for '$($Config.GpuProfileType)'. A profile's VM type cannot be changed."
        Write-Info "  Delete it and re-run: az containerapp env workload-profile delete -g $rg -n $($n.ContainerEnv) --workload-profile-name gpu-t4"
    }
    Write-Ok "profile gpu-t4 (exists: $gpuType)"
}
else {
    Write-Info 'GPU disabled in the psd1 (EnableGpu = $false).'
    # Turning EnableGpu off moves the workload to CPU but leaves the profile attached, and an idle GPU profile
    # with min-nodes above zero keeps billing.
    if ($profiles.ContainsKey('gpu-t4')) {
        Write-Warn "The 'gpu-t4' workload profile is still attached to $($n.ContainerEnv) even though EnableGpu is off."
        Write-Info "  Remove it when nothing uses it: az containerapp env workload-profile delete -g $rg -n $($n.ContainerEnv) --workload-profile-name gpu-t4"
    }
}

# The query and ingest pools are separate apps from separate image repos. They only serve the same vectors
# because both were built from the same MODEL_ID/MODEL_REVISION. A partial rebuild (06 -Images embedder-cpu) or
# a targeted redeploy (-Only rag-embed-query) can split them, and nothing downstream would notice: /readyz
# checks the query pool only, and the worker checks the ingest pool only, once, at startup.
if ($useTei -and (Test-Path -LiteralPath $Config.ImagesPath)) {
    $imgManifest = Get-Content -LiteralPath $Config.ImagesPath -Raw | ConvertFrom-Json -AsHashtable
    # With GPU off both pools run the CPU image, so there is nothing to compare.
    $embedRepos = $useGpu ? @('rag-embedder-cpu', 'rag-embedder-turing') : @('rag-embedder-cpu')
    $seen = [ordered]@{}
    foreach ($repo in $embedRepos) {
        $id = Get-Value $imgManifest "images.$repo.modelId"
        $rev = Get-Value $imgManifest "images.$repo.modelRevision"
        if ($id) { $seen[$repo] = "$id@$rev" }
    }
    $distinct = @($seen.Values | Sort-Object -Unique)
    if ($distinct.Count -gt 1) {
        foreach ($repo in $seen.Keys) { Write-Fail "$repo was built from $($seen[$repo])" }
        throw ('The embedder images were built from different models, so the query and ingestion pools would ' +
            'embed into different vector spaces and every query would silently return the wrong passages. ' +
            "Rebuild both: ./infra/scripts/06-registry-build.ps1 -Env $($Config.EnvName)")
    }
    $expected = "$($Config.EmbedderModelId)@$($Config.EmbedderModelRevision)"
    if ($distinct.Count -eq 1 -and $distinct[0] -ne $expected) {
        Write-Warn "Embedder images were built from $($distinct[0]), but the psd1 now declares $expected."
        Write-Info "Rebuild them to match: ./infra/scripts/06-registry-build.ps1 -Env $($Config.EnvName)"
    }
}

# ============================================================================================== tokens
$envId = $cae.id
$appEnv = [ordered]@{
    APP_ENV                = $Config.AppEnv
    LOG_LEVEL              = $Config.LogLevel
    AZURE_CLIENT_ID        = $o.identityClientId     # DefaultAzureCredential -> the user-assigned identity
    KEY_VAULT_URL          = $o.keyVaultUri
    CONFIG_STORE           = 'blob'
    CONFIG_CONTAINER       = 'config'
    BLOB_ACCOUNT_URL       = $o.blobEndpoint
    RAW_STORE              = 'blob'
    RAW_CONTAINER          = 'raw-docs'
    EXPORTS_CONTAINER      = 'exports'
    STATE_DB_URL           = $o.stateDbUrl
    PG_ENTRA_AUTH          = 'true'
    QUEUE                  = 'servicebus'
    SERVICEBUS_NAMESPACE   = $o.serviceBusFqdn
    QUEUE_PRIORITY         = 'ingest-priority'
    QUEUE_BULK             = 'ingest-bulk'
    QUEUE_MAX_DELIVERY     = $Config.QueueMaxDeliveryCount
    SEARCH_BACKEND         = 'azure'
    SEARCH_ENDPOINT        = $o.searchEndpoint
    INDEX_DOMAIN           = $Config.IndexDomain
    EMBEDDING_PROFILE      = $Config.EmbeddingProfile
    AOAI_ENDPOINT          = $o.aoaiEndpoint
    LLM_ANSWER             = $Config.AnswerModelProvider
    LLM_UTILITY            = $Config.UtilityModelProvider
    CLAUDE_FOUNDRY_RESOURCE = $o.foundryName
    CLAUDE_MODEL           = $Config.ClaudeAnswerModelName
    CLAUDE_UTILITY_MODEL   = $Config.ClaudeUtilityModelName
    EMBED_ORIGINS          = $Config.EmbedOrigins
    DEV_AUTH_ENABLED       = [bool]$Config.DevAuthEnabled
    INGEST_MAX_CONCURRENCY = $Config.IngestMaxConcurrency
    OTEL_ENABLED           = 'true'
    OTEL_RESOURCE_ATTRIBUTES = "deployment.environment=$($Config.Env)"
}
if ($useTei) {
    $appEnv.TEI_QUERY_URL = 'http://rag-embed-query'
    $appEnv.TEI_INGEST_URL = 'http://rag-embed-ingest'
}
if ($Config.ActiveIndex) { $appEnv.ACTIVE_INDEX = $Config.ActiveIndex }
# Each of these is set only when step 05 actually produced it. Reading them straight off the outputs file used
# to inject a deployment name that 05 no longer manages after a provider or embedding flip.
$chatDeployment = Get-Value $o 'chatDeployment'
if ($chatDeployment) { $appEnv.AOAI_CHAT_DEPLOYMENT = $chatDeployment }
$embedDeployment = Get-Value $o 'embeddingDeployment'
if ($embedDeployment) { $appEnv.AOAI_EMBED_DEPLOYMENT = $embedDeployment }
# Only set when the utility role has a deployment of its own; unset means "reuse the answer deployment".
$utilityDeployment = Get-Value $o 'utilityDeployment'
if ($utilityDeployment -and $utilityDeployment -ne $chatDeployment) { $appEnv.AOAI_UTILITY_DEPLOYMENT = $utilityDeployment }
if ($Config.ClaudeEffort) { $appEnv.CLAUDE_EFFORT = $Config.ClaudeEffort }
if ($Config.EntraTenantId) { $appEnv.ENTRA_TENANT_ID = $Config.EntraTenantId }
if ($Config.EntraAudience) { $appEnv.ENTRA_AUDIENCE = $Config.EntraAudience }
if ($Config.EntraClientId) { $appEnv.ENTRA_CLIENT_ID = $Config.EntraClientId }
if ($Config.EntraApiScope) { $appEnv.ENTRA_API_SCOPE = $Config.EntraApiScope }
# Settings (Security) writes app-role assignments, which hang off the ENTERPRISE APPLICATION's object id - not the
# client id. Set-EntraAppRegistration.ps1 records it; passed through whenever it is known, so enabling DIRECTORY
# later needs no second trip through this step. -AllowMissing: a deployment that does not administer people has no
# reason to have run that script's later half.
# $o rather than Get-Output, because this one is genuinely optional: a deployment that does not administer
# people has no reason to have it, and Get-Output throws on a missing value by design.
if ($o.ContainsKey('entraServicePrincipalObjectId') -and $o.entraServicePrincipalObjectId) {
    $appEnv.ENTRA_SERVICE_PRINCIPAL_OBJECT_ID = $o.entraServicePrincipalObjectId
}
if (-not $Config.EntraTenantId -and -not $Config.DevAuthEnabled) {
    Write-Warn 'No Entra settings and DevAuthEnabled is $false: nobody will be able to sign in. See Deployment.md section 9 - Signing people in with Microsoft Entra ID.'
}
$reserved = @('SERVICE_NAME', 'OTEL_SERVICE_NAME', 'DEV_JWT_KEY', 'APPLICATIONINSIGHTS_CONNECTION_STRING', 'AOAI_API_KEY',
    'CLAUDE_API_KEY', 'BLOB_CONNECTION_STRING')
foreach ($key in $Config.ExtraAppSettings.Keys) {
    $name = ([string]$key).ToUpperInvariant()
    if ($name -in $reserved -or $name -match '(KEY|SECRET|PASSWORD|CONNECTION_STRING|TOKEN)$') {
        throw "ExtraAppSettings.$key looks like a secret or is reserved. Store secrets in Key Vault and reference them (Deployment.md section 4 - Key Vault secrets)."
    }
    $appEnv[$name] = $Config.ExtraAppSettings[$key]
}
# The embedding equivalent of the Claude check below, and stricter: without AOAI_EMBED_DEPLOYMENT the apps come
# up healthy and then fail on their FIRST embedding call, which is a query or an ingested document rather than
# anything this script would notice. The Claude path can warn because a missing chat deployment surfaces on the
# next question; this one has to stop, because the alternative is a deployment that looks finished and is not.
if (-not $useTei -and -not (Get-Value $o 'embeddingDeployment')) {
    throw ("EmbeddingProfile '$($Config.EmbeddingProfile)' uses provider '$embedProvider', but step 05 recorded " +
        "no embedding deployment, so AOAI_EMBED_DEPLOYMENT cannot be set and every embedding call would fail. " +
        "Set DeployAoaiEmbedding = `$true and re-run: ./infra/scripts/05-foundry.ps1 -Env $($Config.EnvName)")
}
if (($Config.AnswerModelProvider -eq 'claude' -or $Config.UtilityModelProvider -eq 'claude') -and -not (Get-Value $o 'claudeDeployed')) {
    Write-Warn "A model role uses Claude, but step 05 did not deploy it. Check the deployment exists in the Foundry portal."
    Write-Info "Expected deployment name(s): $(@($Config.AnswerModelProvider -eq 'claude' ? $Config.ClaudeAnswerModelName : $null, $Config.UtilityModelProvider -eq 'claude' ? $Config.ClaudeUtilityModelName : $null | Where-Object { $_ }) -join ', ')"
}
$commonEnv = (@($appEnv.Keys | ForEach-Object { "- name: $_"; "  value: $(ConvertTo-YamlString $appEnv[$_])" }) -join "`n")
$tagsBlock = (@($Config.AllTags.Keys | ForEach-Object { "$($_): $(ConvertTo-YamlString $Config.AllTags[$_])" }) -join "`n")

$tokens = @{
    LOCATION                      = $loc
    ENV_ID                        = $envId
    MI_ID                         = $o.identityId
    ACR_LOGIN_SERVER              = $o.acrLoginServer
    KEY_VAULT_URI                 = $o.keyVaultUri
    TAGS                          = $tagsBlock
    COMMON_ENV                    = $commonEnv
    IMAGE_API                     = $imageRefs['rag-api']
    IMAGE_CHAT_UI                 = $imageRefs['rag-chat-ui']
    IMAGE_EMBED_QUERY             = $imageRefs['rag-embedder-cpu']
    IMAGE_EMBED_INGEST            = ($useGpu ? $imageRefs['rag-embedder-turing'] : $imageRefs['rag-embedder-cpu'])
    EMBED_INGEST_PROFILE          = ($useGpu ? 'gpu-t4' : 'ingest')
    EMBED_INGEST_CPU              = ($useGpu ? '8.0' : '4.0')      # serverless T4 replicas get the whole GPU node size
    EMBED_INGEST_MEMORY           = ($useGpu ? '56Gi' : '8Gi')
    EMBED_INGEST_THREADS          = ($useGpu ? '8' : '4')
    EMBED_INGEST_MAX_BATCH_TOKENS = ($useGpu ? $Config.EmbedderMaxBatchTokensGpu : $Config.EmbedderMaxBatchTokensCpu)
    EMBED_INGEST_MIN_REPLICAS     = $Config.GpuMinReplicas
    EMBED_INGEST_MAX_REPLICAS     = ($useGpu ? $Config.GpuMaxReplicas : $Config.EmbedIngestCpuMaxReplicas)
    EMBED_INGEST_CONCURRENCY      = $Config.EmbedIngestConcurrency
    EMBEDDER_MAX_BATCH_TOKENS_CPU = $Config.EmbedderMaxBatchTokensCpu
    EMBEDDER_MAX_INPUT_TOKENS     = $Config.EmbedderMaxInputTokens
    EMBED_QUERY_MIN_REPLICAS      = $Config.EmbedQueryMinReplicas
    EMBED_QUERY_MAX_REPLICAS      = $Config.EmbedQueryMaxReplicas
    EMBED_QUERY_CONCURRENCY       = $Config.EmbedQueryConcurrency
    API_MIN_REPLICAS              = $Config.ApiMinReplicas
    API_MAX_REPLICAS              = $Config.ApiMaxReplicas
    API_CONCURRENCY               = $Config.ApiConcurrency
    CHAT_UI_MIN_REPLICAS          = $Config.ChatUiMinReplicas
    CHAT_UI_MAX_REPLICAS          = $Config.ChatUiMaxReplicas
    EMBED_ORIGINS_YAML            = (ConvertTo-YamlString $Config.EmbedOrigins)
    DEV_AUTH_ENABLED              = [bool]$Config.DevAuthEnabled
    APP_ENV                       = $Config.AppEnv
    WORKER_CPU                    = $Config.WorkerCpu
    WORKER_MEMORY                 = $Config.WorkerMemory
    WORKER_MAX_REPLICAS           = $Config.WorkerMaxReplicas
    WORKER_PRIORITY_MESSAGE_COUNT = $Config.WorkerPriorityMessageCount
    WORKER_BULK_MESSAGE_COUNT     = $Config.WorkerBulkMessageCount
    SERVICEBUS_NAMESPACE_NAME     = $o.serviceBusName
    SCHEDULER_CRON                = $Config.SchedulerCron
}

# ============================================================================================== deploy
$renderDir = Join-Path ([IO.Path]::GetTempPath()) "rag-os-$($Config.EnvName)-containerapps"
$null = New-Item -ItemType Directory -Force -Path $renderDir
Write-Info "Rendered YAML: $renderDir"

$workloads = @(
    @{ Name = 'rag-api'; Job = $false }
    @{ Name = 'rag-ingest-worker'; Job = $false }
    @{ Name = 'rag-chat-ui'; Job = $false }
    @{ Name = 'rag-scheduler'; Job = $true }
    @{ Name = 'rag-bootstrap'; Job = $true }
)
if ($useTei) { $workloads = @(@{ Name = 'rag-embed-query'; Job = $false }, @{ Name = 'rag-embed-ingest'; Job = $false }) + $workloads }
else {
    Write-Warn "EmbeddingProfile '$($Config.EmbeddingProfile)' uses provider '$embedProvider': the TEI pools are not deployed."
    # Collected into one block rather than announced as it goes. Switching to a remote embedder leaves several
    # things behind, each cheap to miss and all of them still billing; scattered across two sections of output
    # they read as commentary rather than as a list of things to do.
    # Nothing here deletes anything: a provisioning script must not remove a running app on its own.
    $leftovers = [System.Collections.Generic.List[string]]::new()
    foreach ($app in @('rag-embed-query', 'rag-embed-ingest')) {
        if (Test-AzResource @('containerapp', 'show', '-g', $rg, '-n', $app, '--query', 'id', '-o', 'tsv')) {
            $leftovers.Add("az containerapp delete -g $rg -n $app --yes")
        }
    }
    # Keyed off EnableGpu, not the provider, so the branch above reports an attached gpu-t4 profile as a success
    # when the profile is remote - the one case where nothing will ever use it again.
    if ($Config.EnableGpu -and $profiles.ContainsKey('gpu-t4')) {
        $leftovers.Add("az containerapp env workload-profile delete -g $rg -n $($n.ContainerEnv) --workload-profile-name gpu-t4")
    }
    if ($leftovers.Count -gt 0) {
        Write-Warn "$($leftovers.Count) resource(s) from the self-hosted embedder are still deployed and still billing:"
        foreach ($cmd in $leftovers) { Write-Host "      $cmd" -ForegroundColor Yellow }
    }
    Write-Info 'Then, in the psd1 - these are the ones that actually change the bill:'
    if ($Config.EnableGpu) { Write-Info '  EnableGpu     = $false   # nothing uses the T4 profile once the pools are gone' }
    if ([int]$Config.QueryMinNodes -gt 1) {
        Write-Info "  QueryMinNodes = 1        # currently $($Config.QueryMinNodes). The query profile was sized for"
        Write-Info '                           # api(2) + chat-ui(1) + embed-query(2) = 5 vCPU; without the embedder'
        Write-Info '                           # it is 3 vCPU, so one node is enough.'
    }
    Write-Info "The embedder images stay in ACR and count against its size: az acr repository delete -n $($n.Registry) --repository rag-embedder-cpu"
}
foreach ($w in $workloads) {
    if ($Only -and $w.Name -notin $Only) { continue }
    Write-Step "$($w.Job ? 'Job' : 'App') $($w.Name)"
    $yamlPath = Join-Path $renderDir "$($w.Name).yaml"
    Expand-Template -Path (Join-Path $templatesDir "$($w.Name).yaml.tmpl") -Tokens $tokens | Set-Content -LiteralPath $yamlPath -Encoding utf8NoBOM
    $group = $w.Job ? @('containerapp', 'job') : @('containerapp')
    $exists = Test-AzResource ($group + @('show', '-g', $rg, '-n', $w.Name, '--query', 'id', '-o', 'tsv'))
    $verb = $exists ? 'update' : 'create'
    # Retries cover role propagation (AcrPull, Key Vault Secrets User) right after 02/06 ran.
    Invoke-WithRetry -Activity "$verb $($w.Name)" -MaxAttempts 6 -DelaySeconds 30 `
        -RetryOn '(?i)(forbidden|unauthori[sz]ed|denied|AcrPull|keyvault|secret|identity|MANIFEST_UNKNOWN|could not be pulled|Operation expired|conflict|in progress)' -ScriptBlock {
        $null = Invoke-Az ($group + @($verb, '-g', $rg, '-n', $w.Name, '--yaml', $yamlPath, '-o', 'none'))
    }
    Write-Ok "$($w.Name) ($($verb)d)"
}

# ============================================================================================== outputs
$chatFqdn = Invoke-Az @('containerapp', 'show', '-g', $rg, '-n', 'rag-chat-ui', '--query', 'properties.configuration.ingress.fqdn', '-o', 'tsv') -AllowNotFound
Save-Outputs -Config $Config -Values @{
    containerEnvName   = $n.ContainerEnv
    containerEnvId     = $envId
    containerEnvDomain = Get-Value $cae 'properties.defaultDomain'
    gpuEnabled         = $useGpu
    chatUiFqdn         = $chatFqdn
    deployedImages     = $imageRefs
}
Write-Ok 'Container Apps deployed.'
if ($chatFqdn) { Write-Info "Chat UI: https://$chatFqdn" }
Write-Info "Verify: az containerapp list -g $rg --query '[].{name:name, state:properties.runningStatus, fqdn:properties.configuration.ingress.fqdn}' -o table"
Write-Info "        az containerapp revision list -g $rg -n rag-api --query '[].{rev:name, active:properties.active, health:properties.healthState, replicas:properties.replicas}' -o table"
