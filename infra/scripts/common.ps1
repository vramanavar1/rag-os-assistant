#Requires -Version 7.3

<#
.SYNOPSIS
    Shared helpers for the RAG-OS provisioning scripts. Dot-source it:  . (Join-Path $PSScriptRoot 'common.ps1')

.DESCRIPTION
    - Import-RagOsConfig   loads infra/env/<env>.psd1 (defaults from dev.sample.psd1) and derives every resource name.
    - Invoke-Az            runs az with an argument array, parses JSON, throws on a non-zero exit code with stderr,
                           and never echoes arguments marked -Sensitive.
    - Get-AzResourceOrNull / Test-AzResource / Ensure-AzResource   show-then-create idempotency.
    - Invoke-WithRetry     retries transient failures (role-assignment propagation, replication delays).
    - Grant-Role           idempotent role assignment by object id (no Microsoft Graph lookup).
    - Expand-Template      {{TOKEN}} replacement for infra/containerapps/*.yaml.tmpl.
    - Save-Outputs / Get-Outputs / Get-Output   persist ids/endpoints to infra/env/<env>.outputs.json.
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseApprovedVerbs', '', Justification = 'Ensure-* is the documented show-then-create pattern')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Interactive console scripts')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '', Justification = 'Outputs is a file of many values')]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# We check $LASTEXITCODE ourselves; do not let PowerShell turn native stderr into terminating errors.
$PSNativeCommandUseErrorActionPreference = $false
# az is a Python app: force UTF-8 so streamed build logs never raise UnicodeEncodeError on Windows consoles.
$env:PYTHONIOENCODING = 'utf-8'
if (-not $env:AZURE_EXTENSION_USE_DYNAMIC_INSTALL) { $env:AZURE_EXTENSION_USE_DYNAMIC_INSTALL = 'yes_without_prompt' }
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { Write-Verbose 'Console encoding unchanged' }

$script:RagOsScriptsDir = $PSScriptRoot
$script:RagOsInfraDir = Split-Path -Parent $PSScriptRoot
$script:RagOsRepoRoot = Split-Path -Parent $script:RagOsInfraDir
$script:RagOsEnvDir = Join-Path $script:RagOsInfraDir 'env'
# ARM's house style for "it is not there". Deliberately narrow, and NOT a list to grow when a new command
# phrases absence differently:
#   - it also gates Test-TransientFailure below, so every wording added here stops being retried repo-wide;
#   - Invoke-Az consults it before the transient check, so a newly-matched message becomes a silent $null at
#     every -AllowNotFound call site rather than an error.
# az commands that write their own prose ("No deleted Vault or HSM was found with name X", "There are no active
# accounts.") cannot be covered here safely. Give those an existence check that exits 0 instead - typically
# `az <thing> list --query "[?name=='X']"` - so there is no error to classify. See Get-AzAccountOrNull.
$script:RagOsNotFoundPattern = '(?i)(ResourceNotFound|ResourceGroupNotFound|NotFound|was not found|could not be found|does not exist|not found)'
# Graph rejecting a preAuthorizedApplications entry whose permission id it cannot see yet. Named once because
# two places must agree on it: the retry that waits for replication, and the catch that explains it. It
# deliberately overlaps neither of the patterns below - see the two-layer note - and note that 'cannot be
# found' is NOT matched by RagOsNotFoundPattern above ('could not be found' is), so this never reaches the
# -AllowNotFound path and get silently swallowed.
$script:RagOsGraphPermissionIdPattern = '(?i)(AppPermissions sets|Permission Id that cannot be found)'
# Two retry layers, deliberately kept apart:
#   Invoke-Az (here)  - transport, throttling and control-plane 5xx. 3 attempts, seconds apart.
#   Invoke-WithRetry  - eventual consistency (Entra RBAC replication, resource state). Tailored -RetryOn,
#                       10-12 attempts, because directory replication routinely takes minutes.
# Nothing appears in both lists: overlapping them would multiply the attempts (10 outer x 3 inner) and turn a
# two-minute wait into a twenty-minute one. So Forbidden/AuthorizationFailed/PrincipalNotFound/conflict/in-progress
# are NOT transient here - they belong to the outer layer, which knows how long each is worth waiting for.
# Deterministic failures (bad name, quota denied, unsupported region, already exists) are in neither.
$script:RagOsTransientPattern = @(
    'TooManyRequests', 'Too many requests', 'RequestThrottled', 'throttl',        # 429
    # Both spellings of every 5xx. ARM's own error pages use the spaced, human form - 'Service Unavailable' -
    # while the SDK uses CamelCase, and having only the latter is what turned a one-minute Azure blip into a
    # failed deployment: Invoke-Az saw no match, did not retry, and step 08 abandoned a running bootstrap job.
    'InternalServerError', 'Internal Server Error',
    'ServiceUnavailable', 'Service Unavailable',
    'ServerTimeout', 'Server Timeout',
    'GatewayTimeout', 'Gateway Timeout',
    'BadGateway', 'Bad Gateway',
    '\(50[0234]\)', 'status code: 50[0234]', '\b50[0234]\b.{0,40}(?:Service Unavailable|Bad Gateway|Gateway Timeout|Internal Server Error)',
    # az choking on an HTML error page. The CLI asks for JSON, ARM returns a maintenance page, and json.loads
    # fails - so the traceback is about quoting rather than about Azure. It is never a real answer.
    'Expecting property name enclosed in double quotes', 'JSONDecodeError',
    'Cannot deserialize', 'DOCTYPE html',
    'Operation timed out', 'timed out', 'Connection reset', 'Connection aborted', 'connection was closed',
    'Temporary failure', 'EOF occurred', 'Max retries exceeded', 'ConnectionError', 'ReadTimeout',
    'RetryableError', 'please retry', 'Please try again', 'try again later'
) -join '|'
$script:RagOsTransientPattern = "(?i)($script:RagOsTransientPattern)"
$script:RagOsMaxAttempts = 3          # total attempts, i.e. the first try plus 2 retries
$script:RagOsRetryDelays = @(3, 9)    # seconds before attempt 2 and attempt 3
$script:RagOsDeployer = $null
$script:RagOsAzInvocation = $null
$script:RagOsLastAccountError = $null   # why 'az account show' last failed, so callers can quote az rather than guess

# A streamed build (az acr build) costs minutes to hours, so its retry set is deliberately much narrower than
# RagOsTransientPattern: only failures that happen while pulling or pushing layers, where a retry is seconds of
# work. A timeout is excluded on purpose - re-running the same two-hour build to fail identically at the same
# point helps nobody, and is checked first below.
$script:RagOsStreamRetryPattern = '(?i)(toomanyrequests|too many requests|rate limit|TLS handshake timeout|' +
'connection reset|i/o timeout|unexpected EOF|temporary failure in name resolution|net/http|' +
'\b(?:502|503)\b|registry is unavailable|error pulling image|failed to (?:pull|fetch) )'
$script:RagOsTimeoutPattern = '(?i)(context deadline exceeded|timed out|timeout (?:exceeded|reached)|' +
'run timeout|step timeout|deadline exceeded)'

# Known az/ACR failure signatures -> what actually went wrong and what to do about it. Ordered: the first match
# wins, so the specific entries come before the general ones. These exist because the failing line is usually
# buried a long way up a build log, and the reader has no reason to know which of two hundred lines mattered.
$script:RagOsFailureHints = @(
    @{ Match = '(?i)(requires BuildKit|--mount option|dockerfile frontend)'
        Cause = 'The Dockerfile uses BuildKit-only syntax, but az acr build runs on ACR Tasks, which uses the classic Docker builder.'
        Fix   = 'Remove the BuildKit construct (RUN --mount, COPY --link, heredocs, "# syntax="). uv run pytest tests/unit/test_dockerfiles.py catches these before a deploy.' }
    @{ Match = '(?i)(toomanyrequests|too many requests|pull rate limit|rate limit exceeded)'
        Cause = "Docker Hub's anonymous pull rate limit was hit while pulling a base image."
        Fix   = 'Copy the base images into your own registry once (az acr import --source docker.io/library/python:3.13-slim -n <acr>) and reference them, or wait for the limit window to reset.' }
    @{ Match = '(?i)(quota|no space left on device|storage limit|exceeded the storage)'
        Cause = 'The registry (or the build agent disk) is full.'
        Fix   = 'Check with: az acr show-usage -n <acr> -o table. Then delete old tags (az acr repository delete) or raise AcrSku in the psd1 - Basic is 10 GB, Standard 100 GB.' }
    @{ Match = '(?i)(manifest unknown|manifest for .* not found|repository does not exist|not found: manifest)'
        Cause = 'A base image tag referenced by the Dockerfile does not exist in its registry.'
        Fix   = 'Check the FROM tags and TeiVersion in the psd1 against what the upstream registry actually publishes.' }
    @{ Match = '(?i)(unauthorized|authentication required|pull access denied|denied: requested access)'
        Cause = 'The build could not authenticate to a registry.'
        Fix   = "Confirm the managed identity's AcrPull assignment from step 06, and that the base image registry does not need credentials." }
    @{ Match = '(?i)(MissingSubscriptionRegistration|not registered to use namespace)'
        Cause = 'An Azure resource provider is not registered on this subscription.'
        Fix   = 'Run ./infra/scripts/00-prereqs.ps1 -Env <env>, which registers every provider RAG-OS needs.' }
    @{ Match = '(?i)(AppPermissions sets|Permission Id that cannot be found)'
        Cause = 'A Microsoft Graph write pre-authorised a client for a permission id the application does not (yet) expose. Graph validates api.preAuthorizedApplications against the permissions ALREADY PERSISTED on the app, never against the oauth2PermissionScopes in the same request, and it rejects the whole request rather than the one entry.'
        Fix   = 'Write the scope first, then pre-authorise in a second call - ./infra/scripts/Set-EntraAppRegistration.ps1 does this in the right order. If it is already in that order, an existing pre-authorised client is pointing at a permission the app no longer defines; remove that entry in the Entra admin center (Expose an API -> Authorized client applications).' }
)

function Get-AzFailureHint {
    <#
    .SYNOPSIS  Cause + recommendation for a known az failure, or $null when the text matches nothing known.
    .DESCRIPTION
        Returning $null for unrecognised output is the point: a guess dressed up as a diagnosis is worse than
        the raw error, because it sends the reader somewhere else entirely.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    foreach ($hint in $script:RagOsFailureHints) {
        if ($Text -match $hint.Match) { return [pscustomobject]@{ Cause = $hint.Cause; Fix = $hint.Fix } }
    }
    return $null
}

function Test-StreamRetryable {
    <# .SYNOPSIS  Is a long streamed build worth re-running? Timeouts never are. #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    if ($Text -match $script:RagOsTimeoutPattern) { return $false }
    return [bool]($Text -match $script:RagOsStreamRetryPattern)
}

function Format-OutputPauses {
    <#
    .SYNOPSIS  The longest silences in a streamed command, as "<duration> after <the line before it>".
    .DESCRIPTION
        A build that takes an hour spends nearly all of it inside one or two steps. Timestamping the lines that
        do arrive is enough to say which, with no timers and no extra Azure calls - and that is the question
        "why did this take so long" actually reduces to.
    #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Pauses, [int]$Top = 3, [int]$MinimumSeconds = 60)
    $worst = @($Pauses | Where-Object { $_.Seconds -ge $MinimumSeconds } | Sort-Object -Property Seconds -Descending | Select-Object -First $Top)
    if ($worst.Count -eq 0) { return @() }
    $lines = @('Longest pauses (where the time went):')
    foreach ($p in $worst) {
        $span = if ($p.Seconds -ge 60) { "{0,3} min" -f [int]($p.Seconds / 60) } else { "{0,3} sec" -f $p.Seconds }
        $after = "$($p.After)".Trim()
        if ($after.Length -gt 90) { $after = $after.Substring(0, 87) + '...' }
        $lines += "  $span after  $after"
    }
    return $lines
}

function Test-TransientFailure {
    <# .SYNOPSIS  Is this az stderr worth retrying? Deterministic failures must return $false. #>
    [CmdletBinding()]
    param([string]$Stderr)
    if ([string]::IsNullOrWhiteSpace($Stderr)) { return $false }   # no error text: do not guess, fail fast
    if ($Stderr -match $script:RagOsNotFoundPattern) { return $false }
    return [bool]($Stderr -match $script:RagOsTransientPattern)
}

function Get-AzInvocation {
    <#
    .SYNOPSIS  How to launch the Azure CLI: @{ Exe; Prefix }.
    .DESCRIPTION
        On Windows 'az' is az.cmd, a batch file: PowerShell passes arguments to batch files with legacy quoting, so
        embedded double quotes are lost and cmd.exe interprets | & < > in JMESPath queries. When the MSI layout is
        detected we call its bundled python.exe exactly like az.cmd does (python -IBm azure.cli), which passes every
        argument verbatim. '-X utf8' makes captured output UTF-8. Elsewhere we call 'az' directly.
    #>
    if ($script:RagOsAzInvocation) { return $script:RagOsAzInvocation }
    $cmd = Get-Command az -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'Azure CLI (az) not found on PATH. Install it: https://aka.ms/installazurecli' }
    $invocation = @{ Exe = $cmd.Source; Prefix = @() }
    if ($IsWindows -and $cmd.Source -match '\.cmd$') {
        $python = Join-Path (Split-Path -Parent (Split-Path -Parent $cmd.Source)) 'python.exe'
        if (Test-Path -LiteralPath $python) {
            $env:AZ_INSTALLER = 'MSI'
            $invocation = @{ Exe = $python; Prefix = @('-X', 'utf8', '-IBm', 'azure.cli') }
        }
    }
    $script:RagOsAzInvocation = $invocation
    return $invocation
}

# ------------------------------------------------------------------------------------------------ console output
function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host ''
    Write-Host ("==> [{0:HH:mm:ss}] {1}" -f (Get-Date), $Message) -ForegroundColor Cyan
}

function Write-Info {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Message)
    Write-Host "    $Message"
}

function Write-Ok {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "    [ok] $Message" -ForegroundColor Green
}

function Write-Warn {
    <#
    .SYNOPSIS  A non-fatal problem the operator must still act on.
    .DESCRIPTION
        Deliberately aligned with [ok] and [FAIL] rather than using Write-Warning: a provisioning run is read as a
        transcript, and one indented column of [ok]/[warn]/[FAIL] can be scanned in a way that interleaved
        'WARNING:' lines cannot. Say what to do next, not only what happened.
    #>
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "    [warn] $Message" -ForegroundColor Yellow
}

function Write-Fail {
    <# .SYNOPSIS  A check that failed. Reports only - the caller decides whether to continue or throw. #>
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "    [FAIL] $Message" -ForegroundColor Red
}

# ------------------------------------------------------------------------------------------------ az wrapper
function Invoke-Az {
    <#
    .SYNOPSIS  Runs az with an argument array. Returns parsed JSON (hashtables), or trimmed text when -o/--output is given.
    .PARAMETER AllowNotFound  Return $null instead of throwing when the resource does not exist.
    .PARAMETER Sensitive      Never log the arguments (use when an argument carries a secret).
    .PARAMETER Stream         Stream output to the console (long-running builds); returns nothing.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)][string[]]$Arguments,
        [switch]$AllowNotFound,
        [switch]$Sensitive,
        [switch]$Stream
    )
    $az = Get-AzInvocation
    $azArgs = [System.Collections.Generic.List[string]]::new()
    $azArgs.AddRange([string[]]$az.Prefix)
    $azArgs.AddRange([string[]]$Arguments)
    $azArgs.Add('--only-show-errors')
    $textOutput = ($Arguments -contains '-o') -or ($Arguments -contains '--output')
    if (-not $textOutput -and -not $Stream) { $azArgs.AddRange([string[]]@('-o', 'json')) }

    $display = if ($Sensitive) { "az $(($Arguments | Select-Object -First 3) -join ' ') ... (arguments hidden)" } else { "az $($Arguments -join ' ')" }
    Write-Verbose $display

    $stdout = $null
    $exitCode = 0
    $stderr = ''
    $streamTail = ''
    $streamPauses = [System.Collections.Generic.List[object]]::new()
    for ($attempt = 1; $attempt -le $script:RagOsMaxAttempts; $attempt++) {
        $stdout = $null
        $errFile = [IO.Path]::GetTempFileName()
        $previousEap = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            if ($Stream) {
                # Everything still reaches the console, but a bounded tail is kept as well. Without it a failed
                # build threw '(no error text captured)' while the line that mattered scrolled past eighty lines
                # earlier - and Test-TransientFailure was handed an empty string, so a streamed build could
                # never be retried either. Timestamping each line costs nothing and is what lets
                # Format-OutputPauses say which step consumed the hour.
                $tail = [System.Collections.Generic.Queue[string]]::new()
                $lastAt = [datetime]::UtcNow
                $lastLine = "(start of az $(@($Arguments | Select-Object -First 2) -join ' '))"
                & $az.Exe @azArgs 2> $errFile | ForEach-Object {
                    $line = "$_"
                    $now = [datetime]::UtcNow
                    $gap = [int]($now - $lastAt).TotalSeconds
                    if ($gap -ge 30) { $streamPauses.Add([pscustomobject]@{ Seconds = $gap; After = $lastLine }) }
                    $lastAt = $now
                    if ($line.Trim()) { $lastLine = $line.Trim() }
                    Write-Host $line
                    $tail.Enqueue($line)
                    while ($tail.Count -gt 400) { [void]$tail.Dequeue() }
                }
                $exitCode = $LASTEXITCODE
                $streamTail = ($tail.ToArray() -join [Environment]::NewLine)
            }
            else {
                $stdout = & $az.Exe @azArgs 2> $errFile
                $exitCode = $LASTEXITCODE
            }
        }
        finally {
            $ErrorActionPreference = $previousEap
        }
        $stderr = Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
        if ($null -eq $stderr) { $stderr = '' }

        if ($exitCode -eq 0) { break }
        # A resource the caller said may be missing is never retried - that is an answer, not a failure.
        if ($AllowNotFound -and $stderr -match $script:RagOsNotFoundPattern) { return $null }
        $failureText = if ($Stream) { "$stderr`n$streamTail" } else { $stderr }
        $worthRetrying = if ($Stream) { Test-StreamRetryable $failureText } else { Test-TransientFailure $failureText }
        if ($attempt -ge $script:RagOsMaxAttempts -or -not $worthRetrying) { break }
        $stderr = $failureText
        $delay = $script:RagOsRetryDelays[$attempt - 1]
        $reason = (($stderr -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1) -replace '\s+', ' ').Trim()
        Write-Host "    [retry] transient az failure (attempt $attempt/$($script:RagOsMaxAttempts)), retrying in ${delay}s: $($reason.Substring(0, [Math]::Min(110, $reason.Length)))" -ForegroundColor DarkYellow
        Start-Sleep -Seconds $delay
    }

    if ($exitCode -ne 0) {
        if ($AllowNotFound -and $stderr -match $script:RagOsNotFoundPattern) { return $null }
        $full = if ($Stream) { "$stderr`n$streamTail" } else { $stderr }
        # A streamed failure's own output is the diagnosis; show the tail rather than the whole build log, which
        # has already scrolled past anyway.
        $detail = if ($Stream) {
            $meaningful = @($full -split "`r?`n" | Where-Object { $_ -match '\S' -and $_ -notmatch '^\s*(Pulling|Waiting|Download|Extracting|Verifying|Pull complete|Digest:|Status:|--->|Removing|Sending build context)' })
            if ($meaningful.Count -gt 0) { ($meaningful | Select-Object -Last 12) -join [Environment]::NewLine }
            else { '(the command produced no output)' }
        }
        elseif ($stderr.Trim()) { $stderr.Trim() }
        else { '(no error text captured; see the output above)' }
        $tried = if ($attempt -gt 1) { " after $([Math]::Min($attempt, $script:RagOsMaxAttempts)) attempts" } else { '' }
        $message = "az command failed (exit $exitCode)${tried}: $display`n$detail"
        if ($full -match $script:RagOsTimeoutPattern) {
            $message += "`n`nThis was a TIMEOUT, not a build error - the work did not finish inside the time allowed."
        }
        $hint = Get-AzFailureHint $full
        if ($hint) { $message += "`n`nLikely cause: $($hint.Cause)`nTry this:    $($hint.Fix)" }
        # @() around the call: an empty array returned through the pipeline arrives as $null, and .Count on
        # $null throws under StrictMode.
        $pauseLines = @(Format-OutputPauses -Pauses $streamPauses.ToArray())
        if ($pauseLines.Count -gt 0) { $message += "`n`n" + ($pauseLines -join [Environment]::NewLine) }
        throw $message
    }
    if ($Stream) { return }

    $text = (@($stdout) -join [Environment]::NewLine).Trim()
    if ($textOutput) { return $text }
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return (ConvertFrom-Json -InputObject $text -AsHashtable -Depth 100)
}

function Invoke-AzRest {
    <# .SYNOPSIS  az rest with a JSON body written to a temp file (never on the command line). #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('get', 'put', 'patch', 'post', 'delete')][string]$Method,
        [Parameter(Mandatory)][string]$Url,
        [object]$Body,
        [switch]$AllowNotFound,
        [switch]$Sensitive
    )
    $restArgs = @('rest', '--method', $Method, '--url', $Url)
    $bodyFile = $null
    $json = $null          # declared here so the catch can read it under Set-StrictMode
    try {
        if ($null -ne $Body) {
            $bodyFile = [IO.Path]::GetTempFileName()
            $json = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 30 -Compress }
            [IO.File]::WriteAllText($bodyFile, $json, [Text.UTF8Encoding]::new($false))
            $restArgs += @('--body', "@$bodyFile", '--headers', 'Content-Type=application/json')
        }
        return (Invoke-Az -Arguments $restArgs -AllowNotFound:$AllowNotFound -Sensitive:$Sensitive)
    }
    catch {
        # az echoes the failing command, which names the temp file - and the finally below has already deleted it,
        # so the reader is shown the path of something they cannot open. When Graph rejects a body, the body is the
        # only thing worth seeing. It is a manifest fragment, not a credential; -Sensitive is how a caller says
        # otherwise, and then nothing is added.
        if ($Sensitive -or $null -eq $json) { throw }
        throw ($_.Exception.Message + "`n`nRequest body sent:`n" + $json)
    }
    finally {
        if ($bodyFile) { Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue }
    }
}

function Get-Value {
    <#
    .SYNOPSIS  Safe nested lookup, e.g. Get-Value $account 'properties.allowProjectManagement'.
    .DESCRIPTION
        Returns $null when any level is missing. Needed because Set-StrictMode -Version Latest turns a missing key or
        property into a terminating error, and ARM omits properties that are unset (enablePurgeProtection, identity, ...).
    #>
    param([Parameter(Mandatory)][AllowNull()][object]$Object, [Parameter(Mandatory)][string]$Path)
    $current = $Object
    foreach ($part in $Path.Split('.')) {
        if ($null -eq $current) { return $null }
        if ($current -is [System.Collections.IDictionary]) { $current = $current[$part]; continue }
        $property = $current.PSObject.Properties[$part]
        $current = if ($property) { $property.Value } else { $null }
    }
    return $current
}

function Get-AzResourceOrNull {
    <# .SYNOPSIS  Runs a 'show' command; returns its result, or $null when the resource does not exist. #>
    param([Parameter(Mandatory)][string[]]$Arguments)
    return (Invoke-Az -Arguments $Arguments -AllowNotFound)
}

function Get-AzTsvValues {
    <#
    .SYNOPSIS  az with '-o tsv', as a string array with blank entries removed.
    .DESCRIPTION
        Exists because the obvious hand-written spelling is a trap. In

            Invoke-Az @('keyvault', 'secret', 'list', '-o', 'tsv') -split '[\t\r\n]+'

        PowerShell is parsing in command-argument mode, so '-split' binds as a PARAMETER NAME of Invoke-Az rather
        than as the operator. Invoke-Az is an advanced function, so it rejects it and throws; a simple function
        would instead swallow it and hand back the unsplit string. Parentheses around the call fix it - and this
        idiom was written by hand eight times and was wrong in all eight, so it lives in one place now.
        Callers keep their own @( ... ) wrapper when they need .Count, because an empty array returned through the
        pipeline arrives as $null.
    .EXAMPLE
        $names = @(Get-AzTsvValues @('keyvault', 'secret', 'list', '--vault-name', $kv, '--query', '[].name'))
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$AllowNotFound
    )
    $withFormat = if (($Arguments -contains '-o') -or ($Arguments -contains '--output')) { $Arguments } else { $Arguments + @('-o', 'tsv') }
    $text = Invoke-Az -Arguments $withFormat -AllowNotFound:$AllowNotFound
    if ([string]::IsNullOrWhiteSpace($text)) { return @() }
    return @($text -split '[\t\r\n]+' | Where-Object { $_ })
}

function Get-AzAccountOrNull {
    <#
    .SYNOPSIS  The current az context, or $null when there is none.
    .DESCRIPTION
        'az account show' is the one command that must work before anything else does, and it reports "no context"
        in at least three wordings - "There are no active accounts.", "Please run 'az login' to setup account.",
        "No subscription found." - none of which look like an ARM not-found. Classifying it by message text is
        therefore the wrong tool, and using -AllowNotFound made 00-prereqs.ps1 throw in exactly the situation it
        exists to fix. There is no partial answer here: if the command fails for any reason, there is no usable
        context, so the caller should log in. A missing CLI is a different problem and still throws.
    #>
    [CmdletBinding()]
    param([string[]]$Query = @('--query', 'id', '-o', 'tsv'))
    $null = Get-AzInvocation      # 'az is not on PATH' must not be reported as 'you are not logged in'
    $script:RagOsLastAccountError = $null
    try { return (Invoke-Az (@('account', 'show') + $Query)) }
    catch {
        # Kept so the caller can put az's own words in front of the operator. Telling someone to re-run with
        # -Verbose to find out what went wrong is the habit this whole area is being fixed for.
        $script:RagOsLastAccountError = ($_.Exception.Message -split "`r?`n" |
            Where-Object { $_ -match '\S' } | Select-Object -Last 1).Trim()
        Write-Verbose "az account show failed (treating as 'not logged in'): $($_.Exception.Message)"
        return $null
    }
}

function Test-AzResource {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $result = Get-AzResourceOrNull -Arguments $Arguments
    return ($null -ne $result -and "$result" -ne '')
}

function New-DesiredProperty {
    <#
    .SYNOPSIS  One desired setting for Sync-AzResource.
    .DESCRIPTION
        Path      where to read the current value on the resource (Get-Value syntax, e.g. 'properties.sku.name').
        Desired   the value from the psd1.
        Arg       the az update flag that sets it. Omitted for Class 'immutable'.
        ArgTemplate  how to render the value after -Arg, with {0} standing for the desired value. Needed when the
                  CLI has no dedicated flag: 'az keyvault update' cannot set a SKU, but its generic --set can, so
                  -Arg '--set' -ArgTemplate 'properties.sku.name={0}' sends '--set properties.sku.name=premium'.
                  Without it the value is passed as-is.
        Label     what to call it in the transcript. Defaults to the last path segment.
        Class     safe      - a difference is applied on every run (cheap, no downtime).
                  gated     - a difference is reported, and applied only with -ApplyChanges. Use for anything
                              that restarts a service, costs money or moves data.
                  immutable - a difference can only be fixed by recreating the resource; -Remediation says how.
        Remediation  required for 'immutable': the exact thing the operator must do.
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][AllowNull()][AllowEmptyString()]$Desired,
        [string]$Arg,
        [string]$ArgTemplate,
        [string]$Label,
        [ValidateSet('safe', 'gated', 'immutable')][string]$Class = 'safe',
        [string]$Remediation
    )
    if ($Class -eq 'immutable' -and -not $Remediation) { throw "New-DesiredProperty '$Path': an immutable property needs -Remediation saying how to change it." }
    if ($Class -ne 'immutable' -and -not $Arg) { throw "New-DesiredProperty '$Path': -Arg is required unless the property is immutable." }
    if (-not $Label) { $Label = ($Path -split '\.')[-1] }
    if ($ArgTemplate -and $ArgTemplate -notmatch '\{0\}') { throw "New-DesiredProperty '$Path': -ArgTemplate must contain {0} for the desired value." }
    return [pscustomobject]@{ Path = $Path; Desired = $Desired; Arg = $Arg; ArgTemplate = $ArgTemplate; Label = $Label; Class = $Class; Remediation = $Remediation }
}

function Test-ValueMatches {
    <#
    .SYNOPSIS  Does a resource's current value already satisfy the desired one?
    .DESCRIPTION
        az returns numbers as numbers, booleans as booleans and absent settings as $null, while the psd1 and the
        az command line deal in strings. Comparing them raw reports drift that is not there and then 'fixes' it
        on every run. So: booleans compare as booleans, anything numeric compares numerically, and everything
        else compares as a trimmed case-insensitive string. $null and '' both mean 'not set'.
    #>
    param([AllowNull()]$Current, [AllowNull()]$Desired)
    $currentUnset = ($null -eq $Current) -or ("$Current" -eq '')
    $desiredUnset = ($null -eq $Desired) -or ("$Desired" -eq '')
    if ($currentUnset -or $desiredUnset) { return ($currentUnset -and $desiredUnset) }
    if ($Desired -is [bool] -or $Current -is [bool]) { return ([bool]$Current -eq [bool]$Desired) }
    $currentNumber = 0.0
    $desiredNumber = 0.0
    if ([double]::TryParse("$Current", [ref]$currentNumber) -and [double]::TryParse("$Desired", [ref]$desiredNumber)) {
        return ($currentNumber -eq $desiredNumber)
    }
    return ("$Current".Trim() -eq "$Desired".Trim())
}

function Format-DesiredValue {
    <# .SYNOPSIS  The argument value for one spec: the desired value, or it rendered through -ArgTemplate. #>
    param([Parameter(Mandatory)]$Spec)
    if ($Spec.ArgTemplate) { return ($Spec.ArgTemplate -f "$($Spec.Desired)") }
    return "$($Spec.Desired)"
}

function Sync-AzResource {
    <#
    .SYNOPSIS  Compares a resource against the desired settings and updates only what differs. Returns the drift it found.
    .DESCRIPTION
        The counterpart to Ensure-AzResource, which only guarantees that something with that name exists. This is
        what makes the psd1 the source of truth after the first run: without it, every SKU, retention day and tag
        is a create-time-only argument that is silently ignored forever after, and the transcript still says
        '(exists)'.

        One update call carrying only the changed flags, then one line saying exactly what changed.
    .EXAMPLE
        Sync-AzResource -Description "Key Vault $kv" -Resource $vault -Update @('keyvault','update','-n',$kv,'-g',$rg) -Desired @(
            (New-DesiredProperty -Path 'properties.publicNetworkAccess' -Desired 'Enabled' -Arg '--public-network-access'),
            (New-DesiredProperty -Path 'properties.sku.name' -Desired $Config.KeyVaultSku -Arg '--sku' -Class 'gated'))
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][AllowNull()]$Resource,
        [Parameter(Mandatory)][object[]]$Desired,
        [string[]]$Update,
        [switch]$ApplyChanges,
        [switch]$Quiet
    )
    $updateArgs = @()
    $applied = @()
    $deferred = @()
    foreach ($spec in $Desired) {
        $current = Get-Value $Resource $spec.Path
        if (Test-ValueMatches -Current $current -Desired $spec.Desired) { continue }
        $shown = "$($spec.Label) $(if ("$current" -eq '') { '(unset)' } else { $current })->$($spec.Desired)"
        switch ($spec.Class) {
            'immutable' {
                Write-Warn "$Description : $($spec.Label) is '$current' but the psd1 asks for '$($spec.Desired)'. This cannot be changed in place."
                Write-Info "  $($spec.Remediation)"
            }
            'gated' {
                if ($ApplyChanges) { $updateArgs += @($spec.Arg, (Format-DesiredValue $spec)); $applied += $shown }
                else { $deferred += $shown }
            }
            default { $updateArgs += @($spec.Arg, (Format-DesiredValue $spec)); $applied += $shown }
        }
    }
    if ($deferred.Count -gt 0) {
        Write-Warn "$Description : $($deferred -join ', ') - left unchanged because these disrupt the service."
        Write-Info '  Re-run this step with -ApplyChanges to apply them.'
    }
    if ($updateArgs.Count -eq 0) {
        if (-not $Quiet) { Write-Ok "$Description (exists)" }
        return [pscustomobject]@{ Applied = @($applied); Deferred = @($deferred); Arguments = @() }
    }
    if (-not $Update) { throw "Sync-AzResource '$Description': drift was found ($($applied -join ', ')) but no -Update command was given." }
    $null = Invoke-Az ($Update + $updateArgs + @('-o', 'none'))
    Write-Ok "$Description (updated: $($applied -join ', '))"
    return [pscustomobject]@{ Applied = @($applied); Deferred = @($deferred); Arguments = @($updateArgs) }
}

function Sync-AzTags {
    <#
    .SYNOPSIS  Brings an existing resource's tags up to the configured set. No call when they already match.
    .DESCRIPTION
        Uses the generic ARM tag API rather than each resource type's own --tags flag, so one helper covers every
        resource. 'merge' adds and overwrites the configured keys and leaves anything else alone - tags set by
        policy or by another team are not ours to remove.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][AllowNull()]$Resource
    )
    if (-not $Resource) { return $false }
    $resourceId = Get-Value $Resource 'id'
    if (-not $resourceId) { return $false }
    $current = Get-Value $Resource 'tags'
    $missing = @()
    foreach ($key in $Config.AllTags.Keys) {
        # Looked up directly rather than through Get-Value: tag names are a flat map and may legitimately contain
        # a dot, which Get-Value would read as a path separator and never find.
        $have = $null
        if ($current -is [System.Collections.IDictionary]) { $have = $current[$key] }
        elseif ($current) { $have = $current.PSObject.Properties[$key]?.Value }
        if (-not (Test-ValueMatches -Current $have -Desired $Config.AllTags[$key])) { $missing += "$key=$($Config.AllTags[$key])" }
    }
    if ($missing.Count -eq 0) { return $false }
    $null = Invoke-Az (@('tag', 'update', '--resource-id', $resourceId, '--operation', 'merge', '--tags') + $missing + @('-o', 'none'))
    Write-Ok "$Description tags (updated: $($missing -join ', '))"
    return $true
}

function Ensure-AzResource {
    <#
    .SYNOPSIS  Show-then-create, then reconcile. Returns the resource (output of -Show).
    .DESCRIPTION
        -Desired/-Update are optional and additive: without them this is the original show-then-create and every
        argument in -Create is applied once, at creation, and never again. With them, an existing resource is
        compared against the desired settings and only the differences are updated. See Sync-AzResource.
        -SyncTags brings the configured tags to an existing resource, which -Create cannot do.
    .EXAMPLE   Ensure-AzResource -Description "identity $mi" -Show @('identity','show','-n',$mi,'-g',$rg) -Create @('identity','create','-n',$mi,'-g',$rg)
    #>
    param(
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][string[]]$Show,
        [Parameter(Mandatory)][string[]]$Create,
        [object[]]$Desired,
        [string[]]$Update,
        [hashtable]$Config,
        [switch]$SyncTags,
        [switch]$ApplyChanges,
        [switch]$Sensitive
    )
    $existing = Get-AzResourceOrNull -Arguments $Show
    if ($null -ne $existing) {
        $changed = $false
        if ($SyncTags) {
            if (-not $Config) { throw "Ensure-AzResource '$Description': -SyncTags needs -Config." }
            $changed = Sync-AzTags -Description $Description -Config $Config -Resource $existing
        }
        if ($Desired) {
            $drift = Sync-AzResource -Description $Description -Resource $existing -Desired $Desired -Update $Update -ApplyChanges:$ApplyChanges -Quiet:$changed
            if ($drift.Applied.Count -gt 0) { return (Invoke-Az -Arguments $Show) }
            return $existing
        }
        if (-not $changed) { Write-Ok "$Description (exists)" }
        return $existing
    }
    Write-Info "Creating $Description ..."
    $null = Invoke-Az -Arguments $Create -Sensitive:$Sensitive
    $created = Invoke-Az -Arguments $Show
    Write-Ok "$Description (created)"
    return $created
}

function Format-ReadyzReasons {
    <#
    .SYNOPSIS  /api/readyz's JSON body as one line per check: 'state_db: ok', 'embedding_profile: <reasons>'.
    .DESCRIPTION
        readyz answers "why not" in a structured body; interpolating the whole blob into a single line buried the
        one sentence that mattered. Returns an array so a wait can join it and a failure report can list it.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Content, [int]$StatusCode = 0)
    $lines = @()
    try {
        $body = ($Content -replace '^HTTP \d+:\s*', '') | ConvertFrom-Json
        foreach ($name in $body.checks.PSObject.Properties.Name) {
            $check = $body.checks.$name
            # Scalars (state_db, llm_answer) carry no properties, and under StrictMode asking one for a property
            # it does not have is a terminating error - which the catch below would then hide, turning the whole
            # body into the raw-text fallback. So establish which shape this is before touching anything.
            # Assigned as plain statements, not `$x = if (...) { @() }`: an empty array returned out of an
            # if-expression arrives as $null, and $null.Count is a terminating error under StrictMode.
            $props = @()
            if ($check -is [psobject] -and $check -isnot [string]) { $props = @($check.PSObject.Properties.Name) }
            $reasons = @()
            if ($props -contains 'reasons') { $reasons = @($check.reasons) }
            $detail =
            if ($props.Count -eq 0) { "$check" }
            elseif ($reasons.Count -gt 0) { $reasons -join '; ' }
            elseif ($props -contains 'ok' -and $check.ok) { 'ok' }
            # Not ok, and nothing to say about why, is worth naming as such rather than printing a bare 'ok=False'.
            elseif ($props -contains 'ok') { 'not ok (no reasons reported)' }
            else { "$check" }
            # The index and its fingerprint are already in the body, but were only ever read on the SUCCESS path -
            # so a 503 never said which index the verdict was about, the one fact needed to act on it. Skipped
            # when a reason already names the index, which the index-state reasons do.
            if ($props -contains 'index' -and $check.index -and $detail -notlike "*$($check.index)*") {
                $detail += " [index $($check.index), profile $($check.fingerprint)]"
            }
            $lines += "${name}: $detail"
        }
    }
    catch {
        # Left non-fatal: the fallback below still reports the raw body, which is what matters. Recorded so a
        # malformed body is distinguishable from an empty one when someone runs with -Verbose.
        Write-Verbose "readyz body is not the expected JSON: $($_.Exception.Message)"
    }
    if ($lines.Count -eq 0) {
        $short = "$Content".Trim()
        if ($short.Length -gt 160) { $short = $short.Substring(0, 157) + '...' }
        $lines = @($(if ($short) { "HTTP ${StatusCode}: $short" } else { "HTTP $StatusCode" }))
    }
    return $lines
}

function Wait-Until {
    <#
    .SYNOPSIS  Polls a condition until it is true, narrating what it is still waiting for.
    .DESCRIPTION
        Distinct from Invoke-WithRetry, which retries something that ERRORED. This waits for something that is
        legitimately not ready yet - a 3 GB image still pulling, a model still loading, a role still replicating -
        where the right behaviour is patience, not a retry.

        -Condition returns @{ Ok = <bool>; Detail = '<what it is waiting for>' }. A line is printed only when
        Detail CHANGES, which is what makes a ten-minute wait readable: a handful of lines showing the thing
        converge, rather than forty identical ones or forty seconds of silence.
    .OUTPUTS
        @{ Ok; Detail; Elapsed } - the final state, so the caller decides whether to warn, throw, or carry on.
    .EXAMPLE
        Wait-Until -Activity 'rag-api /api/readyz' -TimeoutMinutes 10 -Condition {
            $r = Invoke-WebRequest -Uri $url -TimeoutSec 30 -SkipHttpErrorCheck
            @{ Ok = ($r.StatusCode -eq 200); Detail = "HTTP $($r.StatusCode)" }
        }
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Activity,
        [Parameter(Mandatory)][scriptblock]$Condition,
        [int]$TimeoutMinutes = 10,
        [int]$IntervalSeconds = 15,
        # What Ok means for this caller. A job wait is satisfied by a TERMINAL state, which may well be 'Failed',
        # so announcing it as 'ready' would be a lie; it passes -ReadyLabel 'finished' and judges the status itself.
        [string]$ReadyLabel = 'ready'
    )
    $started = Get-Date
    $deadline = $started.AddMinutes($TimeoutMinutes)
    $lastDetail = $null
    $detail = ''
    while ($true) {
        try {
            $state = & $Condition
            $detail = "$($state.Detail)"
            if ($state.Ok) {
                Write-Ok "$Activity $ReadyLabel after $(Format-Elapsed (Get-Date).Subtract($started))"
                return @{ Ok = $true; Detail = $detail; Elapsed = (Get-Date).Subtract($started) }
            }
        }
        catch {
            # A condition that throws is just another way of saying "not yet" - a service that is still starting
            # refuses connections rather than answering politely.
            $detail = ($_.Exception.Message -split "`r?`n" | Where-Object { $_ -match '\S' } | Select-Object -First 1)
        }
        if ($detail -ne $lastDetail) {
            Write-Host "    [wait] $Activity : $detail" -ForegroundColor DarkYellow
            $lastDetail = $detail
        }
        if ((Get-Date) -ge $deadline) {
            return @{ Ok = $false; Detail = $detail; Elapsed = (Get-Date).Subtract($started) }
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
}

function Format-Elapsed {
    <# .SYNOPSIS  A timespan as '4m10s' - short enough to sit inside a status line. #>
    param([Parameter(Mandatory)][timespan]$Span)
    if ($Span.TotalMinutes -ge 1) { return "$([int]$Span.TotalMinutes)m$($Span.Seconds)s" }
    return "$([int]$Span.TotalSeconds)s"
}

function Wait-ContainerAppReady {
    <#
    .SYNOPSIS  Waits until a container app has at least -MinReplicas replicas with every container ready.
    .DESCRIPTION
        "0/2 replicas ready" distinguishes an image still pulling from one that is running and refusing - a
        distinction an HTTP probe cannot make, because both look like a connection that goes nowhere. The
        embedder pools are the reason this exists: a ~3 GB image plus a model load is minutes, so the first
        several minutes of a fresh deployment are a wait, not a fault.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$ResourceGroup,
        [Parameter(Mandatory)][string]$Name,
        [int]$MinReplicas = 1,
        [int]$TimeoutMinutes = 10
    )
    return Wait-Until -Activity "$Name replicas" -TimeoutMinutes $TimeoutMinutes -Condition {
        $replicas = @(Invoke-Az @('containerapp', 'replica', 'list', '-g', $ResourceGroup, '-n', $Name,
                '--query', '[].{name:name, containers:properties.containers[].ready}') -AllowNotFound)
        $ready = @($replicas | Where-Object { $_ -and (@($_.containers) -notcontains $false) -and @($_.containers).Count -gt 0 })
        @{ Ok = ($ready.Count -ge $MinReplicas); Detail = "$($ready.Count)/$($replicas.Count) replicas ready" }
    }
}

function Invoke-WithRetry {
    <#
    .SYNOPSIS  Runs a script block, retrying when the error message matches -RetryOn (linear backoff, capped at 60 s).
    #>
    param(
        [Parameter(Mandatory)][scriptblock]$ScriptBlock,
        [int]$MaxAttempts = 8,
        [int]$DelaySeconds = 10,
        [string]$Activity = 'operation',
        [string]$RetryOn = '.*'
    )
    for ($retryAttempt = 1; ; $retryAttempt++) {
        try {
            return (& $ScriptBlock)
        }
        catch {
            $retryMessage = $_.Exception.Message
            if ($retryAttempt -ge $MaxAttempts -or $retryMessage -notmatch $RetryOn) { throw }
            $retryWait = [Math]::Min($DelaySeconds * $retryAttempt, 60)
            $firstLine = (($retryMessage -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1) -replace '\s+', ' ').Trim()
            # [wait], not [retry]: this layer is waiting for something to become true (a role to replicate, a
            # resource to leave a transitional state), which is why its budget is minutes rather than seconds.
            Write-Host "    [wait] $Activity not ready yet (attempt $retryAttempt/$MaxAttempts), retrying in ${retryWait}s: $($firstLine.Substring(0, [Math]::Min(110, $firstLine.Length)))" -ForegroundColor DarkYellow
            Start-Sleep -Seconds $retryWait
        }
    }
}

# ------------------------------------------------------------------------------------------------ identity + RBAC
function Get-DeployerPrincipal {
    <# .SYNOPSIS  The signed-in principal: @{ ObjectId; PrincipalType ('User'|'ServicePrincipal'); Name }. Cached. #>
    if ($script:RagOsDeployer) { return $script:RagOsDeployer }
    $account = Invoke-Az @('account', 'show', '--query', '{type:user.type, name:user.name}')
    if ($account.type -eq 'user') {
        $me = Invoke-Az @('ad', 'signed-in-user', 'show', '--query', '{id:id, upn:userPrincipalName}')
        $script:RagOsDeployer = @{ ObjectId = $me.id; PrincipalType = 'User'; Name = $me.upn }
    }
    else {
        $sp = Invoke-Az @('ad', 'sp', 'show', '--id', $account.name, '--query', '{id:id, name:displayName}')
        $script:RagOsDeployer = @{ ObjectId = $sp.id; PrincipalType = 'ServicePrincipal'; Name = $sp.name }
    }
    if (-not $script:RagOsDeployer.ObjectId) { throw 'Could not resolve the signed-in principal object id (needs Entra permission to read the signed-in user).' }
    return $script:RagOsDeployer
}

function Get-DeployerObjectId {
    return (Get-DeployerPrincipal).ObjectId
}

function Grant-Role {
    <# .SYNOPSIS  Idempotent 'az role assignment create --assignee-object-id --assignee-principal-type' with propagation retries. #>
    param(
        [Parameter(Mandatory)][string]$PrincipalId,
        [Parameter(Mandatory)][ValidateSet('ServicePrincipal', 'User', 'Group', 'ForeignGroup')][string]$PrincipalType,
        [Parameter(Mandatory)][string]$Role,
        [Parameter(Mandatory)][string]$Scope,
        [string]$PrincipalLabel = $PrincipalId
    )
    $scopeName = ($Scope.TrimEnd('/') -split '/')[-1]
    $count = Invoke-Az @('role', 'assignment', 'list', '--assignee-object-id', $PrincipalId, '--role', $Role, '--scope', $Scope,
        '--fill-principal-name', 'false', '--query', 'length(@)', '-o', 'tsv')
    if ([int]("0$count".Trim()) -gt 0) {
        Write-Ok "'$Role' -> $PrincipalLabel on $scopeName (exists)"
        return
    }
    Invoke-WithRetry -Activity "Assign '$Role' to $PrincipalLabel" -MaxAttempts 10 -DelaySeconds 10 `
        -RetryOn '(?i)(PrincipalNotFound|does not exist in the directory|replicat|InvalidPrincipalId)' -ScriptBlock {
        try {
            $null = Invoke-Az @('role', 'assignment', 'create', '--assignee-object-id', $PrincipalId, '--assignee-principal-type', $PrincipalType,
                '--role', $Role, '--scope', $Scope, '--query', 'id', '-o', 'tsv')
        }
        catch {
            if ($_.Exception.Message -notmatch '(?i)(RoleAssignmentExists|already exists)') { throw }
        }
    }
    Write-Ok "'$Role' -> $PrincipalLabel on $scopeName"
}

# --------------------------------------------------------------------------------- Entra app registration (sign-in)
# These shape the Microsoft Graph body for the app registration people sign in through. They are pure - no az call,
# no state - because every interesting failure here is in the body, and a pure function is the only way to test one
# without a directory to write to. Set-EntraAppRegistration.ps1 does the reading and the writing.
#
# Two spellings of the same manifest exist and mixing them fails SILENTLY. This code talks to
# graph.microsoft.com/v1.0, so it must use the Graph names:
#     api.oauth2PermissionScopes                             not  oauth2Permissions
#     api.preAuthorizedApplications[].delegatedPermissionIds not  permissionIds
# The right-hand spellings belong to the Azure AD Graph / portal Manifest blade. Graph accepts a body carrying them
# and discards the value, which is indistinguishable from success.

# The application roles RAG-OS understands. `Value` is what lands in the token's `roles` claim, and
# config/access-policy/access-policy.yaml maps each one to an internal role (rag.admin -> admin + contributor,
# rag.sme -> taxonomy_editor + reviewer, and so on). Without at least one of these assigned to somebody, a
# deployment has no administrator and nobody can upload: that is a 403 at first use and nothing earlier.
#
# Held here rather than parsed out of the YAML because PowerShell has no built-in YAML reader, and a hand-rolled
# parser for one list would be a worse liability than this list. tests/unit/test_infra_entra_app.py asserts the
# two agree, so they cannot drift.
$script:RagOsEntraAppRoles = @(
    @{ Value = 'rag.admin'; DisplayName = 'RAG-OS administrator'
        Description = 'Full administration: ingestion, configuration and the review queue. Also bypasses the ' +
        'document access filter, so an administrator sees every document whatever its department, region or ' +
        'clearance - grant it deliberately.'
    }
    @{ Value = 'rag.contributor'; DisplayName = 'RAG-OS contributor'
        Description = 'May upload documents. What they can read stays filtered by the department, region and ' +
        'clearance their account carries.'
    }
    @{ Value = 'rag.sme'; DisplayName = 'RAG-OS subject-matter expert'
        Description = 'May edit the taxonomy - facets and path rules - and work the document review queue.'
    }
    @{ Value = 'rag.reviewer'; DisplayName = 'RAG-OS reviewer'
        Description = 'May work the document review queue and approve tags.'
    }
)

function Get-MigrationHead {
    <#
    .SYNOPSIS
        The Alembic revision this checkout would deploy, read from migrations/versions.
    .DESCRIPTION
        The head is the revision no other migration names as its down_revision. Computed from the files rather
        than by running Alembic, because this runs on an operator's machine that has no database connection and
        may have no Python environment.

        Returns $null when the directory is missing or the chain cannot be read - the caller then skips its
        check rather than blocking a deploy on a parsing problem.
    #>
    param([Parameter(Mandatory)][string]$RepoRoot)

    $dir = Join-Path $RepoRoot 'migrations/versions'
    if (-not (Test-Path -LiteralPath $dir)) { return $null }
    $revisions = @{}
    $parents = @{}
    foreach ($file in Get-ChildItem -LiteralPath $dir -Filter '*.py') {
        $text = Get-Content -LiteralPath $file.FullName -Raw
        $rev = [regex]::Match($text, "(?m)^revision:\s*str\s*=\s*'([^']+)'")
        if (-not $rev.Success) { continue }
        $revisions[$rev.Groups[1].Value] = $file.Name
        $down = [regex]::Match($text, "(?m)^down_revision:\s*str \| None\s*=\s*'([^']+)'")
        if ($down.Success) { $parents[$down.Groups[1].Value] = $true }
    }
    if ($revisions.Count -eq 0) { return $null }
    $heads = @($revisions.Keys | Where-Object { -not $parents.ContainsKey($_) })
    # Exactly one head, or the chain has branched and a script is the wrong place to resolve that.
    if ($heads.Count -ne 1) { return $null }
    return $heads[0]
}

function Test-SchemaUpToDate {
    <#
    .SYNOPSIS
        Refuse to roll out an image whose migrations have not been applied.
    .DESCRIPTION
        The failure this prevents: step 07 deploys a new image and redefines the rag-bootstrap job WITHOUT
        starting it, so an image carrying a new migration runs against the old schema. Because SQLAlchemy emits
        the column list from the code's table metadata, one missing column breaks every full-row read of that
        table - the whole Upload and Documents surface returning 500 while chat keeps working, with healthy
        probes. It has happened.

        The database revision comes from the RUNNING api's /api/readyz, so this script needs no database
        connectivity of its own and 07 keeps its contract of never touching the database.

        Returns $true when it is safe to proceed, including every case it cannot determine: a first deploy has
        no API to ask, and an older image does not report its schema. Blocking on an unknown would make the
        check worse than the bug.
    #>
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [Parameter(Mandatory)][string]$RepoRoot,
        [string]$Env = 'dev'
    )

    $head = Get-MigrationHead -RepoRoot $RepoRoot
    if (-not $head) {
        Write-Info 'Could not read the migration head from migrations/versions; skipping the schema pre-flight.'
        return $true
    }
    try {
        $response = Invoke-WebRequest -Uri "$BaseUrl/api/readyz" -TimeoutSec 30 -SkipHttpErrorCheck
        $body = $response.Content | ConvertFrom-Json
    }
    catch {
        Write-Info "No running API to ask about the schema ($($_.Exception.Message.Split([char]10)[0]))."
        Write-Info '  Skipping the pre-flight - a first deploy has nothing to be behind.'
        return $true
    }
    $current = $body.checks.schema.current
    if (-not $current) {
        Write-Info 'The running API does not report its schema revision (an older image); skipping the pre-flight.'
        return $true
    }
    if ($current -eq $head) {
        Write-Ok "Database schema is at $head, which this checkout expects."
        return $true
    }
    Write-Fail "The database schema is behind what this checkout deploys."
    Write-Info "  database is at : $current"
    Write-Info "  this build wants: $head"
    Write-Info ''
    Write-Info '  Deploying now would leave every query against the changed tables failing with a 500.'
    Write-Info '  Apply the migrations first, then re-run this script:'
    Write-Info "    ./infra/scripts/08-bootstrap.ps1 -Env $Env"
    Write-Info '  Or re-run with -SkipSchemaCheck if you intend the image to go out ahead of the migration.'
    return $false
}

function Get-EntraScopeName {
    <#
    .SYNOPSIS  The scope name out of a scope identifier: api://<app-id>/access_as_user -> access_as_user.
    .DESCRIPTION
        The name is hardcoded nowhere in RAG-OS - it is whatever EntraApiScope says after the last '/', so renaming
        the scope is a psd1 edit and nothing else. Returns $null when the identifier carries no name, which is the
        case worth catching: 'api://<app-id>' on its own is a resource, not a scope, and would otherwise yield the
        app id as a scope name and go on to create a scope called after a GUID.
    #>
    param([Parameter(Mandatory)][AllowNull()][AllowEmptyString()][string]$Scope)
    if ([string]::IsNullOrWhiteSpace($Scope)) { return $null }
    $sep = $Scope.IndexOf('://')
    if ($sep -lt 0) { return $null }
    $parts = @(($Scope.Substring($sep + 3).Trim('/')) -split '/' | Where-Object { $_ })
    if ($parts.Count -lt 2) { return $null }   # resource only, no scope segment
    return $parts[-1]
}

function Get-EntraScopeResource {
    <# .SYNOPSIS  The resource half of a scope identifier: api://<app-id>/access_as_user -> api://<app-id>. #>
    param([Parameter(Mandatory)][AllowNull()][AllowEmptyString()][string]$Scope)
    if (-not (Get-EntraScopeName -Scope $Scope)) { return $null }
    return $Scope.TrimEnd('/').Substring(0, $Scope.TrimEnd('/').LastIndexOf('/'))
}

function Add-UniqueValue {
    <#
    .SYNOPSIS  Case-insensitive union of one value into a list. Returns @{ Values; Added }.
    .DESCRIPTION
        Values is ALWAYS wrapped in @(), which is load-bearing rather than tidy: ConvertTo-Json renders a
        one-element collection that came off the pipeline as a bare scalar, so a single redirect URI would reach
        Graph as "https://..." instead of ["https://..."] and be rejected or silently mis-stored.
    #>
    param([AllowNull()][string[]]$Existing, [Parameter(Mandatory)][string]$Value)
    $list = @($Existing | Where-Object { $_ })
    if (@($list | Where-Object { $_ -eq $Value }).Count -gt 0) { return @{ Values = $list; Added = $false } }
    return @{ Values = @($list + $Value); Added = $true }
}

function Get-EntraAppPatch {
    <#
    .SYNOPSIS
        The Graph PATCH body that brings an app registration in line for sign-in, plus what it would change.
    .DESCRIPTION
        Read-modify-write, and that is the whole reason this is a function rather than one az call. A Graph PATCH
        REPLACES a complex property outright: sending { api: { oauth2PermissionScopes: [...] } } deletes
        knownClientApplications, acceptMappedClaims, every other exposed scope and every pre-authorised client on
        that application. So the existing object is the starting value, and only top-level keys that actually
        differ are emitted at all.

        Returns @{ Body; Changes; ScopeId; ScopeExisted; PreAuthDeferred }. An EMPTY Changes list means the
        registration is already correct and the caller must not PATCH: re-sending an identical body would report
        success whether or not this function computed anything sensible, which is exactly the bug a dry run is
        supposed to expose.

        INVARIANT: the body never references a scope id in preAuthorizedApplications unless that scope already
        exists in the directory - see the ordering note in the pre-auth block below. PreAuthDeferred says a second
        call is wanted once the scope has been written, and the caller is expected to make it.
    .PARAMETER App
        The application object as `az ad app show` returns it - the Graph shape, parsed to hashtables.
    .PARAMETER PreAuthorizeAppIds
        Client app ids to pre-authorise IN ADDITION to the app itself, so their users are never asked to consent.
    .PARAMETER AppRoles
        Application role values to expose, from $script:RagOsEntraAppRoles. Roles carry no ordering constraint
        against the scope, so they go in the same write.
    #>
    param(
        [Parameter(Mandatory)][object]$App,
        [Parameter(Mandatory)][string]$ClientId,
        [Parameter(Mandatory)][string]$ScopeName,
        [Parameter(Mandatory)][string]$AppIdUri,
        [string[]]$RedirectUris,
        [string[]]$PreAuthorizeAppIds,
        [string[]]$AppRoles,
        [int]$AccessTokenVersion = 2
    )
    $changes = [System.Collections.Generic.List[object]]::new()
    $body = [ordered]@{}

    # Graph omits anything unset, and Set-StrictMode -Version Latest turns a missing key into a terminating error,
    # so every read goes through Get-Value. An app that has never exposed a scope has no 'api' key at all.
    $origUris = @(Get-Value $App 'identifierUris')
    $origApi = Get-Value $App 'api'
    $origScopes = @(Get-Value $App 'api.oauth2PermissionScopes')
    $origPreAuth = @(Get-Value $App 'api.preAuthorizedApplications')
    $origVersion = Get-Value $App 'api.requestedAccessTokenVersion'
    $origSpaUris = @(Get-Value $App 'spa.redirectUris')

    # ---- identifierUris. Without the app id URI the scope identifier resolves to no resource at all and Entra
    # answers AADSTS500011 (resource principal not found) rather than 65005 - a different hunt entirely.
    $uris = Add-UniqueValue -Existing $origUris -Value $AppIdUri
    if ($uris.Added) {
        $body['identifierUris'] = $uris.Values
        $changes.Add([pscustomobject]@{ What = 'identifierUris'
                Before = (($origUris -join ', ') -replace '^$', '(none)'); After = ($uris.Values -join ', ') })
    }

    # ---- the scope. Its absence is precisely what AADSTS65005 reports.
    $scope = @($origScopes | Where-Object { $_ -and ([string](Get-Value $_ 'value')) -eq $ScopeName }) |
        Select-Object -First 1
    $scopeId = if ($scope -and (Get-Value $scope 'id')) { [string](Get-Value $scope 'id') } else { [guid]::NewGuid().Guid }
    $scopeChanged = $false
    if ($scope) {
        # Keep the operator's consent wording and everything else they set; force only the two fields that decide
        # whether the scope can actually be requested. Rewriting the text would report a change on every run.
        $desiredScope = [ordered]@{}
        if ($scope -is [System.Collections.IDictionary]) {
            foreach ($key in $scope.Keys) { $desiredScope[$key] = $scope[$key] }
        }
        else { foreach ($prop in $scope.PSObject.Properties) { $desiredScope[$prop.Name] = $prop.Value } }
        $wasEnabled = [bool](Get-Value $scope 'isEnabled')
        $wasType = [string](Get-Value $scope 'type')
        $desiredScope['isEnabled'] = $true
        $desiredScope['type'] = 'User'
        if (-not $wasEnabled) {
            $scopeChanged = $true
            $changes.Add([pscustomobject]@{ What = "scope '$ScopeName' isEnabled"; Before = 'false'; After = 'true' })
        }
        if ($wasType -ne 'User') {
            $scopeChanged = $true
            $changes.Add([pscustomobject]@{ What = "scope '$ScopeName' type"
                    Before = (($wasType) ? $wasType : '(unset)'); After = 'User' })
        }
    }
    else {
        $scopeChanged = $true
        $desiredScope = [ordered]@{
            id                      = $scopeId
            value                   = $ScopeName
            type                    = 'User'
            isEnabled               = $true
            adminConsentDisplayName = 'Access RAG-OS as the signed-in user'
            adminConsentDescription = 'Allows the chat UI to call the RAG-OS API on behalf of the signed-in user. ' +
            "The caller's own attributes decide what the search returns; this permission grants no extra access."
            userConsentDisplayName  = 'Access RAG-OS on your behalf'
            userConsentDescription  = 'Allows the assistant to search the knowledge base as you, returning only ' +
            'documents your permissions already allow.'
        }
        $changes.Add([pscustomobject]@{ What = "scope '$ScopeName'"; Before = '(not exposed)'
                After = "exposed, id $scopeId" })
    }
    $desiredScopes = @()
    $replaced = $false
    foreach ($existing in $origScopes) {
        if (-not $existing) { continue }
        if (([string](Get-Value $existing 'value')) -eq $ScopeName) { $desiredScopes += , $desiredScope; $replaced = $true }
        else { $desiredScopes += , $existing }   # another API's scope on the same app - must survive
    }
    if (-not $replaced) { $desiredScopes += , $desiredScope }

    # ---- pre-authorised clients, so nobody is shown a consent prompt for our own UI.
    #
    # ORDERING CONSTRAINT, and the reason this is the awkward part of the function. Graph validates
    # preAuthorizedApplications against the permission set ALREADY PERSISTED on the application - NOT against the
    # oauth2PermissionScopes in the same request body. Pre-authorising a scope this body is creating is answered
    # with HTTP 400:
    #     InvalidValue: Property api.preAuthorizedApplications.delegatedPermissionIds has a Permission Id
    #                   that cannot be found in the AppPermissions sets.
    # and that rejection is atomic, so it takes the new scope down with it - the one thing that had to land.
    #
    # So when the scope does not exist yet the pre-authorisation is DEFERRED. The existing entries are carried
    # through untouched (dropping the key would delete them, since the whole `api` object is replaced), and the
    # caller writes the scope, re-reads the application and calls this function again. That second call sees the
    # scope and uses the id Graph actually persisted rather than one minted here.
    $preAuthMap = [ordered]@{}
    foreach ($entry in $origPreAuth) {
        if (-not $entry) { continue }
        $entryAppId = [string](Get-Value $entry 'appId')
        if (-not $entryAppId) { continue }
        $preAuthMap[$entryAppId] = @(@(Get-Value $entry 'delegatedPermissionIds') | Where-Object { $_ })
    }
    $preAuthChanged = $false
    $preAuthDeferred = $false
    $wantedClients = @(@($ClientId) + @($PreAuthorizeAppIds) | Where-Object { $_ } | Select-Object -Unique)
    if (-not $scope) {
        # $scopeId was minted moments ago, so by definition no existing entry can already carry it: every wanted
        # client needs the second write. Listed in Changes anyway, so the operator is not left wondering why a
        # change they asked for is absent from the diff.
        $preAuthDeferred = $wantedClients.Count -gt 0
        foreach ($wanted in $wantedClients) {
            $changes.Add([pscustomobject]@{ What = "pre-authorise $wanted"
                    Before = ($preAuthMap.Contains($wanted) ? (@($preAuthMap[$wanted]) -join ', ') : '(not listed)')
                    After = 'deferred to a second write - Graph needs the scope to exist first' })
        }
    }
    else {
        foreach ($wanted in $wantedClients) {
            $had = $preAuthMap.Contains($wanted)
            $union = Add-UniqueValue -Existing ($had ? @($preAuthMap[$wanted]) : @()) -Value $scopeId
            if (-not $had -or $union.Added) {
                $preAuthChanged = $true
                $changes.Add([pscustomobject]@{ What = "pre-authorise $wanted"
                        Before = ($had ? (@($preAuthMap[$wanted]) -join ', ') : '(not listed)'); After = ($union.Values -join ', ') })
            }
            $preAuthMap[$wanted] = $union.Values
        }
    }
    $desiredPreAuth = @(foreach ($appKey in $preAuthMap.Keys) {
            @{ appId = $appKey; delegatedPermissionIds = @($preAuthMap[$appKey]) }
        })

    # ---- access token version. null means 1, and 1 means iss=https://sts.windows.net/<tid>/ with aud set to the
    # requested resource URI. 2 means iss=.../v2.0 with aud set to the bare app id. The API has to expect whichever
    # one this says, which is why it is set explicitly rather than left to the default.
    $versionChanged = ([string]$origVersion -ne [string]$AccessTokenVersion)
    if ($versionChanged) {
        $changes.Add([pscustomobject]@{ What = 'api.requestedAccessTokenVersion'
                Before = (($null -eq $origVersion -or "$origVersion" -eq '') ? 'null (means 1)' : "$origVersion")
                After = "$AccessTokenVersion" })
    }

    if ($scopeChanged -or $preAuthChanged -or $versionChanged) {
        # Start from the existing api object so knownClientApplications and acceptMappedClaims survive the replace.
        $api = [ordered]@{}
        if ($origApi -is [System.Collections.IDictionary]) {
            foreach ($key in $origApi.Keys) { $api[$key] = $origApi[$key] }
        }
        $api['oauth2PermissionScopes'] = @($desiredScopes)
        $api['preAuthorizedApplications'] = @($desiredPreAuth)
        $api['requestedAccessTokenVersion'] = $AccessTokenVersion
        $body['api'] = $api
    }

    # ---- application roles. These are what the `roles` claim carries; access-policy.yaml turns each value into
    # an internal role. Nothing else in the deployment creates them, and without one nobody can upload or reach
    # the admin console. Unlike the pre-authorisation there is no ordering constraint here, so they ride along.
    $origAppRoles = @(Get-Value $App 'appRoles')
    $existingRoleValues = @($origAppRoles | ForEach-Object { [string](Get-Value $_ 'value') })
    $desiredAppRoles = @()
    foreach ($existing in $origAppRoles) {
        if (-not $existing) { continue }
        # `origin` is READ-ONLY and Graph rejects any write that carries it back ("must not be included in any
        # POST or PATCH requests"). Echoing what was read is the obvious implementation and it fails with a 400
        # naming the whole collection rather than the offending key, so the copy drops it deliberately.
        $copy = [ordered]@{}
        if ($existing -is [System.Collections.IDictionary]) {
            foreach ($key in $existing.Keys) { if ($key -ne 'origin') { $copy[$key] = $existing[$key] } }
        }
        else {
            foreach ($prop in $existing.PSObject.Properties) {
                if ($prop.Name -ne 'origin') { $copy[$prop.Name] = $prop.Value }
            }
        }
        $desiredAppRoles += , $copy
    }
    $rolesAdded = @()
    foreach ($wantedRole in @($AppRoles | Where-Object { $_ })) {
        $spec = @($script:RagOsEntraAppRoles | Where-Object { $_.Value -eq $wantedRole }) | Select-Object -First 1
        if (-not $spec) {
            throw ("Unknown application role '$wantedRole'. RAG-OS understands only: " +
                "$(($script:RagOsEntraAppRoles | ForEach-Object { $_.Value }) -join ', '). A role this policy " +
                'does not map gates nothing - see access-policy.yaml.')
        }
        # Graph's rules for the value, which becomes the claim: no whitespace, and it may not begin with a dot.
        if ($spec.Value -match '\s' -or $spec.Value.StartsWith('.')) {
            throw "Application role value '$($spec.Value)' is not legal: no whitespace, and it may not start with '.'."
        }
        if ($existingRoleValues -contains $spec.Value) { continue }
        $desiredAppRoles += , ([ordered]@{
                id                 = [guid]::NewGuid().Guid
                value              = $spec.Value
                displayName        = $spec.DisplayName
                description        = $spec.Description
                allowedMemberTypes = @('User')
                isEnabled          = $true
            })
        $rolesAdded += $spec.Value
    }
    if ($rolesAdded.Count -gt 0) {
        $body['appRoles'] = @($desiredAppRoles)
        $changes.Add([pscustomobject]@{ What = 'appRoles'
                Before = ((($existingRoleValues -join ', ')) -replace '^$', '(none)')
                After = (@($desiredAppRoles | ForEach-Object { [string]$_['value'] }) -join ', ') })
    }

    # ---- SPA redirect URIs. Replacing 'spa' wholesale is safe only because spaApplication has exactly one
    # property; the URI list itself is still a union, so a localhost entry someone added by hand is kept.
    $spaUris = @($origSpaUris)
    $spaAdded = @()
    foreach ($uri in @($RedirectUris | Where-Object { $_ })) {
        $union = Add-UniqueValue -Existing $spaUris -Value $uri
        $spaUris = $union.Values
        if ($union.Added) { $spaAdded += $uri }
    }
    if ($spaAdded.Count -gt 0) {
        $body['spa'] = @{ redirectUris = @($spaUris) }
        $changes.Add([pscustomobject]@{ What = 'spa.redirectUris'
                Before = (($origSpaUris -join ', ') -replace '^$', '(none)'); After = ($spaUris -join ', ') })
    }

    return @{ Body = $body; Changes = @($changes); ScopeId = $scopeId; ScopeExisted = [bool]$scope
        PreAuthDeferred = $preAuthDeferred }
}

function Get-EntraAppChecks {
    <#
    .SYNOPSIS
        Read-only verdicts on whether an app registration can actually sign anybody in. Pure - no az call.
    .DESCRIPTION
        Separated from 00-prereqs.ps1 so it can be tested against an app object without a directory, which is the
        only reason any of this is covered: the failure it exists to catch (a scope that was never exposed) used
        to surface nowhere but at a user's sign-in, long after provisioning had reported success.

        Returns a list of @{ Item; Status; Detail } in the shape 00-prereqs.ps1's Add-Check takes. FAIL means no
        one can sign in; WARN means something is imperfect but sign-in works, or will once a later step runs.
    .PARAMETER App
        The application object as `az ad app show` returns it, or $null when it does not exist.
    .PARAMETER ChatUiFqdn
        The deployed chat UI host, when one is known. Omit before step 07 - the redirect URI check is then skipped
        rather than reported as missing.
    .PARAMETER AdminAssignments
        How many people hold rag.admin. -1 means "not looked up" and the check is skipped - listing assignments
        needs a Graph call, which this function deliberately does not make.
    #>
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][AllowNull()][object]$App,
        [string]$ChatUiFqdn,
        [int]$AdminAssignments = -1,
        [string]$Env = 'dev'
    )
    $checks = [System.Collections.Generic.List[object]]::new()
    function New-Check([string]$Item, [string]$Status, [string]$Detail) {
        $checks.Add([pscustomobject]@{ Item = $Item; Status = $Status; Detail = $Detail })
    }

    if (-not $App) {
        New-Check 'Entra app registration exists' 'FAIL' ("no app with app id $($Config.EntraClientId) in tenant " +
            "$($Config.EntraTenantId) - create it (Deployment.md section 9.1) or correct EntraClientId")
        return $checks
    }

    $appId = [string](Get-Value $App 'appId')
    $uris = @(Get-Value $App 'identifierUris')
    $scopes = @(Get-Value $App 'api.oauth2PermissionScopes')
    $preAuth = @(Get-Value $App 'api.preAuthorizedApplications')
    $tokenVersion = Get-Value $App 'api.requestedAccessTokenVersion'
    $signIn = [string](Get-Value $App 'signInAudience')
    New-Check 'Entra app registration exists' 'PASS' "$(Get-Value $App 'displayName') ($appId)"

    # ---- the scope the browser asks for, and the resource it names.
    $scopeName = Get-EntraScopeName -Scope $Config.EntraApiScope
    $scopeResource = Get-EntraScopeResource -Scope $Config.EntraApiScope
    if (-not $scopeName) {
        New-Check 'EntraApiScope names a scope' 'FAIL' ("'$($Config.EntraApiScope)' is a resource with no scope " +
            "on the end - it has to look like api://$appId/access_as_user")
    }
    else {
        New-Check 'EntraApiScope names a scope' 'PASS' "$scopeName (resource $scopeResource)"
        # Without the resource on identifierUris the scope resolves to nothing at all, and Entra answers
        # AADSTS500011 instead of 65005 - a different symptom, same underlying omission.
        $uriOk = @($uris | Where-Object { $_ -eq $scopeResource }).Count -gt 0
        New-Check 'Scope resource is an identifierUri' ($uriOk ? 'PASS' : 'FAIL') $(
            $uriOk ? $scopeResource
            : "'$scopeResource' is not in identifierUris [$($uris -join ', ')] - AADSTS500011 at sign-in")

        $exposed = @($scopes | Where-Object { $_ -and ([string](Get-Value $_ 'value')) -eq $scopeName })
        if ($exposed.Count -eq 0) {
            New-Check "Scope '$scopeName' exposed" 'FAIL' ('not exposed under Expose an API - this is exactly ' +
                "AADSTS65005 at sign-in. Fix: ./infra/scripts/Set-EntraAppRegistration.ps1 -Env $Env")
        }
        elseif (-not (Get-Value $exposed[0] 'isEnabled')) {
            New-Check "Scope '$scopeName' exposed" 'FAIL' ('exposed but DISABLED, which fails identically. Fix: ' +
                "./infra/scripts/Set-EntraAppRegistration.ps1 -Env $Env")
        }
        else {
            $scopeId = [string](Get-Value $exposed[0] 'id')
            New-Check "Scope '$scopeName' exposed" 'PASS' "enabled, type $(Get-Value $exposed[0] 'type'), id $scopeId"
            # A consent prompt rather than a broken sign-in, so advisory.
            $ownPre = @($preAuth | Where-Object {
                    $_ -and ([string](Get-Value $_ 'appId')) -eq $appId -and
                    (@(Get-Value $_ 'delegatedPermissionIds') -contains $scopeId) })
            New-Check 'Client pre-authorised for its own scope' (($ownPre.Count -gt 0) ? 'PASS' : 'WARN') $(
                ($ownPre.Count -gt 0) ? 'no consent prompt'
                : 'not listed - each user is asked to consent once. Set-EntraAppRegistration.ps1 adds it')
        }
    }

    # ---- the pairing that 401s with a token that is otherwise perfectly valid.
    # requestedAccessTokenVersion is a DIRECTORY setting deciding the token format, and null means 1:
    #     1  ->  iss = https://sts.windows.net/<tid>/   aud = the api:// resource URI that was requested
    #     2  ->  iss = .../<tid>/v2.0                   aud = the bare app id
    # rag-api accepts both, deriving the second audience spelling from ENTRA_AUDIENCE - but only when
    # ENTRA_AUDIENCE names THIS app, and only when a bare app id can be derived from it at all.
    $versionLabel = (($null -eq $tokenVersion) -or ("$tokenVersion" -eq '')) ? 'null (means 1)' : "$tokenVersion"
    $audience = "$($Config.EntraAudience)"
    $audienceBare = ($audience -replace '^api://', '') -replace '^.*/', ''
    $guidAudience = $audienceBare -match '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'
    if ($guidAudience -and $audienceBare -ne $appId) {
        New-Check 'ENTRA_AUDIENCE names this app' 'FAIL' ("EntraAudience is '$audience' but this registration " +
            "is $appId - no token can carry both, so every authenticated request 401s")
    }
    elseif ((-not $guidAudience) -and "$tokenVersion" -eq '2') {
        New-Check 'ENTRA_AUDIENCE matches the token version' 'FAIL' ("token version 2 stamps aud = $appId, but " +
            "EntraAudience is '$audience', which carries no app id to match. Set EntraAudience = '$appId'")
    }
    else {
        New-Check 'ENTRA_AUDIENCE matches the token version' 'PASS' ("requestedAccessTokenVersion $versionLabel, " +
            "so aud will be $(("$tokenVersion" -eq '2') ? $appId : $audience) - accepted")
    }
    # The manifest reference states this as a requirement, not a preference.
    if ($signIn -in @('AzureADandPersonalMicrosoftAccount', 'PersonalMicrosoftAccount') -and "$tokenVersion" -ne '2') {
        New-Check 'signInAudience vs token version' 'FAIL' ("signInAudience is $signIn, which REQUIRES " +
            "api.requestedAccessTokenVersion = 2; it is $versionLabel")
    }

    # ---- application roles. Without these the deployment has no administrator and nobody can upload: the
    # first sign-in succeeds and then every upload is refused with "uploading requires the contributor or admin
    # role". Nothing used to report it before that point.
    $exposedRoles = @(@(Get-Value $App 'appRoles') |
            Where-Object { $_ -and (Get-Value $_ 'isEnabled') } | ForEach-Object { [string](Get-Value $_ 'value') })
    $wantedRoles = @($script:RagOsEntraAppRoles | ForEach-Object { $_.Value })
    $missingRoles = @($wantedRoles | Where-Object { $exposedRoles -notcontains $_ })
    if ($exposedRoles.Count -eq 0) {
        New-Check 'Application roles exposed' 'FAIL' ('none - nobody can upload or open the admin console. ' +
            "Fix: ./infra/scripts/Set-EntraAppRegistration.ps1 -Env $Env -GrantAdminTo me")
    }
    elseif ($missingRoles.Count -gt 0) {
        New-Check 'Application roles exposed' 'WARN' ("$($exposedRoles -join ', ') - missing " +
            "$($missingRoles -join ', '), so nobody can hold those. Re-run Set-EntraAppRegistration.ps1 to add them.")
    }
    else {
        New-Check 'Application roles exposed' 'PASS' ($exposedRoles -join ', ')
    }
    # Creating a role grants nobody anything, and a deployment where no one holds rag.admin is administrable by
    # nobody. WARN rather than FAIL: it is recoverable at any time and does not stop anything being provisioned.
    if ($AdminAssignments -ge 0 -and $exposedRoles -contains 'rag.admin') {
        New-Check 'Someone holds rag.admin' (($AdminAssignments -gt 0) ? 'PASS' : 'WARN') $(
            ($AdminAssignments -gt 0) ? "$AdminAssignments assignment(s)"
            : "nobody - the role exists but is unassigned. Grant it: ./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env $Env -Role admin -To me")
    }

    # ---- redirect URI. Unknowable until 07 has run, so silence before then is correct, not a gap.
    if ($ChatUiFqdn) {
        $want = "https://$ChatUiFqdn/auth/callback"
        $spaOk = @(@(Get-Value $App 'spa.redirectUris') | Where-Object { $_ -eq $want }).Count -gt 0
        New-Check 'SPA redirect URI' ($spaOk ? 'PASS' : 'WARN') $(
            $spaOk ? $want
            : "'$want' is not registered - sign-in fails with AADSTS50011. Run Set-EntraAppRegistration.ps1 -Env $Env")
    }
    return $checks
}

