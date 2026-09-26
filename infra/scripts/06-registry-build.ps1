#Requires -Version 7.3
<#
.SYNOPSIS
    Step 06 - Azure Container Registry (admin disabled) + AcrPull for the managed identity + cloud builds (az acr build).
.DESCRIPTION
    Images (tag = git short SHA, '-dirty-<timestamp>' for uncommitted changes, or -Tag / ImageTag in the psd1):
      rag-api:<tag>              repo-root Dockerfile (API, worker, scheduler and bootstrap all use this image)
      rag-chat-ui:<tag>          chat-ui/Dockerfile (context chat-ui/)
      rag-embedder-cpu:<tag>     embedder/Dockerfile with the TEI cpu base image
      rag-embedder-turing:<tag>  embedder/Dockerfile with the TEI turing (T4) base image
    No local Docker is needed. Digests are written to infra/env/<env>.images.json (07 deploys by digest; the embedding
    profile's server image is recorded there too).
.EXAMPLE
    ./infra/scripts/06-registry-build.ps1 -Env dev
    ./infra/scripts/06-registry-build.ps1 -Env dev -Images api,chat-ui          # rebuild only the app images
    ./infra/scripts/06-registry-build.ps1 -Env dev -Tag 3f2a1bc -SkipBuild      # just record digests of an existing tag
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [string]$Tag,
    [ValidateSet('api', 'chat-ui', 'embedder-cpu', 'embedder-turing')][string[]]$Images,
    [switch]$SkipBuild,
    # Rebuild even when the tag is already in the registry (source changed without the tag changing).
    [switch]$Rebuild
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '06 registry + image builds'

# The embedder images exist to serve a self-hosted profile. A remote profile never calls them, so building two
# multi-GB images (up to 2h each, into a 10GB Basic ACR) would be pure waste. An explicit -Images still wins.
if (-not $Images) {
    $Images = @('api', 'chat-ui', 'embedder-cpu', 'embedder-turing')
    $embedProvider = Get-EmbeddingProfileProvider -Config $Config
    if ($embedProvider -and $embedProvider -ne 'tei') {
        $Images = @('api', 'chat-ui')
        Write-Info "EmbeddingProfile '$($Config.EmbeddingProfile)' uses provider '$embedProvider': skipping the embedder images."
        Write-Info 'Build them anyway with -Images embedder-cpu,embedder-turing.'
    }
}
$n = $Config.Names
$rg = $n.ResourceGroup
$tags = Get-TagArgs -Config $Config
$repoRoot = $Config.RepoRoot
$miName = Get-Output -Config $Config -Name 'identityName' -ProducedBy '02-identity-keyvault.ps1'
$miPrincipalId = Get-Output -Config $Config -Name 'identityPrincipalId' -ProducedBy '02-identity-keyvault.ps1'

Write-Step "Container registry $($n.Registry)"
$acr = Ensure-AzResource -Description "registry $($n.Registry)" -Config $Config -SyncTags `
    -Show @('acr', 'show', '-g', $rg, '-n', $n.Registry) `
    -Create (@('acr', 'create', '-g', $rg, '-n', $n.Registry, '-l', $Config.Location, '--sku', $Config.AcrSku, '--admin-enabled', 'false') + $tags) `
    -Update @('acr', 'update', '-g', $rg, '-n', $n.Registry) -Desired @(
    (New-DesiredProperty -Path 'adminUserEnabled' -Desired $false -Arg '--admin-enabled' -Label 'admin user'),
    # Basic caps at 10 GB, and repeated builds fill it. Changing tier is safe and takes effect immediately.
    (New-DesiredProperty -Path 'sku.name' -Desired $Config.AcrSku -Arg '--sku' -Label 'sku')
)
Grant-Role -PrincipalId $miPrincipalId -PrincipalType 'ServicePrincipal' -Role 'AcrPull' -Scope $acr.id -PrincipalLabel $miName
$loginServer = $acr.loginServer

# A full registry is a slow way to fail: the push happens at the end, after the build time is already spent.
# One read up front turns that into a warning before the wait. Limits are per SKU, from the ACR service tiers.
$acrLimitGb = @{ Basic = 10; Standard = 100; Premium = 500 }[$Config.AcrSku]
if ($acrLimitGb) {
    $usedBytes = Get-AzTsvValues @('acr', 'show-usage', '-n', $n.Registry, '--query', "value[?name=='Size'].currentValue | [0]")
    if ($usedBytes) {
        $usedGb = [math]::Round(([double]($usedBytes | Select-Object -First 1)) / 1GB, 1)
        Write-Info "Registry usage: $usedGb GB of $acrLimitGb GB ($($Config.AcrSku))"
        # The embedder images are the large ones - a CUDA base plus a baked-in model - so they are what actually
        # decides whether this fits.
        $needGb = if ($Images -match 'embedder') { 12 } else { 2 }
        if (($usedGb + $needGb) -gt $acrLimitGb) {
            Write-Warn "This build needs roughly ${needGb} GB more and the $($Config.AcrSku) registry holds $acrLimitGb GB - it will probably run out."
            Write-Info '  The push happens after the build, so you would find out at the end of a long wait. Either:'
            Write-Info "    az acr repository list -n $($n.Registry) -o tsv    then delete what you no longer need"
            Write-Info "    or set AcrSku = 'Standard' (100 GB) in the psd1 and re-run step 06"
        }
    }
}

if (-not $Tag) { $Tag = $Config.ImageTag }
if (-not $Tag) { $Tag = Get-ImageTag -RepoRoot $repoRoot }
Write-Info "Image tag: $Tag"

$teiCpu = if ($Config.TeiCpuImage) { $Config.TeiCpuImage } else { "ghcr.io/huggingface/text-embeddings-inference:cpu-$($Config.TeiVersion)" }
$teiTuring = if ($Config.TeiTuringImage) { $Config.TeiTuringImage } else { "ghcr.io/huggingface/text-embeddings-inference:turing-$($Config.TeiVersion)" }
function New-EmbedderArgs([string]$TeiImage) {
    return [ordered]@{ TEI_IMAGE = $TeiImage; MODEL_ID = $Config.EmbedderModelId; MODEL_REVISION = $Config.EmbedderModelRevision }
}
$builds = [ordered]@{
    'api'             = @{ Repo = 'rag-api'; Context = $repoRoot; File = 'Dockerfile'; Args = [ordered]@{}; Timeout = 3600 }
    'chat-ui'         = @{ Repo = 'rag-chat-ui'; Context = (Join-Path $repoRoot 'chat-ui'); File = 'Dockerfile'; Args = [ordered]@{}; Timeout = 3600 }
    'embedder-cpu'    = @{ Repo = 'rag-embedder-cpu'; Context = (Join-Path $repoRoot 'embedder'); File = 'Dockerfile'; Args = (New-EmbedderArgs $teiCpu); Timeout = 7200 }
    'embedder-turing' = @{ Repo = 'rag-embedder-turing'; Context = (Join-Path $repoRoot 'embedder'); File = 'Dockerfile'; Args = (New-EmbedderArgs $teiTuring); Timeout = 7200 }
}

# ---------------------------------------------------------------------------------------------- builds
foreach ($key in $Images) {
    $b = $builds[$key]
    $image = "$($b.Repo):$Tag"
    Write-Step "Image $image"
    if ($SkipBuild) { Write-Info 'Skipping build (-SkipBuild)'; continue }
    # Building a tag the registry already holds is pure waste - up to two hours for an embedder image - and it is
    # not harmless: every rebuild produces a new digest, and 07 rolls a new revision for every app whose digest
    # changed. A 'just redeploy' run used to cost a full rebuild and a full platform restart.
    if (-not $Rebuild) {
        $have = Invoke-Az @('acr', 'repository', 'show', '-n', $n.Registry, '--image', $image, '--query', 'digest', '-o', 'tsv') -AllowNotFound
        if ($have) {
            Write-Ok "$image (exists: $have) - not rebuilt. Pass -Rebuild to force."
            continue
        }
    }
    $dockerfile = Join-Path $b.Context $b.File
    if (-not (Test-Path -LiteralPath $dockerfile)) { throw "Dockerfile not found: $dockerfile" }
    if ($key -in @('api', 'chat-ui') -and -not (Test-Path -LiteralPath (Join-Path $b.Context '.dockerignore'))) {
        Write-Warn "No .dockerignore in $($b.Context): az acr build uploads the whole folder (.venv, node_modules, .git)."
        Write-Info 'That makes the build slow and can bake local files into the image. Add a .dockerignore and re-run.'
    }
    # -f must carry the context, not just the file name. az documents it as "relative to the source code root
    # folder", but it does not join it to the context: when -f is supplied it is used verbatim, checked against
    # the CURRENT WORKING DIRECTORY, and then force-added into the uploaded tar as the Dockerfile to build with
    # (acr/build.py -> _archive_utils._pack_source_code). Running from the repo root, a bare 'Dockerfile' meant
    # every image was built from the API's Dockerfile - chat-ui failed with 'stat pyproject.toml: file does not
    # exist' because it was running 19 API steps against the chat-ui context.
    $buildArgs = @('acr', 'build', '-r', $n.Registry, '-g', $rg, '-t', $image, '-f', $dockerfile, '--platform', 'linux', '--timeout', [string]$b.Timeout)
    foreach ($argName in $b.Args.Keys) { $buildArgs += @('--build-arg', "$argName=$($b.Args[$argName])") }
    $buildArgs += $b.Context
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Invoke-Az -Stream $buildArgs
    Write-Ok "$image built in $([int]$sw.Elapsed.TotalMinutes) min"
}

# ---------------------------------------------------------------------------------------------- digests -> <env>.images.json
Write-Step "Recording digests in $(Split-Path -Leaf $Config.ImagesPath)"
$manifest = if (Test-Path -LiteralPath $Config.ImagesPath) { Get-Content -LiteralPath $Config.ImagesPath -Raw | ConvertFrom-Json -AsHashtable } else { @{} }
if (-not $manifest.ContainsKey('images')) { $manifest.images = @{} }
foreach ($key in $Images) {
    $b = $builds[$key]
    # -AllowNotFound, or az's own not-found error is thrown first and the message below is unreachable -
    # which is exactly the '-SkipBuild -Tag <old>' path this script advertises.
    $digest = Invoke-Az @('acr', 'repository', 'show', '-n', $n.Registry, '--image', "$($b.Repo):$Tag", '--query', 'digest', '-o', 'tsv') -AllowNotFound
    if (-not $digest) { throw "No image $($b.Repo):$Tag in $($n.Registry). Build it (drop -SkipBuild) or pass a -Tag that exists." }
    $entry = [ordered]@{ tag = $Tag; digest = $digest; ref = "$loginServer/$($b.Repo)@$digest"; recordedAt = (Get-Date).ToUniversalTime().ToString('o') }
    # Embedder images bake the model in at build time. Recording which model per repo is what lets 07 catch a
    # partial rebuild that leaves the two pools on different weights.
    if ($b.Args.Contains('MODEL_ID')) {
        $entry.modelId = $b.Args['MODEL_ID']
        $entry.modelRevision = $b.Args['MODEL_REVISION']
    }
    $manifest.images[$b.Repo] = $entry
    Write-Ok "$($b.Repo):$Tag -> $digest"
}
$manifest.registry = $loginServer
$manifest.tag = $Tag
$embeddingImages = @{}
foreach ($repo in @('rag-embedder-cpu', 'rag-embedder-turing')) { if ($manifest.images.ContainsKey($repo)) { $embeddingImages[$repo] = $manifest.images[$repo].ref } }
# Recorded per provider. Written unconditionally this used to claim `profile: aoai-3-small-1536` beside
# `model: Qwen/...` and image digests for servers that were deliberately not built - metadata that contradicts
# itself, which Test-EmbeddingAlignment.ps1 then reads to decide whether the two pools agree.
$manifest.embedding = [ordered]@{ profile = $Config.EmbeddingProfile; provider = ($embedProvider ?? 'unknown') }
if ($embedProvider -in @('tei', $null)) {
    $manifest.embedding.model = $Config.EmbedderModelId
    $manifest.embedding.modelRevision = $Config.EmbedderModelRevision
    $manifest.embedding.dimensions = $Config.EmbeddingDimensions
    $manifest.embedding.teiVersion = $Config.TeiVersion
    $manifest.embedding.teiBaseImages = [ordered]@{ cpu = $teiCpu; turing = $teiTuring }
    $manifest.embedding.serverImages = $embeddingImages
}
else {
    # The model is the deployment's, not an image's. Nothing here is built.
    $manifest.embedding.model = $Config.EmbeddingModelName
    $manifest.embedding.deployment = $Config.EmbeddingDeploymentName
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Config.ImagesPath -Encoding utf8NoBOM

Save-Outputs -Config $Config -Values @{ acrName = $n.Registry; acrId = $acr.id; acrLoginServer = $loginServer; imageTag = $Tag }
Write-Ok "Images ready. Next: 07-container-apps.ps1 -Env $Env"
Write-Info "Verify: az acr repository show-tags -n $($n.Registry) --repository rag-api -o table"
