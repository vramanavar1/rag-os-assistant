#Requires -Version 7.3

<#
.SYNOPSIS
    Checks that the vectors in the index and the vectors a query is embedded into come from the same model.
.DESCRIPTION
    This is the one failure in a RAG system that does not announce itself. Search a 1024-dimension index with a
    1024-dimension vector from a DIFFERENT model and nothing errors: the similarity scores are meaningless, so
    the top hits are arbitrary passages returned with full confidence. It reads as "the answers got worse",
    which is indistinguishable from a hundred other things, and it survives every health check in the system.

    Four independent things have to line up, and each can drift on its own:

      1. The running API's index and fingerprint  - what is actually being queried right now.
      2. EMBEDDING_PROFILE on rag-api AND rag-ingest-worker - a -Only redeploy can update one and not the other.
      3. Both TEI pools' running images          - 07 compares the build manifest, not what is deployed, and
                                                   skips the comparison entirely when EnableGpu is false.
      4. The psd1 against profiles.yaml          - the psd1 calls its own dimensions value "informational", so
                                                   the two can disagree with nothing to notice.

    Every check is read-only. Exit code 0 when everything lines up, 1 otherwise, set on every path so a caller
    can test $LASTEXITCODE. Step 09 runs this before the smoke tests, because a mismatch makes the smoke test's
    ingestion check fail by TIMEOUT - four minutes later, reading like a broken worker.
.EXAMPLE
    ./infra/scripts/Test-EmbeddingAlignment.ps1 -Env dev
    ./infra/scripts/Test-EmbeddingAlignment.ps1 -Env dev -SkipLive   # config only; no running deployment needed
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    # Skip the checks that need a running deployment (1-3). Useful before 07, or from a machine with no access.
    [switch]$SkipLive
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title 'embedding alignment'
$rg = $Config.Names.ResourceGroup

$results = [System.Collections.Generic.List[object]]::new()
function Add-Result([string]$Check, [bool]$Ok, [string]$Detail, [switch]$Advisory) {
    $results.Add([pscustomobject]@{ Check = $Check; Ok = $Ok; Detail = $Detail; Advisory = [bool]$Advisory })
    $label = if ($Ok) { 'ok' } elseif ($Advisory) { 'warn' } else { 'FAIL' }
    $colour = if ($Ok) { 'Green' } elseif ($Advisory) { 'Yellow' } else { 'Red' }
    Write-Host ("    [{0}] {1,-46} {2}" -f $label, $Check, $Detail) -ForegroundColor $colour
}

# --------------------------------------------------------------------------- 4. the two config files agree
# First, because it needs nothing deployed and because a disagreement here means every other check is
# comparing against a value that was already wrong.
Write-Step 'Declared configuration (psd1 vs config/embedding/profiles.yaml)'
$profileName = [string]$Config.EmbeddingProfile
$provider = Get-EmbeddingProfileProvider -Config $Config
Write-Info "profile: $profileName (provider $(if ($provider) { $provider } else { 'unknown' }))"

