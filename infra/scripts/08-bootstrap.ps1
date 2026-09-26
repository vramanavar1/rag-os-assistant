#Requires -Version 7.3
<#
.SYNOPSIS
    Step 08 - upload config/** to the config blob container, run the rag-bootstrap job, start the scheduler, print the URLs.
.DESCRIPTION
    0. Runs Test-Connectivity.ps1 -Preflight first: the job talks to PostgreSQL, Search and Blob itself, so an
       unreachable one of those is a guaranteed 30-minute failure. -SkipConnectivityCheck bypasses it.
    1. Seeds the config container from config/** keeping relative paths (sources/sources.yaml,
       access-policy/access-policy.yaml, classification/facets.yaml, embedding/profiles.yaml, ...). Files that are
       already in the container are left alone - admins edit them through the API - unless -OverwriteConfig.
    2. Starts the rag-bootstrap job (database migrations, index from the embedding profile, policy + facets) and waits.
    3. Starts rag-scheduler once so the first discovery does not wait for the next cron tick.
    4. Waits for https://<chat-ui>/api/readyz and prints the URLs.
.EXAMPLE
    ./infra/scripts/08-bootstrap.ps1 -Env dev
    ./infra/scripts/08-bootstrap.ps1 -Env dev -SkipUpload -SkipScheduler
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [switch]$SkipUpload,
    # Replace config blobs that already exist with the repository copies. Without it they are left alone,
    # because the admin API writes those same blobs and its edits are the newer truth.
    [switch]$OverwriteConfig,
    [switch]$SkipScheduler,
    # The pre-flight only reads; skip it if a hop is known-bad and you want the job attempted regardless.
    [switch]$SkipConnectivityCheck,
    [int]$TimeoutMinutes = 30
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '08 bootstrap'
$rg = $Config.Names.ResourceGroup
$storage = Get-Output -Config $Config -Name 'storageName' -ProducedBy '03-data.ps1'

# ------------------------------------------------------------------------------------- connectivity pre-flight
# The bootstrap job reaches PostgreSQL, Search and Blob directly from its own container - not through rag-api. If
# one of those is unreachable the job still starts, still runs and still fails, up to 30 minutes later, with the
# cause buried in container logs. These checks read only and take seconds, so they run before anything is spent.
if (-not $SkipConnectivityCheck) {
    & (Join-Path $PSScriptRoot 'Test-Connectivity.ps1') -Env $Env -Preflight
    # The child's `exit` does not end this script - it sets $LASTEXITCODE, and Test-Connectivity sets it on every
    # path so this test cannot read a code left behind by some earlier az call.
    if ($LASTEXITCODE -ne 0) {
        throw ("Connectivity pre-flight failed - see the FAIL line(s) above. Fix the hop, or pass " +
            "-SkipConnectivityCheck to run the job anyway.")
    }
}

# ---------------------------------------------------------------------------------------------- config upload
if (-not $SkipUpload) {
    $configDir = Join-Path $Config.RepoRoot 'config'
    Write-Step "Uploading $configDir -> container 'config'"
    if (-not (Test-Path -LiteralPath $configDir)) {
        Write-Warn "No config/ folder at $configDir - skipping the upload. The bootstrap job will fall back to its"
        Write-Info "built-in defaults, so your sources, access policy and facets will NOT be applied. Run this step"
        Write-Info "again from the repository root once config/ is present."
    }
    else {
        # config/** seeds the container; after that the running app owns it. Admins edit sources, access policy
        # and facets through the admin API, which writes these very blobs (with an etag guard that a blanket
        # upload-batch bypasses). A blind --overwrite therefore reverted their work on every re-run, silently.
        # So: upload what is missing, and never replace what is already there unless asked.
        $present = @(Get-AzTsvValues @('storage', 'blob', 'list', '--account-name', $storage, '-c', 'config', '--auth-mode', 'login', '--query', '[].name'))
        $local = @(Get-ChildItem -LiteralPath $configDir -Recurse -File)
        $prefix = (Resolve-Path -LiteralPath $configDir).Path.TrimEnd('\', '/')
        $new = @()
        $kept = @()
        foreach ($file in $local) {
            $rel = $file.FullName.Substring($prefix.Length).TrimStart('\', '/').Replace('\', '/')
            if ($present -contains $rel) { $kept += $rel } else { $new += @{ Path = $file.FullName; Blob = $rel } }
        }
        foreach ($item in $new) {
            $null = Invoke-Az @('storage', 'blob', 'upload', '--account-name', $storage, '-c', 'config', '-n', $item.Blob,
                '-f', $item.Path, '--auth-mode', 'login', '--no-progress', '-o', 'none')
            Write-Ok "config/$($item.Blob) (uploaded)"
        }
        if ($kept.Count -gt 0 -and -not $OverwriteConfig) {
            Write-Info "$($kept.Count) config file(s) already in the container were left alone: $($kept -join ', ')"
            Write-Info 'Anything an admin changed through the UI lives there. Pass -OverwriteConfig to replace them with the repository copies.'
        }
        elseif ($kept.Count -gt 0) {
            Write-Warn "-OverwriteConfig: replacing $($kept.Count) existing config file(s) with the repository copies. Admin edits made through the UI will be lost."
            $null = Invoke-Az @('storage', 'blob', 'upload-batch', '--account-name', $storage, '--destination', 'config',
                '--source', $configDir, '--auth-mode', 'login', '--overwrite', 'true', '--no-progress', '-o', 'none')
            foreach ($rel in $kept) { Write-Ok "config/$rel (overwritten)" }
        }
        if ($new.Count -eq 0 -and $kept.Count -gt 0 -and -not $OverwriteConfig) { Write-Ok 'config container already seeded (nothing uploaded)' }
        Write-Info "List: az storage blob list --account-name $storage -c config --auth-mode login --query '[].name' -o tsv"
    }
}

# ---------------------------------------------------------------------------------------------- jobs
function Start-JobAndWait {
    param([Parameter(Mandatory)][string]$JobName, [int]$Minutes = 30)
    $execution = Invoke-Az @('containerapp', 'job', 'start', '-g', $rg, '-n', $JobName, '--query', 'name', '-o', 'tsv')
    if (-not $execution) { throw "Could not start job $JobName." }
    Write-Info "execution: $execution"
    # Kept in a hashtable, not in plain variables: the condition below runs as a scriptblock, and mutating this
    # object is what carries the status and the failure count back out to the diagnostics that follow.
    $poll = @{ Status = 'Running'; Failures = 0; Consecutive = 0 }
    $running = @('Running', 'Processing', 'Unknown', '')
    $null = Wait-Until -Activity "$JobName execution $execution" -ReadyLabel 'finished' -TimeoutMinutes $Minutes -Condition {
        # A poll that fails is not a job that failed. Letting the exception out of here abandoned a running
        # bootstrap job during a one-minute ARM outage: nothing was stopped, and none of the diagnostics below
        # were printed, so the job's outcome was simply unknown. The job can easily outlive a control-plane blip,
        # so keep asking until the deadline and only give up on the answer, never on the job.
        try {
            $poll.Status = Invoke-Az @('containerapp', 'job', 'execution', 'show', '-g', $rg, '-n', $JobName, '--job-execution-name', $execution,
                '--query', 'properties.status', '-o', 'tsv')
            $poll.Consecutive = 0
        }
        catch {
            $poll.Failures++
            $poll.Consecutive++
            $poll.Status = 'Unknown'
            $first = ($_.Exception.Message -split "`r?`n" | Where-Object { $_ -match '\S' } | Select-Object -First 1)
            # Warned once, on the first failure of a run: it has to stand out, because it is what decides at the
            # end that the outcome is unknown and the execution must be left alone. Repeats of the same failure
            # are carried by the wait's detail line instead, which prints only when the reason changes.
            if ($poll.Consecutive -eq 1) {
                Write-Warn "Could not read the status of $execution : $first"
                Write-Info '  The job itself is unaffected - this is the control plane. Still waiting.'
            }
            return @{ Ok = $false; Detail = "status unreadable ($($poll.Consecutive)x, job unaffected): $first" }
        }
        @{ Ok = ($poll.Status -notin $running); Detail = "status: $($poll.Status)" }
    }
    $status = $poll.Status
    $pollFailures = $poll.Failures
    if ($status -ne 'Succeeded') {
        # A run that is still going when the deadline passes has NOT stopped. Leaving it meant a re-run started a
        # second rag-bootstrap alongside it, and migrations/env.py takes no advisory lock - two concurrent
        # 'alembic upgrade head' runs against one database. Stop it before giving up.
        if ($pollFailures -gt 0) {
            # Not knowing the state is a reason NOT to act. Stopping blind could interrupt a healthy
            # 'alembic upgrade head' part-way through, which is worse than either leaving it alone or
            # re-running later once its outcome is known.
            Write-Warn "The status of $execution could not be read $pollFailures time(s) in a row, so its outcome is UNKNOWN and it was deliberately NOT stopped."
            Write-Info '  It may well have succeeded. Check with the first command below before re-running:'
            Write-Info '  two concurrent bootstrap executions would run migrations against one database at once.'
        }
        elseif ($status -in $running) {
            Write-Warn "$JobName did not finish within $Minutes minutes and is still running. Stopping execution $execution so a re-run cannot start a second one alongside it."
            try { $null = Invoke-Az @('containerapp', 'job', 'stop', '-g', $rg, '-n', $JobName, '--job-execution-name', $execution, '-o', 'none') }
            catch { Write-Warn "Could not stop it: $($_.Exception.Message.Split("`n")[0])" }
            Write-Info "  Confirm before re-running: az containerapp job execution list -g $rg -n $JobName --query `"[?properties.status=='Running'].name`" -o tsv"
        }
        Write-Host "    Status: az containerapp job execution list -g $rg -n $JobName --query `"sort_by([].{name:name,status:properties.status,start:properties.startTime}, &start)`" -o table" -ForegroundColor Yellow
        Write-Host "    Logs: az containerapp job logs show -g $rg -n $JobName --container $($JobName.Replace('rag-','')) --execution $execution --tail 200" -ForegroundColor Yellow
        Write-Host "    KQL : ContainerAppConsoleLogs_CL | where ContainerGroupName_s startswith '$execution' | order by _timestamp_d desc" -ForegroundColor Yellow
        throw "Job $JobName finished with status '$status' (execution $execution)."
    }
    Write-Ok "$JobName succeeded"
    return $execution
}

Write-Step 'Running rag-bootstrap (migrations, index, policy, facets)'
$bootstrapExecution = Start-JobAndWait -JobName 'rag-bootstrap' -Minutes $TimeoutMinutes
# The job prints a JSON summary. A malformed source no longer fails it - so the problems it reports have to be
# read out here, or a source that will never ingest anything would pass by in silence.
try {
    $jobLog = Invoke-Az @('containerapp', 'job', 'logs', 'show', '-g', $rg, '-n', 'rag-bootstrap',
        '--container', 'bootstrap', '--execution', $bootstrapExecution, '--tail', '50', '-o', 'tsv') -AllowNotFound
    foreach ($line in @("$jobLog" -split "`r?`n")) {
        if ($line -notmatch '"source_problems"') { continue }
        $problems = @((($line | Select-String -Pattern '\{.*\}').Matches.Value | ConvertFrom-Json).source_problems)
        if ($problems.Count -gt 0) {
            Write-Warn "$($problems.Count) source(s) in sources.yaml are misconfigured and will not ingest:"
            foreach ($problem in $problems) { Write-Info "  $problem" }
            Write-Info '  The index and the database schema are fine - fix these in config/sources/sources.yaml'
            Write-Info '  (or via /admin) and they will start working without another bootstrap.'
        }
    }
}
catch { Write-Verbose "Could not read the bootstrap job output: $($_.Exception.Message)" }

if (-not $SkipScheduler) {
    Write-Step 'Starting rag-scheduler once (first discovery)'
    $execution = Invoke-Az @('containerapp', 'job', 'start', '-g', $rg, '-n', 'rag-scheduler', '--query', 'name', '-o', 'tsv')
    Write-Ok "rag-scheduler started (execution $execution); it then runs on its cron schedule ($($Config.SchedulerCron))"
}

# ---------------------------------------------------------------------------------------------- readiness + URLs
$baseUrl = Get-ChatUiUrl -Config $Config

# Wait for what readyz depends on BEFORE asking readyz about it. 08 runs straight after 07, and a fresh
# rag-embed-query pulls a ~3 GB image and loads a model - minutes during which /api/readyz cannot succeed and is
# not meant to. Waiting here turns those minutes into a narrated wait instead of an opaque timeout.
Write-Step 'Waiting for the workloads /api/readyz depends on'
$null = Wait-ContainerAppReady -ResourceGroup $rg -Name 'rag-api' -TimeoutMinutes 10
if ((Get-EmbeddingProfileProvider -Config $Config) -in @('tei', $null)) {
    # /api/readyz probes the TEI query pool through the embedding-profile guard, so it cannot pass until this
    # pool serves /health. 15 minutes: the image is large and the model is loaded at startup.
    $null = Wait-ContainerAppReady -ResourceGroup $rg -Name 'rag-embed-query' -TimeoutMinutes 15
}

# One fast probe first. /api/healthz goes through the SAME nginx /api/ proxy block but returns instantly, so it
# separates "the chat-ui -> rag-api hop is broken" from "the hop is fine, a dependency is not". Without it a
# dependency that is merely slow looks exactly like a network fault.
Write-Step "Checking $baseUrl/api/healthz (the chat-ui -> rag-api hop)"
try {
    $hop = Invoke-WebRequest -Uri "$baseUrl/api/healthz" -TimeoutSec 20 -SkipHttpErrorCheck
    if ($hop.StatusCode -eq 200) { Write-Ok 'chat-ui reaches rag-api (HTTP 200)' }
    else {
        Write-Warn "chat-ui -> rag-api returned HTTP $($hop.StatusCode). /api/healthz is a static response, so this is the hop itself, not a dependency."
        Write-Info "  Diagnose it with: ./infra/scripts/Test-Connectivity.ps1 -Env $Env"
    }
}
catch {
    Write-Warn "chat-ui -> rag-api could not be reached: $($_.Exception.Message.Split("`n")[0])"
    Write-Info "  Diagnose it with: ./infra/scripts/Test-Connectivity.ps1 -Env $Env"
}

Write-Step "Waiting for $baseUrl/api/readyz"
$response = $null
# 40s per attempt, deliberately above readyz's own worst case of 32s (12s database + 20s profile guard). At 20s
# we cut off every slow-but-working attempt, which nginx logged as a 499 with no upstream response - a timeout
# that read exactly like a broken network.
$readyState = Wait-Until -Activity '/api/readyz' -TimeoutMinutes 10 -Condition {
    $script:response = Invoke-WebRequest -Uri "$baseUrl/api/readyz" -TimeoutSec 40 -SkipHttpErrorCheck
    $detail = if ($script:response.StatusCode -eq 200) { 'HTTP 200' }
    else { @(Format-ReadyzReasons -Content $script:response.Content -StatusCode $script:response.StatusCode) -join '; ' }
    @{ Ok = ($script:response.StatusCode -eq 200); Detail = $detail }
}
$ready = $readyState.Ok
$reason = if ($ready) { '' } else { "$($readyState.Detail)" }
if ($ready) {
    Write-Ok '/api/readyz is healthy'
    # Name the index explicitly. "The index is in place" should be something the transcript states, not
    # something the reader infers from the absence of an error - and this comes from the running API rather
    # than from the job's own claim about itself.
    try {
        $checks = ($response.Content | ConvertFrom-Json).checks.embedding_profile
        if ($checks.index) {
            Write-Info "index: $($checks.index)  (embedding profile $($checks.fingerprint))"
            # Recorded, not just printed: 09 compares what the API is querying NOW against the index bootstrap
            # actually created. A profile changed in between produces a NEW, EMPTY index - and an empty index
            # answers every question with "I could not find this", which reads like missing content.
            Save-Outputs -Config $Config -Values @{
                embeddingIndex       = "$($checks.index)"
                embeddingFingerprint = "$($checks.fingerprint)"
            }
        }
    }
    catch { Write-Verbose 'readyz returned 200 but no index detail could be parsed' }
}
else {
    Write-Fail "/api/readyz did not return 200 within $(Format-Elapsed $readyState.Elapsed)."
    foreach ($line in @(Format-ReadyzReasons -Content $(if ($response) { $response.Content } else { '' }) -StatusCode $(if ($response) { $response.StatusCode } else { 0 }))) {
        Write-Info "  $line"
    }
    Write-Info 'Usual causes, in the order worth checking:'
    Write-Info "  1. az containerapp logs show -g $rg -n rag-api --tail 100"
    Write-Info '  2. An embedding-profile mismatch between rag-api and the TEI pools (EMBEDDING_PROFILE must match'
    Write-Info "     the model the embedders actually serve): az containerapp logs show -g $rg -n rag-embed-query --tail 50"
    Write-Info '  3. A role assignment that has not replicated yet (Search, Storage, PostgreSQL). Wait and re-run.'
}

# ---------------------------------------------------------------------------------------------- wiring sheet
# Generated even when readiness failed: knowing which secret feeds which variable is exactly what you need while
# you are debugging one. It contains references only - never a secret value.
Write-Step 'Writing the deployment wiring sheet'
$sheet = Join-Path $Config.RepoRoot 'output.txt'
try { & (Join-Path $PSScriptRoot 'Write-OutputSheet.ps1') -Env $Config.EnvName -Path $sheet }
catch {
    Write-Warn "Could not write $sheet : $($_.Exception.Message)"
    Write-Info "Re-run it on its own: ./infra/scripts/Write-OutputSheet.ps1 -Env $($Config.EnvName)"
}

if ($ready) { Write-Step 'RAG-OS is deployed' } else { Write-Step 'RAG-OS is deployed, but NOT yet serving traffic' }
Write-Host "    Chat          : $baseUrl" -ForegroundColor Green
Write-Host "    Admin         : $baseUrl/admin" -ForegroundColor Green
if ($Config.DevAuthEnabled) { Write-Host "    Embed host    : $baseUrl/dev/embed-host" -ForegroundColor Green }
else { Write-Info "Dev embed host ($baseUrl/dev/embed-host) is disabled (DevAuthEnabled = `$false)." }
Write-Host "    OpenAPI       : $baseUrl/api/docs" -ForegroundColor Green
Write-Host "    Wiring sheet  : $sheet" -ForegroundColor Green
if (-not $ready) {
    # Exit non-zero so provision-all.ps1 and any CI wrapper stop here rather than reporting a green run.
    throw "The deployment finished but /api/readyz is not healthy. Fix that before running 09-smoke.ps1 (it will fail otherwise)."
}
Write-Info "Next: ./infra/scripts/09-smoke.ps1 -Env $($Config.EnvName)"
