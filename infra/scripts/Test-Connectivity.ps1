#Requires -Version 7.3
<#
.SYNOPSIS
    Checks every network hop RAG-OS depends on, and prints copy-paste tests for the ones only reachable from
    inside a container.
.DESCRIPTION
    Written because a slow dependency and a broken network look identical from outside: both produce a request
    that never comes back. What separates them is WHICH hop fails, and some hops cannot be tested from a laptop
    at all.

    Sections:
      1. Platform state      - ready replica counts per app. "0/2 ready" explains what an HTTP probe cannot.
      2. From this machine   - the chat UI, the chat-ui -> rag-api hop, then readiness.
      3. Backing services    - PostgreSQL, Search, Blob, Service Bus. BLOCKING: the bootstrap job reaches these
                               directly from its own container, so a failure here guarantees a wasted job.
      4. Console snippets    - for Monitoring -> Console in the portal, per container, using only the tools that
                               image actually has (verified: python-slim has no curl/wget; alpine and TEI do).

    Exit code 0 when nothing blocking failed, 1 otherwise, and set on every path so a caller can test it.
    Warnings never fail the run: a chat-ui -> rag-api problem does not stop bootstrap, which does not use that hop.
.EXAMPLE
    ./infra/scripts/Test-Connectivity.ps1 -Env dev
    ./infra/scripts/Test-Connectivity.ps1 -Env dev -SnippetsOnly   # just the Console commands
    ./infra/scripts/Test-Connectivity.ps1 -Env dev -Preflight      # what 08 runs: blocking hops only
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [switch]$SnippetsOnly,
    # What 08 runs before starting the job: the hops the job itself needs, and nothing else. No snippets, and no
    # readiness probe - at that point rag-embed-query is still starting and 08 waits for it separately.
    [switch]$Preflight
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title 'connectivity check'
$rg = $Config.Names.ResourceGroup
$o = Get-Outputs -Config $Config

$results = [System.Collections.Generic.List[object]]::new()
function Add-Result([string]$Hop, [bool]$Ok, [string]$Detail, [switch]$Blocking) {
    $results.Add([pscustomobject]@{ Hop = $Hop; Ok = $Ok; Detail = $Detail; Blocking = [bool]$Blocking })
    $label = if ($Ok) { 'ok' } elseif ($Blocking) { 'FAIL' } else { 'warn' }
    $colour = if ($Ok) { 'Green' } elseif ($Blocking) { 'Red' } else { 'Yellow' }
    Write-Host ("    [{0}] {1,-42} {2}" -f $label, $Hop, $Detail) -ForegroundColor $colour
}

function Test-Tcp([string]$HostName, [int]$Port, [int]$TimeoutMs = 5000) {
    # Test-NetConnection is Windows-only and slow; a raw socket with an explicit timeout is portable and exact.
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        if (-not $client.ConnectAsync($HostName, $Port).Wait($TimeoutMs)) { return "no response in $($TimeoutMs)ms" }
        return $null
    }
    catch { return ($_.Exception.GetBaseException().Message) }
    finally { $client.Dispose() }
}

$embedProvider = Get-EmbeddingProfileProvider -Config $Config
$useTei = $embedProvider -in @('tei', $null)
$apps = @('rag-api', 'rag-chat-ui', 'rag-ingest-worker')
if ($useTei) { $apps += @('rag-embed-query', 'rag-embed-ingest') }