function Resolve-EntraPrincipal {
    <#
    .SYNOPSIS
        'me', an object id, a UPN or a group name -> @{ Id; Type; Display }.
    .DESCRIPTION
        An app role can be assigned to a user OR a group, and the two are told apart by probing rather than by
        guessing from the string: a bare GUID could be either, and getting it wrong produces a Graph error about
        principal types that says nothing about which one was meant.
    #>
    param([Parameter(Mandatory)][string]$Reference)
    if ($Reference -eq 'me') {
        $me = Get-DeployerPrincipal
        return @{ Id = $me.ObjectId; Type = $me.PrincipalType; Display = $me.Name }
    }
    if ($Reference -match '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$') {
        $asUser = Invoke-Az @('ad', 'user', 'show', '--id', $Reference) -AllowNotFound
        if ($asUser) { return @{ Id = $Reference; Type = 'User'; Display = [string](Get-Value $asUser 'userPrincipalName') } }
        $asGroup = Invoke-Az @('ad', 'group', 'show', '--group', $Reference) -AllowNotFound
        if ($asGroup) { return @{ Id = $Reference; Type = 'Group'; Display = [string](Get-Value $asGroup 'displayName') } }
        # Neither readable - possibly a directory-read permission rather than a missing object, so take the id at
        # face value rather than refusing. Graph will reject it if it is genuinely wrong.
        return @{ Id = $Reference; Type = 'Unknown'; Display = $Reference }
    }
    if ($Reference.Contains('@')) {
        $user = Invoke-Az @('ad', 'user', 'show', '--id', $Reference) -AllowNotFound
        if (-not $user) { throw "No user '$Reference' in tenant - check the sign-in name, or pass an object id." }
        return @{ Id = [string](Get-Value $user 'id'); Type = 'User'; Display = $Reference }
    }
    $group = Invoke-Az @('ad', 'group', 'show', '--group', $Reference) -AllowNotFound
    if (-not $group) {
        throw ("No user or group matches '$Reference'. Pass a sign-in name (someone@example.com), an object id, " +
            "a group display name, or 'me'.")
    }
    return @{ Id = [string](Get-Value $group 'id'); Type = 'Group'; Display = [string](Get-Value $group 'displayName') }
}

