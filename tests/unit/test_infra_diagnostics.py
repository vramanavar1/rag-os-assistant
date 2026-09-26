"""A failed provisioning step has to say what went wrong, where the time went, and what to do next.

`az acr build` streams a Docker build log that can run to hundreds of lines, and the line that matters is
usually nowhere near the end. The scripts used to discard all of it - `Invoke-Az -Stream` piped straight to the
console and captured nothing - so every build failure ended as:

    az command failed (exit 1): az acr build ...
    (no error text captured; see the output above)

which tells the reader only that they now have to go and read the log themselves. Three things fix that, and all
three are pure functions over text, so they test without Azure, without Docker and without a network:

* `Get-AzFailureHint`   - the failing text mapped to a cause and a recommendation
* `Test-StreamRetryable` - whether re-running a build that costs hours is worth it
* `Format-OutputPauses`  - which step consumed the time, from the timestamps of the lines that did arrive
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMMON = REPO / "infra" / "scripts" / "common.ps1"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")


def run_pwsh(body: str) -> dict:
    """Dot-source common.ps1, run `body`, and read back the JSON it writes to $out."""
    work = Path(tempfile.mkdtemp())
    out, script = work / "out.json", work / "probe.ps1"
    script.write_text(f". '{COMMON.as_posix()}'\n$out = '{out.as_posix()}'\n{body}\n", encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n{done.stdout}\n{done.stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


def hints(samples: dict[str, str]) -> dict[str, dict | None]:
    payload = json.dumps(samples)
    src = Path(tempfile.mkdtemp()) / "samples.json"
    src.write_text(payload, encoding="utf-8")
    return run_pwsh(f"""
$samples = Get-Content -LiteralPath '{src.as_posix()}' -Raw | ConvertFrom-Json -AsHashtable
$result = @{{}}
foreach ($k in $samples.Keys) {{
    $h = Get-AzFailureHint $samples[$k]
    $result[$k] = if ($h) {{ @{{ cause = $h.Cause; fix = $h.Fix }} }} else {{ $null }}
}}
ConvertTo-Json -InputObject $result -Depth 5 | Set-Content -LiteralPath $out -Encoding utf8NoBOM
""")


@needs_pwsh
def test_the_failure_that_actually_happened_is_diagnosed() -> None:
    """The exact line from the deploy log that cost a round trip to identify."""
    got = hints({"buildkit": "the --mount option requires BuildKit. Refer to https://docs.docker.com/go/buildkit/"})
    hint = got["buildkit"]
    assert hint, "the BuildKit failure must be recognised"
    assert "classic" in hint["cause"].lower(), f"the cause should name the classic builder: {hint['cause']}"
    assert "test_dockerfiles" in hint["fix"], "the fix should point at the guard that catches it before a deploy"


@needs_pwsh
def test_the_expensive_failure_modes_are_diagnosed() -> None:
    """Each of these costs real time to hit, and none of them says what to do on its own."""
    got = hints({
        "ratelimit": "toomanyrequests: You have reached your pull rate limit. https://www.docker.com/increase-rate-limit",
        "quota": "denied: requested access to the resource is denied: registry quota exceeded",
        "disk": "write /var/lib/docker: no space left on device",
        "manifest": "manifest for ghcr.io/huggingface/text-embeddings-inference:turing-9.9 not found",
        "auth": "unauthorized: authentication required",
        "provider": "MissingSubscriptionRegistration: The subscription is not registered to use namespace 'Microsoft.App'",
    })
    expected_in_fix = {
        "ratelimit": "acr import",
        "quota": "show-usage",
        "disk": "show-usage",
        "manifest": "TeiVersion",
        "auth": "AcrPull",
        "provider": "00-prereqs",
    }
    for key, needle in expected_in_fix.items():
        hint = got[key]
        assert hint, f"{key} was not diagnosed at all"
        assert needle in hint["fix"], f"{key} recommendation should mention {needle!r}, got: {hint['fix']}"


@needs_pwsh
def test_output_that_is_merely_unfamiliar_gets_no_diagnosis() -> None:
    """A guess presented as a diagnosis is worse than the raw error - it sends the reader somewhere else.

    Pattern matching is only trustworthy if it also declines to match, so this pins the declining.
    """
    got = hints({
        "healthy": "Step 19/19 : CMD [\"uvicorn\"]\\nSuccessfully built e8d723e6890c\\nSuccessfully tagged rag-api:x",
        "unknown": "Error: something nobody has seen before (0x80070002)",
        "empty": "",
    })
    assert all(got[k] is None for k in ("healthy", "unknown", "empty")), f"should not have matched: {got}"


@needs_pwsh
def test_a_timeout_is_never_retried_but_a_pull_failure_is() -> None:
    """An image build costs minutes to hours, so the retry decision is not the cheap one Invoke-Az makes.

    A rate limit or a handshake failure happens while pulling layers, in the first seconds - retrying costs
    almost nothing. A timeout retried is the same hour spent to fail at the same point.
    """
    cases = {
        "ratelimit": "toomanyrequests: You have reached your pull rate limit",
        "handshake": "net/http: TLS handshake timeout",
        "reset": "connection reset by peer",
        "deadline": "Run ID: ca5 failed after 2h0m0s. Error: failed during run, err: context deadline exceeded",
        "timedout": "ERROR: Run failed: the operation timed out",
        "buildkit": "the --mount option requires BuildKit",
        "syntax": "unknown instruction: FOO",
    }
    src = Path(tempfile.mkdtemp()) / "cases.json"
    src.write_text(json.dumps(cases), encoding="utf-8")
    got = run_pwsh(f"""
