#Requires -Version 7.3
<#
.SYNOPSIS
    Pause, resume or throttle ingestion through the admin API (PUT /api/admin/ingestion/controls).
.DESCRIPTION
    Workers re-read the ingestion_controls row every INGEST_CONTROLS_REFRESH_S seconds (30 by default), so a change
    takes effect within about half a minute without a redeployment.
    The request goes through the public chat UI URL, which proxies /api/* to the internal API.
    The bearer token must belong to a principal with the admin role. Pass it with -Token or set RAG_OS_ADMIN_TOKEN
    (preferred: it keeps the token out of your shell history).
    The current controls are fetched first and merged, so each switch changes only its own field.
.EXAMPLE
    $env:RAG_OS_ADMIN_TOKEN = '<jwt>'
    ./infra/scripts/Set-IngestionControls.ps1 -Env dev -Pause
    ./infra/scripts/Set-IngestionControls.ps1 -Env dev -Resume -MaxConcurrency 8
    ./infra/scripts/Set-IngestionControls.ps1 -Env dev -MaxConcurrency 2 -SourceId hr-share
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [switch]$Pause,
    [switch]$Resume,
    [ValidateRange(0, 64)][int]$MaxConcurrency = -1,
    [string]$SourceId,
    [string]$Token = $env:RAG_OS_ADMIN_TOKEN,
    [string]$BaseUrl
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title 'Ingestion controls'
if ($Pause -and $Resume) { throw 'Use either -Pause or -Resume.' }
if (-not $Pause -and -not $Resume -and $MaxConcurrency -lt 0) { throw 'Nothing to do: pass -Pause, -Resume and/or -MaxConcurrency.' }
if (-not $Token) { throw 'No admin token. Set $env:RAG_OS_ADMIN_TOKEN or pass -Token (a JWT for a principal with the admin role).' }
if (-not $BaseUrl) { $BaseUrl = Get-ChatUiUrl -Config $Config }
$url = "$($BaseUrl.TrimEnd('/'))/api/admin/ingestion/controls"
$headers = @{ Authorization = "Bearer $Token" }

Write-Step "GET $url"
$body = @{}
try {
    $current = Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 30
    foreach ($property in $current.PSObject.Properties) { $body[$property.Name] = $property.Value }
    Write-Info "current: $($current | ConvertTo-Json -Compress -Depth 5)"
}
catch {
    Write-Warn "Could not read the current controls ($($_.Exception.Message)); sending only the requested fields."
}

if ($Pause) { $body['paused'] = $true }
if ($Resume) { $body['paused'] = $false }
if ($MaxConcurrency -ge 0) { $body['max_concurrency'] = $MaxConcurrency }
if ($SourceId) { $body['source_id'] = $SourceId }

Write-Step "PUT $url"
Write-Info "body: $($body | ConvertTo-Json -Compress -Depth 5)"
$result = Invoke-RestMethod -Uri $url -Method Put -Headers $headers -ContentType 'application/json' -Body ($body | ConvertTo-Json -Depth 5) -TimeoutSec 30
Write-Ok "updated: $($result | ConvertTo-Json -Compress -Depth 5)"
Write-Info "Workers pick this up within INGEST_CONTROLS_REFRESH_S seconds. Status: $BaseUrl/admin"