function Get-EntraServicePrincipal {
    <#
    .SYNOPSIS
        The enterprise application for an app id, created when absent. Returns $null with -NoCreate.
    .DESCRIPTION
        Role ASSIGNMENTS hang off the service principal; role DEFINITIONS hang off the app registration. They are
        two objects, which is why looking under 'App registrations -> App roles' never shows who holds one.
        `az ad app create` makes only the registration, so a first grant may have to create this.

        The appId is asserted rather than assumed: a lookup that silently returned somebody else's enterprise
        application would assign real privileges on the wrong app.
    #>
    param([Parameter(Mandatory)][string]$ClientId, [switch]$NoCreate)
    $sp = Invoke-Az @('ad', 'sp', 'show', '--id', $ClientId) -AllowNotFound
    if (-not $sp) {
        if ($NoCreate) { return $null }
        Write-Info 'No enterprise application for this registration yet - creating one (assignments hang off it).'
        $sp = Invoke-Az @('ad', 'sp', 'create', '--id', $ClientId)
    }
    $actual = [string](Get-Value $sp 'appId')
    if ($actual -and $actual -ne $ClientId) {
        throw ("The enterprise application found for '$ClientId' reports appId '$actual'. Refusing to assign " +
            'roles on an application other than the configured one.')
    }
    return $sp
}

