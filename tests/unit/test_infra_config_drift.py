"""Compare-ConfigWithContainer: does a deploy notice that the running config is older than the checkout?

This exists because of a real outage of a feature rather than a hypothetical. The access policy the app reads
lives in a blob container, seeded once by 08-bootstrap.ps1 and then never overwritten - correctly, because a
blanket overwrite had silently destroyed edits admins made through the console. But the script reported only
*that* a file was kept, so a file nobody had touched and a file whose repository copy had gained three new
features printed the same line. Settings (Security) consequently shipped against a policy predating every feature
it needed, and rendered no attributes and no roles while every health check stayed green.

The assertions below are mostly about the ways this check could be worse than useless: reporting drift that is not
real (line endings) would train people to ignore it, and uploading whatever happens to sit in config/ would ship a
developer's error logs into the deployment.
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


def compare(local: dict[str, str], remote: dict[str, str]) -> list[dict]:
    """Run Compare-ConfigWithContainer over a fabricated checkout and container.

    `local` maps a relative path to its contents on disk; `remote` maps a blob name to what the container holds.
    Invoke-Az is shadowed so `storage blob download` copies from the fake container, which is the only az call the
    function makes besides the listing.
    """
    work = Path(tempfile.mkdtemp())
    config_dir, container, out = work / "config", work / "container", work / "out.json"
    container.mkdir(parents=True, exist_ok=True)  # an empty container is a case: nothing seeded yet
    for rel, text in local.items():
        p = config_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    for rel, text in remote.items():
        p = container / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))

    script = work / "probe.ps1"
    script.write_text(f""". '{COMMON.as_posix()}'
