"""How the provisioning scripts decide that a resource is absent.

`Invoke-Az -AllowNotFound` classifies an az failure by matching its stderr against a regex. That works for ARM,
which has a house style ("(ResourceNotFound) ... was not found"), and fails for the az commands that write their
own prose. One of those - `az keyvault show-deleted` - turned a routine "nothing to recover" probe into a fatal
error on a subscription that had never held a vault, which is where these tests come from.

Two kinds of test, because the defect has two halves:

* behavioural, running the real PowerShell functions in a child pwsh - `common.ps1` only defines functions when
  it loads and never touches Azure, so this needs no credentials and no network;
* structural, plain text over the scripts, guarding the call sites that must not go anywhere near the regex.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "infra" / "scripts"
COMMON = SCRIPTS / "common.ps1"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")


def run_pwsh(body: str) -> None:
    """Dot-source common.ps1 and run `body`. Raises with both streams when the script fails."""
    path = Path(tempfile.mkdtemp()) / "probe.ps1"
    path.write_text(f". '{COMMON.as_posix()}'\n{body}\n", encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(path)],
                          capture_output=True, text=True, cwd=REPO, timeout=180)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n--- stdout ---\n{done.stdout}\n"
                             f"--- stderr ---\n{done.stderr}")


def classify(messages: list[str]) -> dict[str, dict[str, bool]]:
    """Ask the real functions how they read each az stderr. {message: {not_found, transient}}."""
    work = Path(tempfile.mkdtemp())
    src, dst = work / "in.json", work / "out.json"
    src.write_text(json.dumps(messages), encoding="utf-8")
    run_pwsh(f"""
$messages = @(Get-Content -LiteralPath '{src.as_posix()}' -Raw | ConvertFrom-Json)
$rows = foreach ($m in $messages) {{
    [pscustomobject]@{{
        message   = $m
        not_found = [bool]($m -match $script:RagOsNotFoundPattern)
        transient = [bool](Test-TransientFailure $m)
    }}
}}
$rows | ConvertTo-Json -Depth 3 -AsArray | Set-Content -LiteralPath '{dst.as_posix()}' -Encoding utf8NoBOM
""")
    return {r["message"]: r for r in json.loads(dst.read_text(encoding="utf-8"))}


# Collected from the azure-cli source and from a real failed run. These are the reason the affected probes were
# rewritten to use a command that exits 0 rather than taught to the regex: see the comment on RagOsNotFoundPattern.
AZ_PROSE_ABSENCE = [
    "No deleted Vault or HSM was found with name kv-ragosdev-ragos",   # az keyvault show-deleted
    "There are no active accounts.",                                   # az account show
    "Please run 'az login' to setup account.",                         # az account show
    "No subscription found. Run 'az account set' to select a subscription.",
]
# ARM's own wording, which the pattern does handle. The remaining -AllowNotFound probes depend on these.
ARM_ABSENCE = [
    "(ResourceGroupNotFound) Resource group 'rg-ragos-dev' could not be found.",
    "(ResourceNotFound) The Resource 'Microsoft.KeyVault/vaults/kv-x' was not found.",
    "The requested data does not exist.",
]
# Failures that must keep being retried. If a not-found wording ever matched one of these, Test-TransientFailure
# would short-circuit to $false and the retry would silently stop happening - everywhere, not just at one site.
TRANSIENT = [
    "Max retries exceeded with url: /subscriptions/x/providers",
    "(503) Server Error: Service Unavailable",
    "TooManyRequests: Too many requests, please retry after 30 seconds",
    "Operation timed out",
    "Connection reset by peer",
]


@needs_pwsh
def test_az_prose_absence_is_not_recognised_by_the_pattern() -> None:
    """Pins *why* those probes were restructured instead of the regex being widened.

    If a future change makes these match, the probes can be simplified - but read the comment on
    RagOsNotFoundPattern first, because matching them also disables their retries.
    """
    rows = classify(AZ_PROSE_ABSENCE)
    matched = [m for m in AZ_PROSE_ABSENCE if rows[m]["not_found"]]
    assert not matched, f"these now match; revisit the probes that avoid the error path: {matched}"


@needs_pwsh
def test_arm_absence_is_still_recognised() -> None:
    """Narrowing the pattern would turn every surviving -AllowNotFound probe back into a fatal error."""
    rows = classify(ARM_ABSENCE)
    missed = [m for m in ARM_ABSENCE if not rows[m]["not_found"]]
    assert not missed, f"ARM 'absent' wordings no longer classify as not-found: {missed}"


@needs_pwsh
def test_retryable_failures_are_never_read_as_absence() -> None:
    """The coupling that makes widening the pattern dangerous, stated as a test.

    Test-TransientFailure returns $false for anything the not-found pattern matches, so an overlap does not
    merely mislabel one call - it removes the retry from every az call in the repo.
    """
    rows = classify(TRANSIENT)
    broken = [m for m in TRANSIENT if rows[m]["not_found"] or not rows[m]["transient"]]
    assert not broken, f"these must be retried, not treated as absent: {broken}"


@needs_pwsh
def test_save_outputs_keeps_a_good_value_when_handed_nothing(tmp_path: Path) -> None:
    """A probe that returns $null must not erase what an earlier step proved.

    07 reads the chat UI FQDN with -AllowNotFound because `-Only rag-api` legitimately leaves no chat UI. If that
    $null overwrote the stored value, Get-Output would then report it as missing and send the operator back to a
    step that had already succeeded. $false must still be written - it is an answer, not an absence.
    """
    outputs = tmp_path / "dev.outputs.json"
    run_pwsh(f"""