function Get-EntraAppRoleId {
    <#
    .SYNOPSIS  The id of an application role by its value, or $null when it is absent (or disabled).
    .DESCRIPTION
        Assignments reference the role's GUID, not its name, so every grant, revoke and readiness check needs
        this lookup - it was written out by hand in two places before it lived here.
    #>
    param(
        [Parameter(Mandatory)][AllowNull()][object]$Application,
        [Parameter(Mandatory)][string]$Value,
        [switch]$RequireEnabled
    )
    $role = @(@(Get-Value $Application 'appRoles') |
            Where-Object { $_ -and ([string](Get-Value $_ 'value')) -eq $Value }) | Select-Object -First 1
    if (-not $role) { return $null }
    if ($RequireEnabled -and -not (Get-Value $role 'isEnabled')) { return $null }
    return [string](Get-Value $role 'id')
}

function Get-EntraRoleAssignment {
    <#
    .SYNOPSIS
        Every app role assignment on the enterprise application, with the role VALUE joined on.
    .DESCRIPTION
        Graph returns appRoleId as a GUID. Reporting that to an operator is useless - the join against the
        registration's appRoles is what turns "e3f1... -> 8ab2..." into "Priya -> rag.admin".
    #>
    param(
        [Parameter(Mandatory)][string]$ServicePrincipalId,
        [AllowNull()][object]$Application
    )
    $raw = Invoke-AzRest -Method get -Url "https://graph.microsoft.com/v1.0/servicePrincipals/$ServicePrincipalId/appRoleAssignedTo"
    $valueById = @{}
    foreach ($role in @(Get-Value $Application 'appRoles')) {
        if ($role) { $valueById[[string](Get-Value $role 'id')] = [string](Get-Value $role 'value') }
    }
    return @(@(Get-Value $raw 'value') | Where-Object { $_ } | ForEach-Object {
            $roleId = [string](Get-Value $_ 'appRoleId')
            [pscustomobject]@{
                Id            = [string](Get-Value $_ 'id')
                PrincipalId   = [string](Get-Value $_ 'principalId')
                Principal     = [string](Get-Value $_ 'principalDisplayName')
                PrincipalType = [string](Get-Value $_ 'principalType')
                RoleId        = $roleId
                # A role that was deleted after being assigned leaves the assignment behind, pointing at nothing.
                Role          = $valueById.ContainsKey($roleId) ? $valueById[$roleId] : "(no such role: $roleId)"
            }
        })
}