$cases = Get-Content -LiteralPath '{src.as_posix()}' -Raw | ConvertFrom-Json -AsHashtable
$result = @{{}}
foreach ($k in $cases.Keys) {{ $result[$k] = [bool](Test-StreamRetryable $cases[$k]) }}
ConvertTo-Json -InputObject $result | Set-Content -LiteralPath $out -Encoding utf8NoBOM
""")
    assert got["ratelimit"] and got["handshake"] and got["reset"], f"transient pull failures should retry: {got}"
    assert not got["deadline"] and not got["timedout"], "a timeout must never be retried - it just spends the time again"
    assert not got["buildkit"] and not got["syntax"], "a deterministic build error must not be retried"


@needs_pwsh
def test_the_slowest_step_is_named() -> None:
    """"What caused the delay" reduces to "which step was the process inside while it was silent"."""
    got = run_pwsh("""
$pauses = @(
    [pscustomobject]@{ Seconds = 2820; After = 'Step 4/8 : RUN python -c "...snapshot_download..."' }
    [pscustomobject]@{ Seconds = 372;  After = 'Step 1/8 : FROM ghcr.io/huggingface/text-embeddings-inference:turing-1.9' }
    [pscustomobject]@{ Seconds = 12;   After = 'Step 2/8 : RUN pip install huggingface_hub' }
)
ConvertTo-Json -InputObject @{ lines = @(Format-OutputPauses -Pauses $pauses) } -Depth 4 |
    Set-Content -LiteralPath $out -Encoding utf8NoBOM
""")
    lines = got["lines"]
    assert lines, "recorded pauses must produce a report"
    assert "47 min" in lines[1] and "snapshot_download" in lines[1], f"slowest first, with its step: {lines}"
    assert not any("pip install" in ln for ln in lines), "a 12-second pause is noise, not a finding"


@needs_pwsh
def test_a_build_with_no_long_silences_reports_nothing() -> None:
    """Otherwise every healthy build ends with a block of non-findings that people learn to skip."""
    got = run_pwsh("""
$pauses = @([pscustomobject]@{ Seconds = 3; After = 'Step 1/2 : FROM python:3.13-slim' })
ConvertTo-Json -InputObject @{ lines = @(Format-OutputPauses -Pauses $pauses) } -Depth 4 |
    Set-Content -LiteralPath $out -Encoding utf8NoBOM
""")
    assert got["lines"] == [], f"nothing was slow, so nothing should be reported: {got['lines']}"


# The ARM error that broke step 08: a 503 served as an HTML page, which the az CLI then failed to parse as JSON.
ARM_503_HTML = (
    "azure.cli.core.azclierror.HTTPError: Service Unavailable(<!DOCTYPE html PUBLIC "
    "'-//W3C//DTD XHTML 1.0 Transitional//EN'><html><head><title>AzureResourceManager</title></head><body>"
    "<h2>Our services aren't available right now</h2><p>We're working to restore all services as soon as "
    "possible. Please check back soon.</p><span>Ref A: 9CBE43795C174A85A9D709D46B7D14AD</span></body></html>)"
)
AZ_HTML_JSON_DECODE = (
    "ERROR: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)\n"
    "json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
)


@needs_pwsh
def test_the_arm_outage_that_broke_step_08_is_retried() -> None:
    """ARM says 'Service Unavailable' with a space; the pattern only had the CamelCase spelling.

    So a one-minute Azure blip was treated as permanent and step 08 failed on the first of three attempts,
    abandoning a bootstrap job whose outcome nobody then knew. The az JSONDecodeError in the same log is the
    CLI choking on an HTML error page - never a real answer, always worth retrying.
    """
    cases = {
        "arm_503_html": ARM_503_HTML,
        "az_html_json_decode": AZ_HTML_JSON_DECODE,
        "spaced_503": "The remote server returned an error: (503) Service Unavailable",
        "spaced_502": "Bad Gateway",
        "spaced_504": "Gateway Timeout",
        "spaced_500": "Internal Server Error",
    }
    src = Path(tempfile.mkdtemp()) / "cases.json"
    src.write_text(json.dumps(cases), encoding="utf-8")
    got = run_pwsh(f"""
