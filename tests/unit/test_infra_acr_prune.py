"""Deleting the wrong image is worse than a full registry.

Images only ever accumulated: nothing in the provisioning scripts removed one, and `Get-ImageTag` hands out a
unique `-dirty-<timestamp>` tag on every run against a dirty tree, so a Basic registry (10 GB) fills quietly.

Pruning is easy. Pruning *safely* is the whole problem, because every workload is deployed by **digest** with
`activeRevisionsMode: Single` and a live revision re-pulls on every scale-out, node move and restart. Remove a
digest it points at and you have not tidied up - you have armed a failure that fires the next time the platform
moves a replica. For the two Container Apps *jobs* it stays invisible until the next cron fire. And nothing here
is recoverable: ACR soft-delete is a preview policy and is not enabled.

Three specifics make a naive "keep the newest N tags" wrong:

* `rag-api`'s image is shared by **four** workloads - api, ingest-worker, scheduler, bootstrap - so checking only
  the app of the same name misses three of them.
* One digest can carry several tags, so "the old tag" can be the current manifest under another name.
* An app can be deployed from a tag the manifest no longer records, which is why the live platform is consulted
  rather than the files alone.

The tests below are mostly about what must NOT be deleted.
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

ACR = "acrtest"
REG = "acrtest.azurecr.io"


def ref(repo: str, digest: str) -> str:
    return f"{REG}/{repo}@{digest}"


def sha(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def run_prune(*, manifests: dict[str, list[dict]], live_apps: dict[str, list[str]],
              live_jobs: dict[str, list[str]], keep: int = 2, outputs: dict | None = None,
              images: dict | None = None, fail_app: str | None = None,
              extra_args: str = "") -> dict:
    """Run Remove-StaleAcrImages with az stubbed, and report what it deleted.

    The stub is keyed on the az argument list, so the assertions are about the decisions the helper makes rather
    than about how it phrases a query.
    """
    work = Path(tempfile.mkdtemp())
    out = work / "result.json"
    env_dir = work / "env"
    env_dir.mkdir()
    (env_dir / "dev.outputs.json").write_text(json.dumps(outputs or {}), encoding="utf-8")
    (env_dir / "dev.images.json").write_text(json.dumps(images or {}), encoding="utf-8")
    fixture = work / "fixture.json"
    fixture.write_text(json.dumps({
        "manifests": manifests, "apps": live_apps, "jobs": live_jobs, "failApp": fail_app,
    }), encoding="utf-8")

    script = work / "probe.ps1"
    script.write_text(f""". '{COMMON.as_posix()}'
$fixture = Get-Content -LiteralPath '{fixture.as_posix()}' -Raw | ConvertFrom-Json -AsHashtable
$script:deleted = [System.Collections.Generic.List[string]]::new()
$script:calls = [System.Collections.Generic.List[string]]::new()

function Invoke-Az {{
    [CmdletBinding()]
    param([Parameter(Mandatory, Position = 0)][string[]]$Arguments, [switch]$AllowNotFound,
          [switch]$Sensitive, [switch]$Stream)
    $line = $Arguments -join ' '
    $script:calls.Add($line)
    if ($line -like 'acr manifest list-metadata*') {{
        $repo = $Arguments[$Arguments.IndexOf('-n') + 1]
        if ($fixture.manifests.ContainsKey($repo)) {{ return $fixture.manifests[$repo] }}
        return @()
    }}
    if ($line -like 'acr manifest delete*') {{
        $script:deleted.Add($Arguments[$Arguments.IndexOf('-n') + 1])
        return ''
    }}
    if ($line -like 'containerapp revision list*') {{
        $app = $Arguments[$Arguments.IndexOf('-n') + 1]
        if ($app -eq $fixture.failApp) {{ throw "the control plane is unavailable" }}
        if ($fixture.apps.ContainsKey($app)) {{ return ($fixture.apps[$app] -join "`n") }}
        return ''
    }}
    if ($line -like 'containerapp job show*') {{
        $job = $Arguments[$Arguments.IndexOf('-n') + 1]
        if ($fixture.jobs.ContainsKey($job)) {{ return ($fixture.jobs[$job] -join "`n") }}
        return ''
    }}
    if ($line -like 'acr repository show*') {{ return '' }}
    return ''
}}