function Grant-EntraRoleAssignment {
    <# .SYNOPSIS  Assigns one application role to one principal. Idempotent: an existing pairing is left alone. #>
    param(
        [Parameter(Mandatory)][string]$ServicePrincipalId,
        [Parameter(Mandatory)][string]$PrincipalId,
        [Parameter(Mandatory)][string]$RoleId,
        [Parameter(Mandatory)][string]$Label,
        [AllowNull()][object[]]$Existing,
        [switch]$DryRun
    )
    # Graph does not deduplicate: the same assignment posted twice becomes two assignments, not one.
    $already = @(@($Existing) | Where-Object {
            $_ -and $_.PrincipalId -eq $PrincipalId -and $_.RoleId -eq $RoleId })
    if ($already.Count -gt 0) { Write-Ok "already assigned: $Label"; return $false }
    if ($DryRun) { Write-Host "    would assign $Label" -ForegroundColor Yellow; return $false }
    $null = Invoke-AzRest -Method post -Url "https://graph.microsoft.com/v1.0/servicePrincipals/$ServicePrincipalId/appRoleAssignedTo" `
        -Body @{ principalId = $PrincipalId; resourceId = $ServicePrincipalId; appRoleId = $RoleId }
    Write-Ok "assigned: $Label"
    return $true
}

function Revoke-EntraRoleAssignment {
    <# .SYNOPSIS  Removes one app role assignment by its own id (not the principal's and not the role's). #>
    param(
        [Parameter(Mandatory)][string]$ServicePrincipalId,
        [Parameter(Mandatory)][string]$AssignmentId,
        [Parameter(Mandatory)][string]$Label,
        [switch]$DryRun
    )
    if ($DryRun) { Write-Host "    would revoke $Label" -ForegroundColor Yellow; return $false }
    $null = Invoke-AzRest -Method delete `
        -Url "https://graph.microsoft.com/v1.0/servicePrincipals/$ServicePrincipalId/appRoleAssignedTo/$AssignmentId"
    Write-Ok "revoked: $Label"
    return $true
}

