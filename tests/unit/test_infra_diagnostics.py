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