if ($provider -eq 'azure_openai') {
    # The self-hosted keys - EmbedderModelId, EmbedderModelRevision - describe an image that is not built and not
    # deployed for a remote profile, so comparing the profile against them reports a mismatch that means nothing.
    # This check originally did exactly that, and step 09 therefore failed on every correct remote configuration.
    # What has to line up instead is the model the profile asks for against the model step 05 deploys, and the
    # deployment name the apps were actually given.
    Write-Info 'Remote provider: EmbedderModelId / EmbedderModelRevision describe the TEI image and do not apply.'

    $yamlModel = Get-EmbeddingProfileField -Config $Config -Name 'model'
    if ($null -eq $yamlModel) { Add-Result 'model' $true 'profiles.yaml did not state it - not compared' -Advisory }
    else {
        $deployed = "$($Config.EmbeddingModelName)"
        Add-Result 'model' ($yamlModel -eq $deployed) $(
            if ($yamlModel -eq $deployed) { $yamlModel }
            else { "profiles.yaml '$yamlModel' != EmbeddingModelName '$deployed'" })
    }

    # Deploying the model and selecting it are two different settings, and only one of them is in the profile.
    Add-Result 'DeployAoaiEmbedding' ([bool]$Config.DeployAoaiEmbedding) $(
        if ($Config.DeployAoaiEmbedding) { 'true' }
        else { 'false - step 05 creates no deployment, so AOAI_EMBED_DEPLOYMENT is never set' })

    # Recorded by step 05. Without it step 07 silently omits AOAI_EMBED_DEPLOYMENT and the apps fail on their
    # first embedding call, which is a long way from here.
    $recorded = Get-Value (Get-Outputs -Config $Config) 'embeddingDeployment'
    Add-Result 'embedding deployment recorded by 05' ([bool]$recorded) $(
        if ($recorded) { $recorded } else { 'absent from outputs.json - re-run 05-foundry.ps1' })

    # Advisory: nothing reads EmbeddingDimensions at runtime (the psd1 calls it informational and the profile is
    # authoritative), but leaving it on the old value makes every other document here read as if it were wrong.
    $yamlDims = Get-EmbeddingProfileField -Config $Config -Name 'dimensions'
    if ($yamlDims -and "$($Config.EmbeddingDimensions)" -ne $yamlDims) {
        Add-Result 'dimensions' $true (
            "psd1 says $($Config.EmbeddingDimensions), the profile says $yamlDims - informational only, " +
            'but worth aligning') -Advisory
    }
    elseif ($yamlDims) { Add-Result 'dimensions' $true $yamlDims }
}
else {
    foreach ($pair in @(
            @{ Field = 'model'; Psd1 = 'EmbedderModelId' }
            @{ Field = 'model_revision'; Psd1 = 'EmbedderModelRevision' }
            @{ Field = 'dimensions'; Psd1 = 'EmbeddingDimensions' }
        )) {
        $yaml = Get-EmbeddingProfileField -Config $Config -Name $pair.Field
        $psd1 = "$($Config[$pair.Psd1])"
        if ($null -eq $yaml) {
            # The documented contract of the profile readers: unknown is not a failure. An unreadable config file
            # must never be the thing that blocks a deployment.
            Add-Result "$($pair.Field)" $true 'profiles.yaml did not state it - not compared' -Advisory
            continue
        }
        Add-Result "$($pair.Field)" ($yaml -eq $psd1) $(
            if ($yaml -eq $psd1) { $yaml } else { "psd1 '$psd1' != profiles.yaml '$yaml'" })
    }
}