$cfg = @{{ OutputsPath = '{outputs.as_posix()}' }}
Save-Outputs -Config $cfg -Values @{{ chatUiFqdn = 'chat.example.azurecontainerapps.io'; gpuEnabled = $true }}
Save-Outputs -Config $cfg -Values @{{ chatUiFqdn = $null; gpuEnabled = $false }}
""")
    saved = json.loads(outputs.read_text(encoding="utf-8"))
    assert saved["chatUiFqdn"] == "chat.example.azurecontainerapps.io", "a $null erased a known-good output"
    assert saved["gpuEnabled"] is False, "$false is a value and must still be written"


# ------------------------------------------------------------------------------- structural (no pwsh needed)
# az subcommands whose "it is not there" answer the pattern cannot match. A probe built on one of these fails
# the whole script instead of returning $null, which is the bug this file exists for.
BANNED_IN_PROBES = {
    "show-deleted": 'az says "No deleted Vault or HSM was found with name X" - use keyvault list-deleted '
                    "with a [?name=='X'] query, which exits 0",
    # Anchored on the start of the argument array: 'cognitiveservices', 'account', 'show' is an ordinary ARM
    # show and classifies correctly. Only the bare `az account show` writes prose the pattern cannot read.
    "@('account', 'show'": 'az says "There are no active accounts." or "Please run \'az login\'..." - use '
                           "Get-AzAccountOrNull",
}
PROBE_CALLS = re.compile(r"-AllowNotFound|Get-AzResourceOrNull|Test-AzResource")


def logical_lines(text: str) -> list[tuple[int, str]]:
    """(line number, text) with PowerShell continuations folded in, so a wrapped call is scanned as one line."""
    out: list[tuple[int, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        if out and re.search(r"[`,]\s*$", out[-1][1]):
            out[-1] = (out[-1][0], out[-1][1] + " " + raw.strip())
        else:
            out.append((n, raw))
    return out


def test_no_probe_uses_a_command_the_pattern_cannot_classify() -> None:
    offenders = []
    for script in sorted(SCRIPTS.glob("*.ps1")):
        if script.name == "common.ps1":
            continue  # defines the helpers; its own doc comments name them
        for n, line in logical_lines(script.read_text(encoding="utf-8")):
            if not PROBE_CALLS.search(line):
                continue
            for banned, why in BANNED_IN_PROBES.items():
                if banned in line:
                    offenders.append(f"{script.name}:{n} uses `{banned}` as a probe - {why}")
    assert not offenders, "probes that will throw instead of returning $null:\n  " + "\n  ".join(offenders)


def test_a_failed_step_prints_the_error_above_the_resume_instructions() -> None:
    """PowerShell runs `finally` while the exception is still propagating.

    Whatever that block prints therefore lands *before* the error text, so guidance written as "fix the error
    above" points at nothing. The block has to print the captured error itself.
    """
    lines = (SCRIPTS / "provision-all.ps1").read_text(encoding="utf-8").splitlines()
    printed = next((i for i, ln in enumerate(lines) if "Write-Host" in ln and "$errorText" in ln), None)
    resume = next((i for i, ln in enumerate(lines) if "-From $failedAt" in ln), None)
    assert resume is not None, "provision-all.ps1 no longer prints a resume command"
    assert printed is not None, "the failure summary never prints $errorText, so the operator sees no error"
    assert printed < resume, "the error is printed after the resume instructions that refer to it"