function Resolve-EntraRoleValue {
    <#
    .SYNOPSIS  Accepts 'rag.admin' or the internal name 'admin' and returns the Entra value, or throws.
    .DESCRIPTION
        Both spellings are in circulation: the docs and access-policy.yaml talk about `admin` and `contributor`,
        while Entra and the token's `roles` claim carry `rag.admin`. Refusing one of them would be a trap, and
        guessing at an unknown name would create a role that gates nothing.
    #>
    param([Parameter(Mandatory)][string]$Name)
    $known = @($script:RagOsEntraAppRoles | ForEach-Object { $_.Value })
    if ($known -contains $Name) { return $Name }
    $prefixed = "rag.$Name"
    if ($known -contains $prefixed) { return $prefixed }
    throw ("Unknown application role '$Name'. RAG-OS understands: $($known -join ', ') " +
        "(the 'rag.' prefix is optional). A role outside that list gates nothing - see access-policy.yaml.")
}

# ------------------------------------------------------------------------------------------------ secrets
function New-RandomSecret {
    <# .SYNOPSIS  Cryptographically random bytes, base64 encoded. #>
    param([int]$Bytes = 64)
    return [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes($Bytes))
}

function Set-KeyVaultSecretValue {
    <# .SYNOPSIS  Writes a secret via a temp file (value never on the command line or console), retrying RBAC propagation. #>
    param(
        [Parameter(Mandatory)][string]$VaultName,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Value,
        [string]$ContentType = 'text/plain'
    )
    $tmp = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($tmp, $Value, [Text.UTF8Encoding]::new($false))
        Invoke-WithRetry -Activity "Set secret '$Name'" -MaxAttempts 10 -RetryOn '(?i)(Forbidden|not authorized|Unauthorized|AuthorizationFailed|ForbiddenByRbac)' -ScriptBlock {
            $null = Invoke-Az -Sensitive @('keyvault', 'secret', 'set', '--vault-name', $VaultName, '--name', $Name, '--file', $tmp,
                '--encoding', 'utf-8', '--content-type', $ContentType, '--query', 'id', '-o', 'tsv')
        }
    }
    finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Get-KeyVaultSecretValue {
    <# .SYNOPSIS  Reads a secret value for use in-process only. Never print the result. #>
    param([Parameter(Mandatory)][string]$VaultName, [Parameter(Mandatory)][string]$Name)
    return (Invoke-Az -Sensitive @('keyvault', 'secret', 'show', '--vault-name', $VaultName, '--name', $Name, '--query', 'value', '-o', 'tsv'))
}

# ------------------------------------------------------------------------------------------------ templates
function ConvertTo-YamlString {
    <# .SYNOPSIS  Single-quoted YAML scalar (safe for any characters except newlines). #>
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return "''" }
    $s = if ($Value -is [bool]) { "$Value".ToLowerInvariant() } else { [string]$Value }
    if ($s -match "[`r`n]") { throw "YAML value must be a single line: '$s'" }
    return "'" + $s.Replace("'", "''") + "'"
}

function Expand-Template {
    <#
    .SYNOPSIS  Replaces {{TOKEN}} placeholders.
    .DESCRIPTION
        A token alone on its line is a block token: every line of its value is indented like the token (for YAML lists).
        Any other token is inline and must be single-line without quotes or backslashes.
        Booleans render as true/false. Unresolved tokens throw.
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][hashtable]$Tokens
    )
    $text = Get-Content -LiteralPath $Path -Raw
    $missing = [System.Collections.Generic.HashSet[string]]::new()
    $format = {
        param($name)
        if (-not $Tokens.ContainsKey($name)) { [void]$missing.Add($name); return '' }
        $v = $Tokens[$name]
        if ($null -eq $v) { return '' }
        if ($v -is [bool]) { return "$v".ToLowerInvariant() }
        return [string]$v
    }
    # block tokens (alone on a line)
    $text = $text -replace '(?m)^(?<indent>[ \t]*)\{\{(?<name>[A-Z0-9_]+)\}\}[ \t]*(?=\r?$)', {
        $indent = $_.Groups['indent'].Value
        $value = & $format $_.Groups['name'].Value
        (($value -split "`r?`n") | ForEach-Object { if ($_.Length) { $indent + $_ } else { $_ } }) -join "`n"
    }
    # inline tokens
    $text = $text -replace '\{\{(?<name>[A-Z0-9_]+)\}\}', {
        $name = $_.Groups['name'].Value
        $value = & $format $name
        if ($value -match "[`r`n`"\\]") { throw "Inline template token {{$name}} contains a newline, quote or backslash." }
        $value
    }
    if ($missing.Count -gt 0) { throw "Template '$Path' has unresolved tokens: $(@($missing) -join ', ')" }
    return $text
}

# ------------------------------------------------------------------------------------------------ configuration
function Get-ShortHash {
    param([Parameter(Mandatory)][string]$Text, [int]$Length = 5)
    $bytes = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($Text.ToLowerInvariant()))
    return (-join ($bytes | ForEach-Object { $_.ToString('x2') })).Substring(0, $Length)
}