if (-not $SnippetsOnly) {
    # ---------------------------------------------------------------------------------- 1. platform state
    Write-Step 'Container app replicas'
    foreach ($app in $apps) {
        try {
            $replicas = @(Invoke-Az @('containerapp', 'replica', 'list', '-g', $rg, '-n', $app,
                    '--query', '[].{name:name, containers:properties.containers[].ready}') -AllowNotFound)
            $ready = @($replicas | Where-Object { $_ -and @($_.containers).Count -gt 0 -and (@($_.containers) -notcontains $false) })
            Add-Result $app ($ready.Count -gt 0) "$($ready.Count)/$($replicas.Count) replicas ready"
        }
        catch { Add-Result $app $false ($_.Exception.Message.Split("`n")[0]) }
    }

    # ---------------------------------------------------------------------------------- 2. from this machine
    # Skipped by -Preflight: 08 calls this before the job, when the chat UI and the embedder are still
    # starting, so these would report a problem that is only a workload that has not come up yet.
    if (-not $Preflight) {
        Write-Step 'From this machine'
        $baseUrl = Get-ChatUiUrl -Config $Config
        foreach ($probe in @(
                @{ Path = '/healthz'; Hop = 'chat UI (nginx, no API involved)'; Timeout = 15 }
                # Same nginx /api/ proxy block as readyz, but a static response - so this isolates the hop itself.
                @{ Path = '/api/healthz'; Hop = 'chat-ui -> rag-api hop'; Timeout = 20 }
                # Above readyz's own 32s budget (12s database + 20s profile guard), or a slow-but-working answer is cut off.
                @{ Path = '/api/readyz'; Hop = 'rag-api readiness (dependencies)'; Timeout = 40 }
            )) {
            try {
                $r = Invoke-WebRequest -Uri "$baseUrl$($probe.Path)" -TimeoutSec $probe.Timeout -SkipHttpErrorCheck
                $detail = if ($r.StatusCode -eq 200) { 'HTTP 200' } else { @(Format-ReadyzReasons -Content $r.Content -StatusCode $r.StatusCode) -join '; ' }
                Add-Result $probe.Hop ($r.StatusCode -eq 200) $detail
            }
            catch { Add-Result $probe.Hop $false ($_.Exception.Message.Split("`n")[0]) }
        }

        # Internal ingress is a security property, so verify it rather than assume it.
        $apiFqdn = Invoke-Az @('containerapp', 'show', '-g', $rg, '-n', 'rag-api',
            '--query', 'properties.configuration.ingress.fqdn', '-o', 'tsv') -AllowNotFound
        if ($apiFqdn) {
            try {
                $null = Invoke-WebRequest -Uri "https://$apiFqdn/api/healthz" -TimeoutSec 10 -SkipHttpErrorCheck
                Add-Result 'rag-api is NOT public' $false "reachable at $apiFqdn - ingress should be internal"
            }
            catch { Add-Result 'rag-api is NOT public' $true 'not reachable from outside, as intended' }
        }
    }

    # ---------------------------------------------------------------------------------- 3. backing services
    Write-Step 'Backing services (these block the bootstrap job)'
    $pgFqdn = Get-Value $o 'postgresFqdn'
    if ($pgFqdn) {
        $err = Test-Tcp $pgFqdn 5432
        Add-Result 'PostgreSQL :5432' (-not $err) ($err ?? "$pgFqdn reachable") -Blocking
        if ($err) { Write-Info '      From this machine that needs the AllowClientIp firewall rule (see 03-data.ps1).' }
    }
    foreach ($svc in @(
            @{ Hop = 'AI Search :443'; Url = Get-Value $o 'searchEndpoint' }
            @{ Hop = 'Blob storage :443'; Url = Get-Value $o 'blobEndpoint' }
            @{ Hop = 'Service Bus :443'; Url = "https://$(Get-Value $o 'serviceBusFqdn')" }
        )) {
        if (-not $svc.Url -or $svc.Url -eq 'https://') { continue }
        $target = ([uri]$svc.Url).Host
        $err = Test-Tcp $target 443
        Add-Result $svc.Hop (-not $err) ($err ?? "$target reachable") -Blocking
    }
}