$Config = @{{
    Names       = @{{ ResourceGroup = 'rg-test' }}
    OutputsPath = '{(env_dir / "dev.outputs.json").as_posix()}'
    ImagesPath  = '{(env_dir / "dev.images.json").as_posix()}'
}}
$result = Remove-StaleAcrImages -Config $Config -Registry '{ACR}' `
    -Repositories @({", ".join(f"'{r}'" for r in manifests)}) -Keep {keep} {extra_args}
ConvertTo-Json -Depth 5 -InputObject @{{
    deleted = @($script:deleted); count = [int]$result.Deleted; aborted = [bool]$result.Aborted
    freed = [long]$result.FreedBytes
}} | Set-Content -LiteralPath '{out.as_posix()}' -Encoding utf8NoBOM
""", encoding="utf-8")

    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n{done.stdout}\n{done.stderr}")
    result = json.loads(out.read_text(encoding="utf-8"))
    result["stdout"] = done.stdout
    return result


def manifest(digest: str, tags: list[str], size: int = 100 * 1024 * 1024) -> dict:
    return {"digest": digest, "tags": tags, "size": size, "created": "2026-09-01T00:00:00Z"}


# --------------------------------------------------------------- what must never be deleted
@needs_pwsh
def test_an_image_a_live_revision_uses_is_never_deleted_however_old_it_is() -> None:
    """The test this whole module exists for.

    The in-use digest is deliberately the OLDEST and far outside the retention window, so a "keep the newest N"
    implementation deletes it and this fails.
    """
    old, mid, new = sha(1), sha(2), sha(3)
    got = run_prune(
        manifests={"rag-api": [manifest(new, ["c"]), manifest(mid, ["b"]), manifest(old, ["a"])]},
        live_apps={"rag-api": [ref("rag-api", old)]},
        live_jobs={}, keep=2)
    assert not got["aborted"], got["stdout"]
    assert f"rag-api@{old}" not in got["deleted"], (
        f"the running revision's image was deleted:\n{got['stdout']}")
    # Two newest are retention-kept, the in-use one is protected -> nothing is deletable here at all.
    assert got["deleted"] == [], got["deleted"]


@needs_pwsh
def test_rag_api_is_protected_by_any_of_the_four_workloads_that_share_its_image() -> None:
    """The worker, the scheduler and the bootstrap job all run the rag-api image. Checking the app of the same
    name would leave three ways to delete a live image."""
    for holder, kind in (("rag-ingest-worker", "app"), ("rag-scheduler", "job"), ("rag-bootstrap", "job")):
        old, a, b = sha(10), sha(11), sha(12)
        got = run_prune(
            manifests={"rag-api": [manifest(b, ["b"]), manifest(a, ["a"]), manifest(old, ["old"])]},
            live_apps={holder: [ref("rag-api", old)]} if kind == "app" else {},
            live_jobs={holder: [ref("rag-api", old)]} if kind == "job" else {},
            keep=2)
        assert f"rag-api@{old}" not in got["deleted"], (
            f"{holder} runs that image and it was deleted anyway:\n{got['stdout']}")


@needs_pwsh
def test_a_digest_with_two_tags_is_protected_when_either_tag_is_live() -> None:
    """One manifest, two tags. Deleting "the old tag" removes the manifest the current revision is running."""
    shared, filler1, filler2 = sha(20), sha(21), sha(22)
    got = run_prune(
        manifests={"rag-api": [manifest(filler1, ["x"]), manifest(filler2, ["y"]),
                               manifest(shared, ["old-name", "current"])]},
        live_apps={"rag-api": [ref("rag-api", shared)]},
        live_jobs={}, keep=2)
    assert f"rag-api@{shared}" not in got["deleted"], got["stdout"]


@needs_pwsh
def test_an_unreadable_workload_aborts_the_prune_entirely() -> None:
    """A partial answer is the one input that must never lead to a delete: the digest we could not see is
    exactly the one that might be in use. Retaining too much costs money; this costs an outage."""
    got = run_prune(
        manifests={"rag-api": [manifest(sha(i), [f"t{i}"]) for i in range(30, 36)]},
        live_apps={"rag-api": [ref("rag-api", sha(30))]},
        live_jobs={}, keep=1, fail_app="rag-chat-ui")
    assert got["aborted"] is True, "an unreadable workload must abort, not narrow the protected set"
    assert got["deleted"] == [], f"nothing may be deleted on an incomplete answer:\n{got['stdout']}"
    assert "could not" in got["stdout"].lower()


# --------------------------------------------------------------- what should be deleted
@needs_pwsh
def test_older_unreferenced_generations_are_deleted_beyond_the_retention_window() -> None:
    newest, previous, older, oldest = sha(40), sha(41), sha(42), sha(43)
    got = run_prune(
        manifests={"rag-api": [manifest(newest, ["d"]), manifest(previous, ["c"]),
                               manifest(older, ["b"]), manifest(oldest, ["a"])]},
        live_apps={"rag-api": [ref("rag-api", newest)]},
        live_jobs={}, keep=2)
    assert not got["aborted"], got["stdout"]
    assert sorted(got["deleted"]) == sorted([f"rag-api@{older}", f"rag-api@{oldest}"]), got["deleted"]
    assert got["count"] == 2
    assert got["freed"] == 2 * 100 * 1024 * 1024, "reclaimed size should be reported, not guessed"


@needs_pwsh
def test_retention_counts_in_use_images_towards_the_window() -> None:
    """Keeping N *plus* every in-use image without counting them would quietly retain more than asked."""
    a, b, c = sha(50), sha(51), sha(52)
    got = run_prune(
        manifests={"rag-api": [manifest(a, ["a"]), manifest(b, ["b"]), manifest(c, ["c"])]},
        live_apps={"rag-api": [ref("rag-api", a)]},
        live_jobs={}, keep=2)
    # a is in use (counts as one), b fills the window, so c goes.
    assert got["deleted"] == [f"rag-api@{c}"], got["deleted"]


@needs_pwsh
def test_the_recorded_manifest_and_outputs_also_protect_a_digest() -> None:
    """A workload that exists but cannot be read right now is still covered by what 06 and 07 recorded."""
    live, recorded, stale = sha(60), sha(61), sha(62)
    got = run_prune(
        manifests={"rag-embedder-cpu": [manifest(live, ["c"]), manifest(recorded, ["b"]), manifest(stale, ["a"])]},
        live_apps={}, live_jobs={}, keep=1,
        images={"images": {"rag-embedder-cpu": {"ref": ref("rag-embedder-cpu", recorded)}},
                "embedding": {"serverImages": {"rag-embedder-cpu": ref("rag-embedder-cpu", recorded)}}},
        outputs={"deployedImages": {"rag-embedder-cpu": ref("rag-embedder-cpu", live)}})
    assert f"rag-embedder-cpu@{recorded}" not in got["deleted"], got["stdout"]
    assert f"rag-embedder-cpu@{live}" not in got["deleted"], got["stdout"]
    assert got["deleted"] == [f"rag-embedder-cpu@{stale}"], got["deleted"]


@needs_pwsh
def test_dry_run_reports_without_deleting() -> None:
    got = run_prune(
        manifests={"rag-api": [manifest(sha(i), [f"t{i}"]) for i in range(70, 75)]},
        live_apps={"rag-api": [ref("rag-api", sha(70))]},
        live_jobs={}, keep=1, extra_args="-DryRun")
    assert got["deleted"] == [], "a dry run must not delete"
    assert "would delete" in got["stdout"], f"it still has to say what it would remove:\n{got['stdout']}"


# ------------------------------------------------------------------ the wiring in step 06
BUILD = REPO / "infra" / "scripts" / "06-registry-build.ps1"


def test_the_embedding_provider_is_resolved_before_anything_reads_it() -> None:
    """Under StrictMode, reading a variable that was only assigned inside a skipped `if` is a terminating error.

    `$embedProvider` used to be set inside `if (-not $Images)`, while two later blocks read it - the retention
    step and the manifest writer - so `06 -Images api` would have died on the read rather than on anything to do
    with images. Verified the failure mode in isolation: "The variable '$x' cannot be retrieved because it has
    not been set."
    """
    src = BUILD.read_text(encoding="utf-8")
    assignment = src.index("$embedProvider = Get-EmbeddingProfileProvider")
    guard = src.index("if (-not $Images) {")
    assert assignment < guard, (
        "$embedProvider must be resolved before the -Images conditional, because code after it reads the value "
        "whether or not that branch ran")
    readers = src.count("$embedProvider")
    assert readers >= 3, f"expected the assignment plus at least two readers, found {readers}"


def test_the_prune_runs_before_the_builds_and_is_refusable() -> None:
    """Pruning after the push would reclaim space too late to help the push that needed it."""
    src = BUILD.read_text(encoding="utf-8")
    body = src.split("#>", 1)[1]
    prune = body.index("Remove-StaleAcrImages")
    build = body.index("Invoke-Az -Stream $buildArgs")
    assert prune < build, "retention has to run before the builds, or it frees space after the push it was for"
    assert "[switch]$NoPrune" in src, "a permanent delete must be refusable without editing the script"
    assert "-DryRun:$WhatIfPrune" in src, "and inspectable before it is trusted"


def test_the_size_estimate_reflects_the_measured_images() -> None:
    """The old estimate budgeted a flat 12 GB for any embedder build, against a measured ~4.3 GB - which on a
    10 GB registry is the difference between a spurious warning and a useful one."""
    src = BUILD.read_text(encoding="utf-8")
    assert "$needGb = if ($Images -match 'embedder') { 4.5 } else { 0.3 }" in src, (
        "the per-build estimate should come from the measured compressed sizes")
    assert "{ 12 }" not in src, "the old 12 GB guess should be gone"