if (-not $SkipLive) {
    # ------------------------------------------------------------------ 2. what the apps were actually given
    # rag-api embeds the queries; rag-ingest-worker embeds the documents. They read the same variable, and 07
    # renders it into both from one block - but only for the apps that run. A targeted redeploy updates one.
    Write-Step 'Deployed EMBEDDING_PROFILE'
    foreach ($app in @('rag-api', 'rag-ingest-worker')) {
        try {
            $deployed = Invoke-Az @('containerapp', 'show', '-g', $rg, '-n', $app, '--query',
                "properties.template.containers[0].env[?name=='EMBEDDING_PROFILE'].value | [0]", '-o', 'tsv') -AllowNotFound
            if (-not $deployed) { Add-Result "$app EMBEDDING_PROFILE" $false 'not set on the running revision' }
            else { Add-Result "$app EMBEDDING_PROFILE" ($deployed -eq $profileName) $(
                    if ($deployed -eq $profileName) { $deployed } else { "running '$deployed' != psd1 '$profileName'" }) }
        }
        catch { Add-Result "$app EMBEDDING_PROFILE" $false ($_.Exception.Message.Split("`n")[0]) }
    }

    # ------------------------------------------------------- 3. the two pools are running the same model
    # The model is baked into the image, not configured at runtime, so the only way to ask a running pool what
    # it serves is to map its image back to the manifest entry that recorded how it was built.
    if ($provider -in @('tei', $null)) {
        Write-Step 'TEI pools (running image -> the model it was built from)'
        $manifest = $null
        if (Test-Path -LiteralPath $Config.ImagesPath) {
            $manifest = Get-Content -LiteralPath $Config.ImagesPath -Raw | ConvertFrom-Json
        }
        $built = @{}
        foreach ($app in @('rag-embed-query', 'rag-embed-ingest')) {
            try {
                $image = Invoke-Az @('containerapp', 'show', '-g', $rg, '-n', $app, '--query',
                    'properties.template.containers[0].image', '-o', 'tsv') -AllowNotFound
                if (-not $image) { Add-Result "$app image" $false 'app not found or has no container'; continue }
                # The manifest is keyed by repository, and the image reference contains it.
                $repo = @('rag-embedder-turing', 'rag-embedder-cpu') | Where-Object { $image -like "*/$_@*" -or $image -like "*/${_}:*" } | Select-Object -First 1
                if (-not $repo) { Add-Result "$app image" $true "$image (not a known embedder repository)" -Advisory; continue }
                $model = if ($manifest) { Get-Value $manifest "images.$repo.modelId" } else { $null }
                $revision = if ($manifest) { Get-Value $manifest "images.$repo.modelRevision" } else { $null }
                if (-not $model) {
                    Add-Result "$app image" $true "$repo (the manifest did not record its model)" -Advisory
                    continue
                }
                $built[$app] = "$model@$revision"
                $expected = "$($Config.EmbedderModelId)@$($Config.EmbedderModelRevision)"
                Add-Result "$app model" ($built[$app] -eq $expected) $(
                    if ($built[$app] -eq $expected) { $built[$app] } else { "running $($built[$app]) != psd1 $expected" })
            }
            catch { Add-Result "$app image" $false ($_.Exception.Message.Split("`n")[0]) }
        }
        # The comparison that matters most, and the one nothing else makes: the two pools against EACH OTHER.
        # Both can disagree with the psd1 and still be consistent, which is survivable; disagreeing with each
        # other is not, because then documents and queries land in different spaces.
        if ($built.Count -eq 2) {
            $same = $built['rag-embed-query'] -eq $built['rag-embed-ingest']
            Add-Result 'both pools serve the same model' $same $(
                if ($same) { $built['rag-embed-query'] }
                else { "query $($built['rag-embed-query']) vs ingest $($built['rag-embed-ingest'])" })
        }
    }

    # ------------------------------------------------------------------ 1. what the running API is using
    # Last, because it is the slowest and because the checks above explain most of its failures.
    Write-Step 'Running API (/api/readyz)'
    $baseUrl = Get-ChatUiUrl -Config $Config
    try {
        # 40s: above readyz's own 32s worst case, or a slow-but-working answer is cut off and reads as a fault.
        $r = Invoke-WebRequest -Uri "$baseUrl/api/readyz" -TimeoutSec 40 -SkipHttpErrorCheck
        $body = $r.Content | ConvertFrom-Json
        $live = $body.checks.embedding_profile
        $expectedIndex = if ($Config.ActiveIndex) { [string]$Config.ActiveIndex } else { "kb-$($Config.IndexDomain)-" }
        $indexOk = if ($Config.ActiveIndex) { $live.index -eq $expectedIndex } else { "$($live.index)".StartsWith($expectedIndex) }
        Add-Result 'index being queried' ([bool]$indexOk) $(
            if ($indexOk) { "$($live.index) (profile $($live.fingerprint))" }
            else { "$($live.index) does not match the configured $expectedIndex" })
        Add-Result 'embedding profile guard' ([bool]$live.ok) $(
            if ($live.ok) { 'both pools agree with the profile' }
            else { @(Format-ReadyzReasons -Content $r.Content -StatusCode $r.StatusCode) -join '; ' })
        foreach ($note in @($live.notes)) { if ($note) { Write-Info "  note: $note" } }

        # What bootstrap created, if 08 recorded it. A live index that is not the one bootstrap stamped means
        # the profile changed since - and the new index is empty, which no amount of querying will reveal.
        $stamped = Get-Value (Get-Outputs -Config $Config) 'embeddingIndex'
        if ($stamped) {
            Add-Result 'index matches the one bootstrap created' ($stamped -eq $live.index) $(
                if ($stamped -eq $live.index) { $stamped } else { "bootstrap created $stamped, the API is querying $($live.index)" })
        }
    }
    catch { Add-Result 'running API (/api/readyz)' $false ($_.Exception.Message.Split("`n")[0]) }
}

# --------------------------------------------------------------------------------------------- verdict
Write-Step 'Result'
$failed = @($results | Where-Object { -not $_.Ok -and -not $_.Advisory })
$warned = @($results | Where-Object { -not $_.Ok -and $_.Advisory })
foreach ($w in $warned) { Write-Warn "$($w.Check): $($w.Detail)" }
if ($failed.Count -gt 0) {
    Write-Fail "$($failed.Count) check(s) failed: $(($failed.Check) -join ', ')"
    Write-Info '  Documents and queries may be embedded by different models. Searching an index with vectors'
    Write-Info '  from another model does not error - it returns arbitrary passages, confidently. Fix this'
    Write-Info '  before trusting any answer, and re-ingest anything indexed while it was wrong:'
    if ($provider -eq 'azure_openai') {
        # No images to rebuild and no pools to redeploy: the model is a deployment on the Foundry account.
        Write-Info "    ./infra/scripts/05-foundry.ps1 -Env $Env        # creates the deployment and records it"
        Write-Info "    ./infra/scripts/07-container-apps.ps1 -Env $Env  # injects AOAI_EMBED_DEPLOYMENT into the apps"
    }
    else {
        Write-Info "    ./infra/scripts/06-registry-build.ps1 -Env $Env    # rebuild BOTH embedder images together"
        Write-Info "    ./infra/scripts/07-container-apps.ps1 -Env $Env    # redeploy both pools"
    }
    Write-Info '    rag-os doctor                                       # from the rag-api console, for detail'
    exit 1
}
Write-Ok 'The index, the query path and the ingestion path all use the same embedding model.'
exit 0