function Import-RagOsConfig {
    <#
    .SYNOPSIS  Loads infra/env/<Env>.psd1 over the defaults in dev.sample.psd1 and derives resource names.
    .OUTPUTS   Hashtable: every psd1 setting + Names (resource names) + paths (RepoRoot, OutputsPath, ImagesPath, ...).
    #>
    param([Parameter(Mandatory)][string]$Env)
    $samplePath = Join-Path $script:RagOsEnvDir 'dev.sample.psd1'
    $envPath = Join-Path $script:RagOsEnvDir "$Env.psd1"
    if (-not (Test-Path -LiteralPath $envPath)) {
        throw "Settings file not found: $envPath`nCopy infra/env/dev.sample.psd1 to infra/env/$Env.psd1 and fill in SubscriptionId, TenantId, Location, Prefix."
    }
    $defaults = Import-PowerShellDataFile -LiteralPath $samplePath
    $settings = Import-PowerShellDataFile -LiteralPath $envPath

    $config = @{}
    foreach ($key in $defaults.Keys) { $config[$key] = $defaults[$key] }
    foreach ($key in $settings.Keys) {
        if (-not $defaults.ContainsKey($key)) { Write-Warning "Unknown setting '$key' in $Env.psd1 (not in dev.sample.psd1) - typo?" }
        $config[$key] = $settings[$key]
    }
    foreach ($key in $defaults.Keys) {
        if (-not $settings.ContainsKey($key)) { Write-Verbose "Setting '$key' not in $Env.psd1; using the sample default." }
    }

    # ---- validation
    $guid = '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'
    if ($config.SubscriptionId -notmatch $guid -or $config.SubscriptionId -eq '00000000-0000-0000-0000-000000000000') { throw "Set SubscriptionId in $envPath" }
    if ($config.TenantId -notmatch $guid -or $config.TenantId -eq '00000000-0000-0000-0000-000000000000') { throw "Set TenantId in $envPath" }
    if (-not $config.Location) { throw "Set Location in $envPath" }
    if ($config.Prefix -cnotmatch '^[a-z][a-z0-9]{1,11}$') { throw "Prefix must be 2-12 lowercase letters/digits starting with a letter (got '$($config.Prefix)')." }
    if ($config.Env -cnotmatch '^[a-z][a-z0-9]{0,7}$') { throw "Env must be 1-8 lowercase letters/digits (got '$($config.Env)')." }
    if ($config.Env -ne $Env) { throw "infra/env/$Env.psd1 declares Env = '$($config.Env)'. They must match." }
    # Service Bus caps the lock at 5 minutes. Caught here rather than at the queue update, where an out-of-range
    # value made every subsequent run of step 03 fail on a queue that already existed and was otherwise fine.
    if ($config.QueueLockDuration -notmatch '^PT(\d+M)?(\d+S)?$' -or $config.QueueLockDuration -eq 'PT') {
        throw "QueueLockDuration must be an ISO-8601 duration such as 'PT5M' or 'PT30S' (got '$($config.QueueLockDuration)')."
    }
    $lockSeconds = 0
    if ($config.QueueLockDuration -match '(\d+)M') { $lockSeconds += [int]$Matches[1] * 60 }
    if ($config.QueueLockDuration -match '(\d+)S') { $lockSeconds += [int]$Matches[1] }
    if ($lockSeconds -lt 5 -or $lockSeconds -gt 300) { throw "QueueLockDuration must be between PT5S and PT5M - Azure Service Bus rejects anything else (got '$($config.QueueLockDuration)')." }

    # ---- names: <type>-<prefix>-<env>[-<suffix>] ; globally unique names get a deterministic 5-char suffix
    $base = "$($config.Prefix)-$($config.Env)"
    $alnum = $base -replace '[^a-z0-9]', ''
    $suffix = if ($config.NameSuffix) { ([string]$config.NameSuffix).ToLowerInvariant() } else { Get-ShortHash "$($config.SubscriptionId)/$base" }
    $cut = { param($s, $max) if ($s.Length -gt $max) { $s.Substring(0, $max) } else { $s } }
    $names = [ordered]@{
        ResourceGroup  = if ($config.ResourceGroup) { $config.ResourceGroup } else { "rg-$base" }
        LogAnalytics   = "log-$base"
        AppInsights    = "appi-$base"
        Budget         = "budget-$base"
        Identity       = "id-$base"
        KeyVault       = "kv-$(& $cut $alnum 15)-$suffix"            # <= 24 chars
        Storage        = "st$(& $cut $alnum 17)$suffix"              # <= 24 lowercase alphanumerics
        Postgres       = "psql-$base-$suffix"
        ServiceBus     = "sb-$base-$suffix"
        Search         = "srch-$base-$suffix"
        Foundry        = "aif-$base-$suffix"                         # also the custom sub-domain
        FoundryProject = if ($config.FoundryProject) { $config.FoundryProject } else { "proj-$base" }
        Registry       = "acr$(& $cut $alnum 40)$suffix"
        ContainerEnv   = "cae-$base"
    }
    if ($config.NameOverrides) {
        foreach ($key in $config.NameOverrides.Keys) {
            if (-not $names.Contains($key)) { throw "NameOverrides.$key is not a known resource name ($($names.Keys -join ', '))." }
            $names[$key] = $config.NameOverrides[$key]
        }
    }

    $config.EnvName = $Env
    $config.Names = $names
    $config.RepoRoot = $script:RagOsRepoRoot
    $config.InfraDir = $script:RagOsInfraDir
    $config.EnvDir = $script:RagOsEnvDir
    $config.OutputsPath = Join-Path $script:RagOsEnvDir "$Env.outputs.json"
    $config.ImagesPath = Join-Path $script:RagOsEnvDir "$Env.images.json"
    $config.ResourceGroupId = "/subscriptions/$($config.SubscriptionId)/resourceGroups/$($names.ResourceGroup)"

    # Checked here rather than with the other validation above, because it needs RepoRoot to read the profile -
    # and it only applies to a self-hosted profile. A remote profile pins nothing: config/embedding/profiles.yaml
    # gives `aoai-3-small-1536` no model_revision, because Azure OpenAI has no commit to pin. Demanding a SHA
    # regardless made an otherwise correct remote configuration impossible to express: every script that loads
    # the config threw before it did anything. $null (profile unreadable) keeps the strict behaviour.
    $embedProvider = Get-EmbeddingProfileProvider -Config $config
    if ($embedProvider -in @('tei', $null) -and $config.EmbedderModelRevision -notmatch '^[0-9a-f]{40}$') {
        throw ("EmbedderModelRevision must be a full 40-character commit SHA (pin the model). " +
            "Got '$($config.EmbedderModelRevision)'.")
    }
    $allTags = [ordered]@{ 'app' = 'rag-os'; 'env' = $config.Env; 'managed-by' = 'rag-os-infra-scripts' }
    if ($config.Tags) { foreach ($key in $config.Tags.Keys) { $allTags[$key] = [string]$config.Tags[$key] } }
    $config.AllTags = $allTags
    return $config
}

function Get-EmbeddingProfileProvider {
    <#
    .SYNOPSIS  The `provider:` of the active embedding profile: 'tei' | 'azure_openai' | 'fake', or $null.
    .DESCRIPTION
        PowerShell has no YAML reader and one field does not justify taking a dependency on powershell-yaml, so
        this reads the one line it needs: find the profile by name, then its first `provider:` before the next
        profile dedents.

        Returns $null when the file, the profile or the field cannot be found. EVERY caller must treat $null as
        "unknown" and carry on as before - an unreadable config file must never block a deployment.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][hashtable]$Config)
    $path = Join-Path $Config.RepoRoot 'config/embedding/profiles.yaml'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    if (-not $Config.EmbeddingProfile) { return $null }
    $want = [regex]::Escape([string]$Config.EmbeddingProfile)
    $inProfile = $false
    foreach ($line in (Get-Content -LiteralPath $path)) {
        if ($line -match "^\s{2}$want\s*:\s*$") { $inProfile = $true; continue }
        if ($inProfile) {
            if ($line -match '^\s{0,2}\S') { break }                    # dedent: next profile started
            if ($line -match '^\s+provider:\s*([a-z_]+)') { return $Matches[1] }
        }
    }
    return $null
}

function Get-EmbeddingProfileField {
    <#
    .SYNOPSIS  One scalar field of the active embedding profile, as text, or $null.
    .DESCRIPTION
        The generalisation of Get-EmbeddingProfileProvider (which stays, because it is called in several places
        and reads better at those call sites). Same deliberate limits: same one-field-at-a-time scan, same
        contract that $null means "unknown, carry on" rather than "absent, fail".

        Values are returned as strings and compared as strings by callers - `dimensions: 1024` and a psd1
        `1024` must compare equal, and a quoted YAML scalar must not compare differently to a bare one, so
        surrounding quotes and a trailing comment are stripped.
    .EXAMPLE
        Get-EmbeddingProfileField -Config $Config -Name 'model'
        Get-EmbeddingProfileField -Config $Config -Name 'model_revision'
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][string]$Name)
    $path = Join-Path $Config.RepoRoot 'config/embedding/profiles.yaml'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    if (-not $Config.EmbeddingProfile) { return $null }
    $want = [regex]::Escape([string]$Config.EmbeddingProfile)
    $field = [regex]::Escape($Name)
    $inProfile = $false
    foreach ($line in (Get-Content -LiteralPath $path)) {
        if ($line -match "^\s{2}$want\s*:\s*$") { $inProfile = $true; continue }
        if (-not $inProfile) { continue }
        if ($line -match '^\s{0,2}\S') { break }                       # dedent: the next profile started
        if ($line -match "^\s+$field\s*:\s*(.+?)\s*$") {
            $value = $Matches[1]
            $value = ($value -replace '\s+#.*$', '').Trim()             # trailing comment
            return $value.Trim('"').Trim("'")
        }
    }
    return $null
}

function Test-EmbeddingDeploymentConfig {
    <#
    .SYNOPSIS  Cross-checks DeployAoaiEmbedding against the profile's provider. Throws on the broken combination.
    .DESCRIPTION
        DeployAoaiEmbedding *deploys* text-embedding-3-small; EmbeddingProfile *selects* which model is used.
        Setting one without the other has two failure modes, and neither was detectable before: the scripts had
        no way to learn that 'qwen3-0.6b-1024' means provider 'tei'.
    .OUTPUTS  The provider string, or $null when it could not be determined.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][hashtable]$Config)
    $provider = Get-EmbeddingProfileProvider -Config $Config
    if (-not $provider) { return $null }          # unknown: say nothing, change nothing
    $profileName = $Config.EmbeddingProfile

    if ($provider -eq 'azure_openai' -and -not $Config.DeployAoaiEmbedding) {
        # Deploys clean and then fails on the first embedding call: AOAI_EMBED_DEPLOYMENT is never set.
        throw ("EmbeddingProfile '$profileName' uses provider 'azure_openai', but DeployAoaiEmbedding is " +
            '$false, so no embedding deployment is created and AOAI_EMBED_DEPLOYMENT is never set. ' +
            'Every query and every ingested document would fail. Set DeployAoaiEmbedding = $true, ' +
            "or choose a self-hosted profile such as 'qwen3-0.6b-1024'.")
    }
    if ($provider -ne 'azure_openai' -and $Config.DeployAoaiEmbedding) {
        Write-Warn ("DeployAoaiEmbedding is `$true, but EmbeddingProfile '$profileName' uses provider " +
            "'$provider' - the deployed model is never called.")
        Write-Info "It still holds $($Config.EmbeddingModelCapacity)K TPM of regional quota. Either set"
        Write-Info "  EmbeddingProfile = 'aoai-3-small-1536'   to actually use it (needs a full re-ingest), or"
        Write-Info '  DeployAoaiEmbedding = $false             to stop deploying it.'
    }
    return $provider
}

function Get-TagArgs {
    <# .SYNOPSIS  '--tags k=v ...' arguments from the merged tag set. #>
    param([Parameter(Mandatory)][hashtable]$Config)
    $pairs = @($Config.AllTags.Keys | ForEach-Object { "$_=$($Config.AllTags[$_])" })
    if ($pairs.Count -eq 0) { return @() }
    return @('--tags') + $pairs
}

function Set-RagOsAzContext {
    <# .SYNOPSIS  Ensures az is logged in to the configured subscription. #>
    param([Parameter(Mandatory)][hashtable]$Config)
    $current = Get-AzAccountOrNull
    if (-not $current) {
        # Deliberately not "you are not logged in": az account show also fails when the CLI cannot reach Azure at
        # all (a TLS-intercepting proxy or antivirus is the common one), and asserting the wrong cause sends the
        # operator to re-login over and over. Quote az instead and let its own words say which it is.
        $why = if ($script:RagOsLastAccountError) { " az said: $($script:RagOsLastAccountError)" } else { '' }
        throw "az has no usable context. Run ./infra/scripts/00-prereqs.ps1 -Env $($Config.Env) (or az login --tenant $($Config.TenantId)).$why"
    }
    if ($current -ne $Config.SubscriptionId) {
        Write-Info "Switching az subscription to $($Config.SubscriptionId)"
        $null = Invoke-Az @('account', 'set', '--subscription', $Config.SubscriptionId, '-o', 'none')
    }
}

function Initialize-RagOsScript {
    <# .SYNOPSIS  Standard script preamble: load settings, check the az context, print a banner. Returns the config. #>
    param([Parameter(Mandatory)][string]$Env, [Parameter(Mandatory)][string]$Title, [switch]$SkipAzContext)
    $config = Import-RagOsConfig -Env $Env
    Write-Host ''
    Write-Host "RAG-OS | $Title | env=$Env | rg=$($config.Names.ResourceGroup) | $($config.Location)" -ForegroundColor Magenta
    if (-not $SkipAzContext) { Set-RagOsAzContext -Config $config }
    return $config
}

# ------------------------------------------------------------------------------------------------ outputs
function Get-Outputs {
    param([Parameter(Mandatory)][hashtable]$Config)
    if (-not (Test-Path -LiteralPath $Config.OutputsPath)) { return @{} }
    $raw = Get-Content -LiteralPath $Config.OutputsPath -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) { return @{} }
    return (ConvertFrom-Json -InputObject $raw -AsHashtable)
}

function Save-Outputs {
    <#
    .SYNOPSIS  Merges values into infra/env/<env>.outputs.json (ids and endpoints only - never secrets).
    .PARAMETER Clear
        Keys to remove outright. The empty-value guard below deliberately refuses to overwrite a good value with
        nothing, which is right for a probe that came back empty but wrong for a step that knows the resource is
        gone - flipping DeployAoaiEmbedding off, for instance. This is how a step says so explicitly.
    #>
    param([Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][hashtable]$Values, [string[]]$Clear)
    $current = Get-Outputs -Config $Config
    # @($null) is a one-element array holding $null, not an empty one, and ContainsKey($null) throws.
    foreach ($key in @($Clear | Where-Object { $_ })) {
        if ($current.ContainsKey($key)) {
            $current.Remove($key)
            Write-Info "Removed output '$key' - it no longer applies to this configuration."
        }
    }
    foreach ($key in $Values.Keys) {
        # Several values come from probes that return $null when a workload was not deployed this run (07 with
        # -Only, for instance). Writing that over a value an earlier run established would make Get-Output report
        # it as missing and send the operator back to a step that already succeeded. $false is an answer, not an
        # absence, so the test is on emptiness rather than truthiness.
        $incoming = $Values[$key]
        $isEmpty = ($null -eq $incoming) -or ("$incoming" -eq '')
        if ($isEmpty -and $current.ContainsKey($key) -and "$($current[$key])" -ne '') {
            Write-Warn "Keeping the stored '$key' - this run had no value for it (nothing was overwritten)."
            continue
        }
        $current[$key] = $incoming
    }
    $sorted = [ordered]@{}
    foreach ($key in ($current.Keys | Sort-Object)) { $sorted[$key] = $current[$key] }
    $sorted | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Config.OutputsPath -Encoding utf8NoBOM
}

