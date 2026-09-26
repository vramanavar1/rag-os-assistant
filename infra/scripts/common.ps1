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
    try {
        if ($null -ne $Body) {
            $bodyFile = [IO.Path]::GetTempFileName()
            $json = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 30 -Compress }
            [IO.File]::WriteAllText($bodyFile, $json, [Text.UTF8Encoding]::new($false))
            $restArgs += @('--body', "@$bodyFile", '--headers', 'Content-Type=application/json')
        }
        return (Invoke-Az -Arguments $restArgs -AllowNotFound:$AllowNotFound -Sensitive:$Sensitive)
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
    if ($config.EmbedderModelRevision -notmatch '^[0-9a-f]{40}$') { throw 'EmbedderModelRevision must be a full 40-character commit SHA (pin the model).' }
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
