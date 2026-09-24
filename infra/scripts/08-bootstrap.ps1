#Requires -Version 7.3
<#
.SYNOPSIS
    Step 08 - upload config/** to the config blob container, run the rag-bootstrap job, start the scheduler, print the URLs.
.DESCRIPTION
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
    [int]$TimeoutMinutes = 30
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '08 bootstrap'
$rg = $Config.Names.ResourceGroup
$storage = Get-Output -Config $Config -Name 'storageName' -ProducedBy '03-data.ps1'

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
    $deadline = (Get-Date).AddMinutes($Minutes)
    $status = 'Running'
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 15
        $status = Invoke-Az @('containerapp', 'job', 'execution', 'show', '-g', $rg, '-n', $JobName, '--job-execution-name', $execution,
            '--query', 'properties.status', '-o', 'tsv')
        Write-Info "status: $status"
        if ($status -notin @('Running', 'Processing', 'Unknown', '')) { break }
    }
    if ($status -ne 'Succeeded') {
        # A run that is still going when the deadline passes has NOT stopped. Leaving it meant a re-run started a
        # second rag-bootstrap alongside it, and migrations/env.py takes no advisory lock - two concurrent
        # 'alembic upgrade head' runs against one database. Stop it before giving up.
        if ($status -in @('Running', 'Processing', 'Unknown', '')) {
            Write-Warn "$JobName did not finish within $Minutes minutes and is still running. Stopping execution $execution so a re-run cannot start a second one alongside it."
            try { $null = Invoke-Az @('containerapp', 'job', 'stop', '-g', $rg, '-n', $JobName, '--job-execution-name', $execution, '-o', 'none') }
            catch { Write-Warn "Could not stop it: $($_.Exception.Message.Split("`n")[0])" }
            Write-Info "  Confirm before re-running: az containerapp job execution list -g $rg -n $JobName --query `"[?properties.status=='Running'].name`" -o tsv"
        }
        Write-Host "    Logs: az containerapp job logs show -g $rg -n $JobName --container $($JobName.Replace('rag-','')) --execution $execution --tail 200" -ForegroundColor Yellow
        Write-Host "    KQL : ContainerAppConsoleLogs_CL | where ContainerGroupName_s startswith '$execution' | order by _timestamp_d desc" -ForegroundColor Yellow
        throw "Job $JobName finished with status '$status'."
    }
    Write-Ok "$JobName succeeded"
    return $execution
}

Write-Step 'Running rag-bootstrap (migrations, index, policy, facets)'
$null = Start-JobAndWait -JobName 'rag-bootstrap' -Minutes $TimeoutMinutes

if (-not $SkipScheduler) {
    Write-Step 'Starting rag-scheduler once (first discovery)'
    $execution = Invoke-Az @('containerapp', 'job', 'start', '-g', $rg, '-n', 'rag-scheduler', '--query', 'name', '-o', 'tsv')
    Write-Ok "rag-scheduler started (execution $execution); it then runs on its cron schedule ($($Config.SchedulerCron))"
}

# ---------------------------------------------------------------------------------------------- readiness + URLs
$baseUrl = Get-ChatUiUrl -Config $Config
Write-Step "Waiting for $baseUrl/api/readyz"
$ready = $false
$reason = ''
$deadline = (Get-Date).AddMinutes(10)
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri "$baseUrl/api/readyz" -TimeoutSec 20 -SkipHttpErrorCheck
        if ($response.StatusCode -eq 200) { $ready = $true; break }
        $reason = "HTTP $($response.StatusCode): $($response.Content)"
    }
    catch { $reason = $_.Exception.Message }
    Start-Sleep -Seconds 15
}
if ($ready) { Write-Ok '/api/readyz is healthy' }
else {
    Write-Fail "/api/readyz did not return 200 within 10 minutes: $reason"
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
