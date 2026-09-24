#!/usr/bin/env pwsh
<#
.SYNOPSIS
  One entry point for everyday RAG-OS tasks (PowerShell 7).

.EXAMPLE
  ./tasks.ps1 up          # build + start the local stack (docker compose)
  ./tasks.ps1 seed        # ingest samples/corpus
  ./tasks.ps1 test        # unit + offline integration tests
  ./tasks.ps1 smoke       # smoke test against http://localhost:8080 (or -BaseUrl)
  ./tasks.ps1 deploy -Env dev   # build images + roll out to Azure (scripts 06 + 07)
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'up', 'down', 'seed', 'status', 'logs', 'test', 'test-int', 'lint', 'build',
                 'provision', 'deploy', 'smoke', 'loadtest', 'synthetic', 'dev-api', 'dev-worker')]
    [string]$Task = 'help',
    [string]$Env = 'dev',
    [string]$BaseUrl = 'http://localhost:8080',
    [int]$Docs = 10000,
    [string]$Service = ''
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $env:UV_NATIVE_TLS) { $env:UV_NATIVE_TLS = '1' }   # corporate TLS inspection friendly

function Invoke-Step([string]$Title, [scriptblock]$Block) {
    Write-Host "==> $Title" -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "$Title failed (exit $LASTEXITCODE)" }
}

switch ($Task) {
    'help' { Get-Help $PSCommandPath -Detailed; break }
    'up' {
        Invoke-Step 'docker compose up' { docker compose up -d --build }
        Write-Host "`nChat:        $BaseUrl/`nEmbed host:  $BaseUrl/dev/embed-host`nAdmin:       $BaseUrl/admin`nAPI docs:    $BaseUrl/api/docs"
        Write-Host "First start downloads the embedding model (~1.2 GB). Watch: ./tasks.ps1 logs -Service tei"
    }
    'down' { Invoke-Step 'docker compose down' { docker compose down } }
    'seed' {
        Invoke-Step 'ingest samples/corpus' { docker compose exec api rag-os discover --source sample-corpus }
        Write-Host "Worker is processing - progress: $BaseUrl/admin  or  ./tasks.ps1 status"
    }
    'status' { Invoke-Step 'ingestion status' { docker compose exec api rag-os status } }
    'logs' { if ($Service) { docker compose logs -f $Service } else { docker compose logs -f --tail 100 } }
    'test' { Invoke-Step 'pytest' { uv run --extra dev pytest -q } }
    'test-int' {
        Invoke-Step 'pytest (incl. integration marker)' { uv run --extra dev pytest -q -m "integration or not integration" }
    }
    'lint' {
        Invoke-Step 'ruff' { uv run --extra dev ruff check src tests scripts }
        Invoke-Step 'mypy (domain + application)' { uv run --extra dev mypy }
    }
    'build' { Invoke-Step 'docker compose build' { docker compose build } }
    'provision' { Invoke-Step "provision Azure ($Env)" { pwsh -NoProfile -File ./infra/scripts/provision-all.ps1 -Env $Env } }
    'deploy' {
        Invoke-Step "build images in ACR ($Env)" { pwsh -NoProfile -File ./infra/scripts/06-registry-build.ps1 -Env $Env }
        Invoke-Step "roll out container apps ($Env)" { pwsh -NoProfile -File ./infra/scripts/07-container-apps.ps1 -Env $Env }
    }
    'smoke' { Invoke-Step "smoke test $BaseUrl" { uv run python scripts/smoke.py --base-url $BaseUrl } }
    'synthetic' { Invoke-Step "generate $Docs synthetic docs" { uv run python scripts/loadtest.py generate --docs $Docs --out samples/synthetic } }
    'loadtest' { Invoke-Step "load test $BaseUrl" { uv run python scripts/loadtest.py run --base-url $BaseUrl } }
    'dev-api' {
        if (-not (Test-Path .env)) { Copy-Item .env.sample .env; Write-Host 'created .env from .env.sample' }
        Invoke-Step 'bootstrap' { uv run rag-os bootstrap }
        uv run uvicorn rag_os.api.main:app --reload --port 8000
    }
    'dev-worker' { uv run rag-os worker }
}