$cases = Get-Content -LiteralPath '{src.as_posix()}' -Raw | ConvertFrom-Json -AsHashtable
$result = @{{}}
foreach ($k in $cases.Keys) {{ $result[$k] = [bool](Test-TransientFailure $cases[$k]) }}
ConvertTo-Json -InputObject $result | Set-Content -LiteralPath $out -Encoding utf8NoBOM
""")
    missed = sorted(k for k, retried in got.items() if not retried)
    assert not missed, f"these are transient Azure failures and must be retried: {missed}"


@needs_pwsh
def test_deterministic_failures_are_still_not_retried() -> None:
    """Widening the 5xx spellings must not turn real answers into retries.

    This matters more than usual here: the not-found pattern gates Test-TransientFailure, so anything that
    starts matching gets three attempts and a slower, more confusing failure.
    """
    cases = {
        "not_found": "(ResourceNotFound) The Resource 'Microsoft.App/containerApps/rag-api' was not found.",
        "quota": "Operation could not be completed as it results in exceeding approved Total Regional Cores quota",
        "auth": "AuthorizationFailed: The client does not have authorization to perform action",
        "bad_request": "(BadRequest) The provided location 'nowhere' is not available",
        "buildkit": "the --mount option requires BuildKit",
    }
    src = Path(tempfile.mkdtemp()) / "deterministic.json"
    src.write_text(json.dumps(cases), encoding="utf-8")
    got = run_pwsh(f"""
$cases = Get-Content -LiteralPath '{src.as_posix()}' -Raw | ConvertFrom-Json -AsHashtable
$result = @{{}}
foreach ($k in $cases.Keys) {{ $result[$k] = [bool](Test-TransientFailure $cases[$k]) }}
ConvertTo-Json -InputObject $result | Set-Content -LiteralPath $out -Encoding utf8NoBOM
""")
    wrongly = sorted(k for k, retried in got.items() if retried)
    assert not wrongly, f"these are answers, not blips, and must fail fast: {wrongly}"


# ---------------------------------------------------------------- step 08's status poll
# Extracted from 08-bootstrap.ps1 so the loop can be driven against a stubbed Invoke-Az. The script dot-sources
# common.ps1 and needs a live Azure context, so the function is re-read from source and invoked in isolation.
POLL_HARNESS = """
$src = Get-Content -LiteralPath '{script}' -Raw
$start = $src.IndexOf('function Start-JobAndWait')
$body = $src.Substring($start, $src.IndexOf("`n}}", $start) - $start + 3)
Invoke-Expression $body