# ---------------------------------------------------------------------------------- 4. console snippets
# The container-to-container hops cannot be reached from outside: rag-api has internal ingress and the TEI pools
# are internal too. These run in the portal under the app's Monitoring -> Console.
# Tool inventory verified by running each base image: python:3.13-slim has python3 and openssl but NO
# curl/wget/nc; nginx-unprivileged:alpine has curl, wget, nc, nslookup, ping; the TEI image has curl and openssl
# but no python3 or wget. Each snippet therefore uses only what its own image provides.
if ($Preflight) {
    Write-Info "Container-to-container tests: ./infra/scripts/Test-Connectivity.ps1 -Env $Env -SnippetsOnly"
}
else {
    Write-Step 'Tests to paste into Monitoring -> Console'
    # A missing output would otherwise render as `getent hosts ` - a command that silently means nothing.
    $pg = (Get-Value $o 'postgresFqdn') ? (Get-Value $o 'postgresFqdn') : '<postgresFqdn-not-in-outputs>'
    $search = ([uri](Get-Value $o 'searchEndpoint')).Host ? ([uri](Get-Value $o 'searchEndpoint')).Host : '<searchEndpoint-not-in-outputs>'
    $snippets = [ordered]@{
        'rag-chat-ui (container: chat-ui)' = @(
            '# Does nginx resolve and reach the API? This is the hop the browser uses.'
            'nslookup rag-api'
            'curl -sS -m 5 -o /dev/null -w "api/healthz -> %{http_code} in %{time_total}s\n" http://rag-api/api/healthz'
            '# NO -o /dev/null on readyz: its body IS the diagnosis. A 503 here names the failing dependency,'
            '# and discarding it is what made a dependency problem look like a network problem.'
            'curl -sS -m 45 -w "\napi/readyz  -> %{http_code} in %{time_total}s\n" http://rag-api/api/readyz'
        )
        'rag-api (container: api)'          = @(
            '# python3 only in this image - no curl, no wget, no nc.'
            '# Step 1, DNS. Run these first: if a name prints nothing, that is the fault, and every command below'
            '# it will fail for that reason alone. getent is used rather than python because connect_ex returns a'
            '# code for a refused port but still raises on an unresolvable name - which would bury the real answer.'
            'getent hosts rag-embed-query'
            "getent hosts $pg"
            "getent hosts $search"
            '# Step 2, is the port open? connect_ex returns a code rather than raising, so a refused port prints'
            '# one clean line. `2>&1 | tail -n 1` covers the other case: an unresolvable name raises before'
            '# connect_ex is reached, and the last line of that traceback is the one that names the cause.'
            "python3 -c `"import socket;s=socket.socket();s.settimeout(5);print('embed-query :80 ', 'OPEN' if s.connect_ex(('rag-embed-query',80))==0 else 'UNREACHABLE')`" 2>&1 | tail -n 1"
            "python3 -c `"import socket;s=socket.socket();s.settimeout(5);print('postgres :5432 ', 'OPEN' if s.connect_ex(('$pg',5432))==0 else 'UNREACHABLE')`" 2>&1 | tail -n 1"
            "python3 -c `"import socket;s=socket.socket();s.settimeout(5);print('search :443   ', 'OPEN' if s.connect_ex(('$search',443))==0 else 'UNREACHABLE')`" 2>&1 | tail -n 1"
            '# Is the app even listening on its own port? Check before the body probe below, which would otherwise'
            '# raise ConnectionRefusedError and bury the answer in a traceback.'
            "python3 -c `"import socket;s=socket.socket();s.settimeout(5);print('api :8000      ', 'OPEN' if s.connect_ex(('localhost',8000))==0 else 'UNREACHABLE')`" 2>&1 | tail -n 1"
            '# Is it the app, or the path to it? This asks rag-api on its own loopback, so nginx and the'
            '# ingress are both out of the picture: a 503 here is unambiguously the application refusing,'
            '# and the body says which dependency. urlopen RAISES on 503 and would print a traceback instead'
            '# of the answer, so http.client is used - it returns the response for any status.'
            '# Do NOT pipe this one through tail: the body is the payload, not the last line of an error.'
            "python3 -c `"import http.client,json;c=http.client.HTTPConnection('localhost',8000,timeout=45);c.request('GET','/api/readyz');r=c.getresponse();print('status',r.status);print(json.dumps(json.loads(r.read()),indent=2))`""
            '# ?fresh=1 re-checks Search and the embedder rather than the guard cached verdict (up to 60s old).'
            "python3 -c `"import http.client,json;c=http.client.HTTPConnection('localhost',8000,timeout=45);c.request('GET','/api/readyz?fresh=1');r=c.getresponse();print('status',r.status);print(json.dumps(json.loads(r.read()),indent=2))`""
            '# Step 3, does the service answer? The TEI query pool is the dependency /api/readyz waits for, so a'
            '# slow answer here is the whole explanation for a slow readyz - which is why the time is printed.'
            "python3 -c `"import urllib.request as u,time;t=time.time();r=u.urlopen('http://rag-embed-query/health',timeout=10);print('embed-query /health ->',r.status,'in',round(time.time()-t,2),'s')`" 2>&1 | tail -n 1"
            "python3 -c `"import urllib.request as u;print('embed-ingest /health ->',u.urlopen('http://rag-embed-ingest/health',timeout=10).status)`" 2>&1 | tail -n 1"
        )
    }
    if ($useTei) {
        $snippets['rag-embed-query (container: embed)'] = @(
            '# TEI image: curl is present, python3 is not.'
            'curl -sS -m 5 -w "\nself /health -> %{http_code}\n" http://localhost:80/health'
            '# /info reports the model actually loaded - compare it with EmbeddingProfile in the psd1.'
            'curl -sS -m 5 http://localhost:80/info'
        )
    }
    foreach ($app in $snippets.Keys) {
        Write-Host ''
        Write-Host "  --- $app ---" -ForegroundColor Cyan
        foreach ($line in $snippets[$app]) { Write-Host "  $line" }
    }
    Write-Host ''
    Write-Info 'az containerapp exec opens an interactive shell, so it is convenient by hand but unreliable in a script:'
    Write-Info "  az containerapp exec -g $rg -n rag-api --command sh"
}

# ---------------------------------------------------------------------------------- verdict
if ($SnippetsOnly) { exit 0 }
Write-Step 'Result'
$blocking = @($results | Where-Object { -not $_.Ok -and $_.Blocking })
$warnings = @($results | Where-Object { -not $_.Ok -and -not $_.Blocking })
if ($warnings.Count -gt 0) {
    Write-Warn "$($warnings.Count) hop(s) are unhealthy but do not block the bootstrap job: $(($warnings.Hop) -join ', ')"
    Write-Info '  The job talks to PostgreSQL, Search and Blob from its own container - not through rag-api.'
}
if ($blocking.Count -gt 0) {
    Write-Fail "$($blocking.Count) hop(s) the bootstrap job needs are unreachable: $(($blocking.Hop) -join ', ')"
    Write-Info '  Running 08 now would spend up to 30 minutes on a job that cannot succeed.'
    exit 1
}
Write-Ok 'Every hop the bootstrap job needs is reachable.'
exit 0