function Get-Output {
    <# .SYNOPSIS  One required output value; throws with the script to run when it is missing. #>
    param([Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][string]$Name, [string]$ProducedBy = 'the earlier provisioning scripts')
    $outputs = Get-Outputs -Config $Config
    if (-not $outputs.ContainsKey($Name) -or $null -eq $outputs[$Name] -or "$($outputs[$Name])" -eq '') {
        throw "Output '$Name' is missing from $($Config.OutputsPath). Run $ProducedBy first."
    }
    return $outputs[$Name]
}

# ------------------------------------------------------------------------------------------------ misc
function Get-ImageTag {
    <# .SYNOPSIS  git short SHA (+ '-dirty-<timestamp>' for uncommitted changes); timestamp when git is unavailable. #>
    param([Parameter(Mandatory)][string]$RepoRoot)
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
    # A timestamp tag is never equal to the last one, so 06 cannot skip a build it already did and 07 sees every
    # digest change and rolls every app. With a TEI embedding profile that is four images, two of them multi-GB
    # and up to two hours each - expensive enough that it must not happen quietly.
    $unstable = {
        param($Reason)
        Write-Warn "Image tag falls back to a timestamp ($stamp): $Reason."
        Write-Info '  Every run will then rebuild every image and roll every container app, because the tag is'
        Write-Info '  never the same twice. Fix it with either:'
        Write-Info '    git add -A && git commit -m "initial"      (the tag becomes the short commit SHA)'
        Write-Info "    ImageTag = '<something-stable>' in the psd1"
    }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) { & $unstable 'git is not on PATH'; return $stamp }
    $previousEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $sha = & git -C $RepoRoot rev-parse --short HEAD 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $sha) { & $unstable 'the repository has no commits yet'; return $stamp }
        # The files provisioning writes must not decide the tag of the next build. 06 rewrites <env>.images.json
        # on every run and .gitignore keeps it tracked on purpose, so including it here left the tree permanently
        # dirty - every run got a fresh '-dirty-<stamp>' tag, rebuilt every image, and rolled every container app.
        $dirty = @(& git -C $RepoRoot status --porcelain 2>$null |
                Where-Object { $_ -and $_ -notmatch 'infra/env/[^/]+\.(images|outputs)\.json$' })
    }
    finally { $ErrorActionPreference = $previousEap }
    if ($dirty.Count -gt 0) { return "$($sha.Trim())-dirty-$stamp" }
    return $sha.Trim()
}

function Format-Bytes {
    <# .SYNOPSIS  Bytes as '2.85 GB' / '412 MB' - short enough to sit inside a status line. #>
    param([Parameter(Mandatory)][AllowNull()][Nullable[long]]$Bytes)
    if (-not $Bytes) { return 'unknown size' }
    if ($Bytes -ge 1GB) { return "$([math]::Round($Bytes / 1GB, 2)) GB" }
    if ($Bytes -ge 1MB) { return "$([math]::Round($Bytes / 1MB, 0)) MB" }
    return "$Bytes B"
}

# ------------------------------------------------------------------------------------- ACR image retention
# Images only ever accumulated. Nothing in these scripts deleted one, and Get-ImageTag hands out a unique
# '-dirty-<timestamp>' tag on every run against a dirty tree, so a Basic registry (10 GB) fills quietly.
#
# The hazard is not the deleting, it is deleting the WRONG thing. Every workload is deployed by digest with
# activeRevisionsMode Single, and a live revision re-pulls on each scale-out, node move and restart - so removing
# a digest it points at breaks it, and for the two jobs that breakage stays invisible until the next cron fire.
# One digest can also carry several tags, `az acr repository delete --image repo:tag` removes the manifest rather
# than the tag pointer, and nothing here is recoverable: soft-delete is a preview policy and is not enabled.
#
# So the protected set is computed first and from the live platform, and any failure to compute it aborts the
# prune. Retaining a few gigabytes too many costs money; deleting a running image costs an outage.

# The workloads that pull from this registry. rag-api's image is shared by four of them, which is exactly the
# case a naive "the app is called rag-api" check would miss.
$script:RagOsImageApps = @('rag-api', 'rag-chat-ui', 'rag-ingest-worker', 'rag-embed-query', 'rag-embed-ingest')
$script:RagOsImageJobs = @('rag-scheduler', 'rag-bootstrap')

function Get-DigestFromImageRef {
    <#
    .SYNOPSIS  The sha256 digest an image reference pins, or $null when it names a tag instead.
    .DESCRIPTION
        A deployed reference is normally '<registry>/<repo>@sha256:...' because 07 resolves tags to digests. A
        tag-pinned reference can still appear if someone deployed by hand, and it has to be resolved rather than
        ignored - an unresolved reference is an unprotected digest.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Reference)
    if ($Reference -match '@(sha256:[0-9a-f]{64})') { return $Matches[1] }
    return $null
}

function Get-AcrProtectedDigests {
    <#
    .SYNOPSIS  Every digest something still references, as a hashtable of digest -> why it is protected.
    .DESCRIPTION
        Three sources, deliberately overlapping. The live platform is authoritative - it is the only one that
        knows about an app deployed from a tag the manifest no longer records - and the two files are belt and
        braces for a workload that exists but cannot be read right now.
    .OUTPUTS
        @{ Digests = @{ '<digest>' = '<reason>' }; Complete = $true|$false; Problems = @(...) }
        Complete is $false when any lookup failed. A caller that deletes on an incomplete set is wrong.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][string]$Registry)
    $rg = $Config.Names.ResourceGroup
    $digests = @{}
    $problems = [System.Collections.Generic.List[string]]::new()

    $record = {
        param([string]$Reference, [string]$Reason)
        $digest = Get-DigestFromImageRef -Reference $Reference
        if (-not $digest) {
            # A tag-pinned deployment. Resolve it, and treat a failure as a hole in the protected set rather
            # than shrugging: the whole point of this function is that it is not allowed to guess.
            if ($Reference -match '/([^/:@]+):([^/:@]+)$') {
                $resolved = Invoke-Az @('acr', 'repository', 'show', '-n', $Registry, '--image',
                    "$($Matches[1]):$($Matches[2])", '--query', 'digest', '-o', 'tsv') -AllowNotFound
                if ($resolved) { $digest = "$resolved".Trim() }
            }
        }
        if ($digest) { $digests[$digest] = $Reason }
        elseif ($Reference) { $problems.Add("could not resolve '$Reference' ($Reason)") }
    }

    # 1. The live platform.
    foreach ($app in $script:RagOsImageApps) {
        try {
            $images = @(Get-AzTsvValues @('containerapp', 'revision', 'list', '-g', $rg, '-n', $app, '--query',
                    '[?properties.active].properties.template.containers[].image') -AllowNotFound)
            foreach ($image in $images) { & $record "$image" "active revision of $app" }
        }
        catch { $problems.Add("could not read revisions of ${app}: $($_.Exception.Message.Split("`n")[0])") }
    }
    foreach ($job in $script:RagOsImageJobs) {
        try {
            $images = @(Get-AzTsvValues @('containerapp', 'job', 'show', '-g', $rg, '-n', $job, '--query',
                    'properties.template.containers[].image') -AllowNotFound)
            foreach ($image in $images) { & $record "$image" "job $job" }
        }
        catch { $problems.Add("could not read job ${job}: $($_.Exception.Message.Split("`n")[0])") }
    }

    # 2. What 07 last deployed, and 3. what 06 last recorded - including the embedder refs, which a partial
    #    -Images run leaves pointing at an older generation that is still live.
    # Guarded: Get-Value hands back $null for a missing key, and .Values on $null is a terminating error under
    # StrictMode - which would abort the prune for the ordinary reason that 07 has not run yet.
    $deployed = Get-Value (Get-Outputs -Config $Config) 'deployedImages'
    if ($deployed) {
        foreach ($ref in @($deployed.Values)) { & $record "$ref" 'recorded in outputs.json (last deployed)' }
    }
    if (Test-Path -LiteralPath $Config.ImagesPath) {
        try {
            $manifest = Get-Content -LiteralPath $Config.ImagesPath -Raw | ConvertFrom-Json -AsHashtable
            $recorded = Get-Value $manifest 'images'
            if ($recorded) {
                foreach ($repo in @($recorded.Keys)) { & $record "$(Get-Value $manifest "images.$repo.ref")" "images.json ($repo)" }
            }
            $serverImages = Get-Value $manifest 'embedding.serverImages'
            if ($serverImages) {
                foreach ($ref in @($serverImages.Values)) { & $record "$ref" 'images.json (embedding profile)' }
            }
        }
        catch { $problems.Add("could not read $(Split-Path -Leaf $Config.ImagesPath): $($_.Exception.Message.Split("`n")[0])") }
    }

    return @{ Digests = $digests; Complete = ($problems.Count -eq 0); Problems = @($problems) }
}

function Remove-StaleAcrImages {
    <#
    .SYNOPSIS  Deletes manifests that nothing references and that fall outside the retention window.
    .DESCRIPTION
        Keeps -Keep generations per repository so the documented rollback (07-container-apps.ps1 -Tag <previous>)
        stays available, and never touches a digest in the protected set however old it is.

        Aborts without deleting anything when the protected set could not be computed in full. That is the whole
        safety property: a permanent delete on incomplete information is the one outcome worth avoiding.
    .OUTPUTS
        @{ Deleted = <count>; FreedBytes = <long>; Skipped = @(...); Aborted = $true|$false }
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][string]$Registry,
        [Parameter(Mandatory)][string[]]$Repositories,
        [int]$Keep = 2,
        # Report what would go, delete nothing.
        [switch]$DryRun
    )
    Write-Step "Registry retention (keep $Keep generation(s) per repository)"
    $protected = Get-AcrProtectedDigests -Config $Config -Registry $Registry
    if (-not $protected.Complete) {
        Write-Warn 'Not pruning: the set of images still in use could not be established in full.'
        foreach ($problem in $protected.Problems) { Write-Info "  $problem" }
        Write-Info '  Deleting on a partial answer could remove an image a running revision needs, which breaks'
        Write-Info '  its next restart or scale-out. Fix the reads above, or pass -NoPrune to skip this step.'
        return @{ Deleted = 0; FreedBytes = 0; Skipped = @(); Aborted = $true }
    }
    Write-Info "$($protected.Digests.Count) digest(s) are in use and will be kept regardless of age."

    $deleted = 0
    $freed = 0L
    $skipped = [System.Collections.Generic.List[string]]::new()
    foreach ($repo in $Repositories) {
        $manifests = @(Invoke-Az @('acr', 'manifest', 'list-metadata', '-r', $Registry, '-n', $repo, '--orderby',
                'time_desc', '--query', '[].{digest:digest, tags:tags, size:imageSize, created:createdTime}') -AllowNotFound)
        if (-not $manifests) { continue }
        $kept = 0
        foreach ($m in $manifests) {
            $digest = "$($m.digest)"
            $tags = if ($m.tags) { @($m.tags) -join ',' } else { '<untagged>' }
            $size = if ($m.size) { [long]$m.size } else { 0L }
            if ($protected.Digests.ContainsKey($digest)) {
                Write-Host ("    keep   $repo  $tags  - in use ($($protected.Digests[$digest]))") -ForegroundColor DarkGray
                $kept++
                continue
            }
            if ($kept -lt $Keep) {
                Write-Host ("    keep   $repo  $tags  - retention $($kept + 1)/$Keep") -ForegroundColor DarkGray
                $kept++
                continue
            }
            if ($DryRun) { Write-Host ("    would delete $repo  $tags  ($(Format-Bytes $size))") -ForegroundColor Yellow; continue }
            try {
                $null = Invoke-Az @('acr', 'manifest', 'delete', '-r', $Registry, '-n', "${repo}@$digest", '-y', '-o', 'none')
                Write-Host ("    delete $repo  $tags  ($(Format-Bytes $size))") -ForegroundColor Yellow
                $deleted++
                $freed += $size
            }
            catch {
                $skipped.Add("$repo $tags")
                Write-Warn "Could not delete $repo@$digest : $($_.Exception.Message.Split("`n")[0])"
            }
        }
    }
    if ($deleted -gt 0) { Write-Ok "Removed $deleted manifest(s), reclaiming $(Format-Bytes $freed)." }
    elseif (-not $DryRun) { Write-Ok 'Nothing to remove - every stored image is either in use or within retention.' }
    return @{ Deleted = $deleted; FreedBytes = $freed; Skipped = @($skipped); Aborted = $false }
}

function Remove-UnusedAcrRepository {
    <#
    .SYNOPSIS  Deletes a whole repository, but only when no container app or job references it.
    .DESCRIPTION
        For the embedder repositories after a switch to a remote embedding profile: several gigabytes that
        nothing will pull again. The guard matters because 07 stops *deploying* those pools without deleting
        them, so the apps outlive their purpose - and removing the images from under a still-existing app turns
        a decommissioning into a broken restart.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][string]$Registry,
        [Parameter(Mandatory)][string]$Repository,
        [Parameter(Mandatory)][string[]]$ReferencedBy,
        [switch]$DryRun
    )
    $rg = $Config.Names.ResourceGroup
    $blockers = @($ReferencedBy | Where-Object {
            Test-AzResource @('containerapp', 'show', '-g', $rg, '-n', $_, '--query', 'id', '-o', 'tsv')
        })
    if ($blockers.Count -gt 0) {
        Write-Warn "$Repository is unused by the current profile but $($blockers -join ' and ') still exist(s)."
        Write-Info '  Delete the app first - removing its image would break the next restart rather than tidy up:'
        foreach ($app in $blockers) { Write-Host "      az containerapp delete -g $rg -n $app --yes" -ForegroundColor Yellow }
        Write-Info "  Then re-run this step and $Repository will be removed."
        return $false
    }
    if (-not (Test-AzResource @('acr', 'repository', 'show', '-n', $Registry, '--repository', $Repository, '--query', 'imageName', '-o', 'tsv'))) {
        return $false
    }
    if ($DryRun) { Write-Host "    would delete repository $Repository" -ForegroundColor Yellow; return $false }
    $null = Invoke-Az @('acr', 'repository', 'delete', '-n', $Registry, '--repository', $Repository, '--yes', '-o', 'none')
    Write-Ok "Deleted repository $Repository - nothing references it under the current embedding profile."
    return $true
}

function Get-ChatUiUrl {
    <# .SYNOPSIS  https://<fqdn> of rag-chat-ui (from outputs, else queried). #>
    param([Parameter(Mandatory)][hashtable]$Config)
    $outputs = Get-Outputs -Config $Config
    $fqdn = if ($outputs.ContainsKey('chatUiFqdn')) { $outputs['chatUiFqdn'] } else { $null }
    if (-not $fqdn) {
        $fqdn = Invoke-Az @('containerapp', 'show', '-n', 'rag-chat-ui', '-g', $Config.Names.ResourceGroup,
            '--query', 'properties.configuration.ingress.fqdn', '-o', 'tsv')
    }
    if (-not $fqdn) { throw 'rag-chat-ui has no FQDN yet. Run 07-container-apps.ps1 first.' }
    return "https://$fqdn"
}