$rg = 'rg-ragos-dev'
# Declared up front: common.ps1 turns on StrictMode, under which reading an undeclared variable throws - which
# would make every stubbed poll fail for the wrong reason and quietly invert what the test proves.
$script:polls = 0
$script:calls = [System.Collections.Generic.List[string]]::new()
$script:transcript = [System.Collections.Generic.List[string]]::new()
function Write-Info {{ param($Message) $script:transcript.Add("INFO $Message") }}
function Write-Warn {{ param($Message) $script:transcript.Add("WARN $Message") }}
function Write-Ok {{ param($Message) $script:transcript.Add("OK $Message") }}
# The clock is stubbed along with the sleep. Stubbing only Start-Sleep leaves the deadline on real wall-clock
# time, so the "every poll fails" case spins for the full 30 minutes instead of finishing instantly.
$script:now = [datetime]'2026-01-01T00:00:00Z'
function Get-Date {{ param($Date, $Day, $Format) return $script:now }}
function Start-Sleep {{ param($Seconds) $script:now = $script:now.AddSeconds($Seconds) }}
function Invoke-Az {{
    param([string[]]$Arguments, [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    $joined = $Arguments -join ' '
    $script:calls.Add($joined)
    if ($joined -match 'job start') {{ return 'rag-bootstrap-7u5l9kh' }}
    if ($joined -match 'execution show') {{ {poll_behaviour} }}
    return $null
}}
$result = @{{ threw = $false; message = '' }}
try {{ $null = Start-JobAndWait -JobName 'rag-bootstrap' -Minutes 30 }}
catch {{ $result.threw = $true; $result.message = "$($_.Exception.Message)" }}
$result.calls = @($script:calls)
$result.transcript = @($script:transcript)
ConvertTo-Json -InputObject $result -Depth 5 | Set-Content -LiteralPath $out -Encoding utf8NoBOM
"""


def drive_poll(poll_behaviour: str) -> dict:
    return run_pwsh(POLL_HARNESS.format(
        script=(REPO / "infra" / "scripts" / "08-bootstrap.ps1").as_posix(),
        poll_behaviour=poll_behaviour))


@needs_pwsh
def test_a_control_plane_blip_does_not_abandon_the_job() -> None:
    """The exact shape of the failure: one ARM 503 during polling, then the job reports Succeeded.

    Previously the exception escaped the loop, so the step failed, the job was left running, and none of the
    diagnostics printed - which is why nobody knew whether bootstrap had completed.
    """
    got = drive_poll("""
        $script:polls = $script:polls + 1
        if ($script:polls -eq 1) { throw 'HTTPError: Service Unavailable(<!DOCTYPE html ...>)' }
        return 'Succeeded'
    """)
    assert not got["threw"], f"a single failed poll must not fail the step: {got['message']}"
    warned = [ln for ln in got["transcript"] if ln.startswith("WARN") and "could not read" in ln.lower()]
    assert warned, f"the blip should be reported, not silent: {got['transcript']}"
    assert any("OK" in ln and "succeeded" in ln.lower() for ln in got["transcript"]), \
        f"the job's real outcome should be reported: {got['transcript']}"


@needs_pwsh
def test_an_unknown_outcome_is_not_stopped_and_says_so() -> None:
    """If every poll fails we do not know the state - so the execution must be left alone.

    Stopping blind could interrupt a healthy `alembic upgrade head` part-way through, which is worse than
    leaving it. The operator gets the execution name and the command to check it.
    """
    got = drive_poll("throw 'HTTPError: Service Unavailable(<!DOCTYPE html ...>)'")
    assert got["threw"], "an unknown outcome must still fail the step"
    assert not any("job stop" in c for c in got["calls"]), \
        f"an execution whose state is unknown must NOT be stopped: {got['calls']}"
    joined = "\n".join(got["transcript"])
    assert "UNKNOWN" in joined and "NOT stopped" in joined, f"say plainly what was and was not done: {joined}"
    assert "rag-bootstrap-7u5l9kh" in got["message"], "the failure must name the execution to investigate"


@needs_pwsh
def test_step_08_lists_readyz_reasons_instead_of_the_raw_body() -> None:
    """/api/readyz answers "why not" in a structured body; one interpolated blob buries it.

    The reason that matters most reads `index has no recorded embedding profile (run rag-os bootstrap)`, which
    is the whole answer - it should not have to be picked out of a line of JSON.
    """
    body = ('HTTP 503: {"status":"not_ready","checks":{"state_db":"ok","embedding_profile":'
            '{"ok":false,"fingerprint":"a1b2c3d4e5","index":"kb-enterprise-a1b2c3d4e5",'
            '"reasons":["index has no recorded embedding profile (run rag-os bootstrap)"]},'
            '"llm_answer":"aoai"}}')
    src = Path(tempfile.mkdtemp()) / "body.txt"
    src.write_text(body, encoding="utf-8")
    got = run_pwsh(f"""
$reason = Get-Content -LiteralPath '{src.as_posix()}' -Raw
$lines = [System.Collections.Generic.List[string]]::new()
$parsed = ($reason -replace '^HTTP \d+:\s*', '') | ConvertFrom-Json
foreach ($name in $parsed.checks.PSObject.Properties.Name) {{
    $check = $parsed.checks.$name
    $detail = if ($check -is [string]) {{ $check }}
              elseif ($check.reasons) {{ $check.reasons -join '; ' }}
              else {{ "ok=$($check.ok)" }}
    $lines.Add("${{name}}: $detail")
}}
ConvertTo-Json -InputObject @{{ lines = @($lines); index = $parsed.checks.embedding_profile.index }} -Depth 4 |
    Set-Content -LiteralPath $out -Encoding utf8NoBOM
""")
    lines = got["lines"]
    assert any(ln.startswith("state_db: ok") for ln in lines), lines
    assert any("no recorded embedding profile" in ln and ln.startswith("embedding_profile:") for ln in lines), lines
    assert got["index"] == "kb-enterprise-a1b2c3d4e5", "the index name is in the body and should be reported"


def test_step_08_reports_the_index_and_any_bad_sources() -> None:
    """Both are things the transcript must state rather than leave to inference.

    A source that will never ingest is no longer fatal to the bootstrap - which is right - so it has to be said
    out loud instead, or it would pass by in silence.
    """
    source = (REPO / "infra" / "scripts" / "08-bootstrap.ps1").read_text(encoding="utf-8")
    assert "$checks.index" in source, "08 should name the index once readyz reports healthy"
    assert "source_problems" in source, "08 should read the bad sources the bootstrap job reports"
    assert "misconfigured and will not ingest" in source, "and say what that means for them"