$script:Container = '{container.as_posix()}'
function Get-AzTsvValues {{
    param([string[]]$Arguments)
    @(Get-ChildItem -LiteralPath $script:Container -Recurse -File | ForEach-Object {{
        $_.FullName.Substring($script:Container.Length).TrimStart('\\','/').Replace('\\','/')
    }})
}}
function Invoke-Az {{
    param([Parameter(Position = 0)][string[]]$Arguments, [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    if ($Arguments -contains 'download') {{
        $name = $Arguments[[array]::IndexOf($Arguments, '-n') + 1]
        $dest = $Arguments[[array]::IndexOf($Arguments, '-f') + 1]
        Copy-Item -LiteralPath (Join-Path $script:Container $name) -Destination $dest -Force
        return
    }}
    throw "unexpected az call: $($Arguments -join ' ')"
}}
$rows = @(Compare-ConfigWithContainer -StorageAccount 'stub' -ConfigDir '{config_dir.as_posix()}')
(@($rows | ForEach-Object {{ @{{ blob = $_.Blob; status = $_.Status }} }}) |
    ConvertTo-Json -Depth 5 -AsArray) | Set-Content -LiteralPath '{out.as_posix()}' -Encoding utf8NoBOM
""", encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n{done.stdout}\n{done.stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


def status_of(rows: list[dict], blob: str) -> str:
    for row in rows:
        if row["blob"] == blob:
            return str(row["status"])
    raise AssertionError(f"{blob} not in {rows}")


@needs_pwsh
def test_a_changed_file_is_reported_as_different() -> None:
    """The case that shipped: the container holds a policy from before three features were added."""
    rows = compare(
        local={"access-policy/access-policy.yaml": "attributes:\n  - name: department\nallowed_values: [HR]\n"},
        remote={"access-policy/access-policy.yaml": "attributes:\n  - name: department\n"},
    )
    assert status_of(rows, "access-policy/access-policy.yaml") == "Differs"


@needs_pwsh
def test_an_unchanged_file_is_reported_as_identical() -> None:
    same = "sources:\n  - name: handbook\n"
    rows = compare(local={"sources/sources.yaml": same}, remote={"sources/sources.yaml": same})
    assert status_of(rows, "sources/sources.yaml") == "Identical"


@needs_pwsh
def test_a_file_not_in_the_container_is_reported_as_missing() -> None:
    rows = compare(local={"classification/facets.yaml": "facets: []\n"}, remote={})
    assert status_of(rows, "classification/facets.yaml") == "Missing"


@needs_pwsh
def test_a_difference_of_line_endings_alone_is_not_drift() -> None:
    """The false positive that would sink this. A Windows checkout against a blob the admin API rewrote differs
    byte-for-byte on every line; a warning that fires on every deploy is one people stop reading, and then the
    real drift goes past unnoticed too. YAML does not care about CRLF, so neither does the comparison."""
    rows = compare(
        local={"sources/sources.yaml": "sources:\r\n  - name: handbook\r\n"},
        remote={"sources/sources.yaml": "sources:\n  - name: handbook\n"},
    )
    assert status_of(rows, "sources/sources.yaml") == "Identical"


@needs_pwsh
def test_a_byte_order_mark_alone_is_not_drift() -> None:
    """Same reasoning: PowerShell and several editors write a BOM, the API does not."""
    rows = compare(
        local={"sources/sources.yaml": "\ufeffsources:\n  - name: handbook\n"},
        remote={"sources/sources.yaml": "sources:\n  - name: handbook\n"},
    )
    assert status_of(rows, "sources/sources.yaml") == "Identical"


@needs_pwsh
def test_a_file_that_is_not_yaml_is_never_treated_as_configuration() -> None:
    """config/ accumulates local scratch - this repository had errorlog.txt, errorlog1.txt and
    errorlog-bootstrap.log sitting in it. They are gitignored, but a recursive file walk does not consult
    .gitignore, so the next bootstrap run would have uploaded a developer's error logs into the deployment's
    configuration container."""
    rows = compare(
        local={"sources/sources.yaml": "sources: []\n", "errorlog.txt": "a stack trace with a tenant id in it\n"},
        remote={"sources/sources.yaml": "sources: []\n"},
    )
    assert status_of(rows, "errorlog.txt") == "NotConfig"
    assert status_of(rows, "sources/sources.yaml") == "Identical"


# ---------------------------------------------------------------- wiring into the deploy steps


def test_the_deploy_step_warns_about_drift_and_cannot_fail_because_of_it() -> None:
    """07 ships images and nothing else, which is exactly why it has to say when the config is older than them.
    But the check is read-only and advisory, so an unreachable storage account must not turn a successful deploy
    into a failed script - the deploy already happened by then."""
    src = (REPO / "infra" / "scripts" / "07-container-apps.ps1").read_text(encoding="utf-8")
    assert "Compare-ConfigWithContainer" in src, "the deploy step must compare config, not just ship images"
    after = src.split("Write-Ok 'Container Apps deployed.'", 1)[1]
    assert "Compare-ConfigWithContainer" in after, "the check belongs after the deploy, not before it"
    assert "try {" in after and "catch {" in after, (
        "a read-only check must not be able to fail a deploy that already succeeded")
    assert "-PushChanged" in after, "tell the operator how to fix what was found"


def test_the_bootstrap_distinguishes_identical_config_from_stale_config() -> None:
    """The original defect was a single 'left alone' line covering both. A file nobody touched and a file whose
    repository copy had gained three features cannot share a message."""
    src = (REPO / "infra" / "scripts" / "08-bootstrap.ps1").read_text(encoding="utf-8")
    assert "Compare-ConfigWithContainer" in src
    assert "'Differs'" in src and "'Identical'" in src, "the two cases must be told apart"
    assert "DIFFER from this checkout" in src, "and the stale one must say so in words"
    assert "$PushChanged" in src, "with a targeted push that does not overwrite unrelated console edits"


def test_pushing_changed_files_warns_that_it_skips_the_api_validation() -> None:
    """A blob push writes past the admin API, so it skips the validation the console does. That is the one way to
    leave a live deployment holding a policy the running image cannot parse, and the fix is ordering: deploy the
    image first."""
    src = (REPO / "infra" / "scripts" / "08-bootstrap.ps1").read_text(encoding="utf-8")
    push = src.split("$PushChanged) {", 1)[1][:1200]
    assert "FIRST" in push, "the ordering rule has to be stated where the push happens"
    assert "validation" in push or "validates" in push, "and why it matters: this route skips it"
