"""Set-CostSavingMode.ps1 against an in-memory fake of Azure.

The promise that matters is "Resume puts back exactly what was running". It rests on one rule - the snapshot is
written once, before the first change, and never again while cost saving is on - and on applying and reversing
every change in the right order. Hibernate now also deletes the Dedicated profiles (the management fee is charged
for as long as any exists), which means moving stopped apps and jobs to Consumption first and moving them back on
Resume. None of that needs Azure: the script's own functions are loaded from its AST and run with Invoke-Az /
Invoke-AzRest replaced by a fake that keeps state and, like Azure, refuses to delete a profile anything is on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra" / "scripts" / "Set-CostSavingMode.ps1"
COMMON = REPO / "infra" / "scripts" / "common.ps1"
SAMPLE = REPO / "infra" / "env" / "dev.sample.psd1"
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")

HARNESS = r"""
$ErrorActionPreference = 'Stop'
. '__COMMON__'
$work = '__WORK__'
$Config = Import-PowerShellDataFile -LiteralPath '__SAMPLE__'
$Config.Names = @{
    ResourceGroup = 'rg-t'; ContainerEnv = 'cae-t'; Search = 'srch-t'; Postgres = 'psql-t'; ServiceBus = 'sb-t'
}
$Config.ResourceGroupId = '/subscriptions/s/resourceGroups/rg-t'
$Config.EnvName = 't'
$Config.OutputsPath = Join-Path $work 't.outputs.json'
$Env = 't'; $n = $Config.Names; $rg = $n.ResourceGroup
$script:AppsUrl = "https://management.azure.com$($Config.ResourceGroupId)/providers/Microsoft.App/containerApps"
$script:AppsApiVersion = '2025-01-01'
$SnapshotPath = Join-Path $work 't.costsaver.json'
$HibernateCron = '0 0 1 1 *'
$StopOrder = @('rag-chat-ui', 'rag-api', 'rag-ingest-worker', 'rag-embed-query', 'rag-embed-ingest')
$Force = $true; $AllowDuringBootstrap = $false; $KeepSearchReplicas = $false; $KeepPostgres = $false
$KeepDedicatedProfiles = $false; $NoWait = $true; $UseConfigDefaults = $false; $ScaleDownCron = '0 * * * *'
$script:DryRun = $false

$ast = [System.Management.Automation.Language.Parser]::ParseFile('__SCRIPT__', [ref]$null, [ref]$null)
$isFunction = { $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }
foreach ($f in @($ast.EndBlock.Statements | Where-Object { & $isFunction $_ })) {
    . ([scriptblock]::Create($f.Extent.Text))
}
# Capture what the operator is told, so the tests can check it names the right things.
$global:Warnings = [System.Collections.Generic.List[string]]::new()
function Write-Warn { param([string]$Message) $global:Warnings.Add($Message) }

# ------------------------------------------------------------------ the fake: state plus a log of every change
function New-App($s, $min, $max, $wp, $cpu, $mem) { @{ s = $s; min = $min; max = $max; wp = $wp; cpu = $cpu; mem = $mem } }
function Reset-Fake {
    $global:F = @{
        apps = [ordered]@{
            'rag-chat-ui' = New-App 'Running' 1 3 'query' 0.5 '1Gi'
            'rag-api' = New-App 'Running' 3 10 'query' 1.0 '2Gi'
            'rag-ingest-worker' = New-App 'Running' 0 10 'ingest' 2.0 '4Gi'
            'rag-embed-query' = New-App 'Running' 1 4 'query' 2.0 '4Gi'
            'rag-embed-ingest' = New-App 'Stopped' 0 4 'gpu-t4' 8.0 '56Gi'
        }
        jobs = [ordered]@{
            'rag-scheduler' = @{ trigger = 'Schedule'; cron = '*/5 * * * *'; wp = 'ingest'; cpu = 1.0; mem = '2Gi' }
            'rag-bootstrap' = @{ trigger = 'Manual'; cron = $null; wp = 'ingest'; cpu = 1.0; mem = '2Gi' }
        }
        profiles = [ordered]@{
            query = @{ t = 'D4'; min = 2; max = 6 }; ingest = @{ t = 'D8'; min = 1; max = 10 }
            'gpu-t4' = @{ t = 'Consumption-GPU-NC8as-T4'; min = $null; max = $null }
        }
        search = 3; pg = 'Ready'; bootstrapRunning = $false; updateStartsApps = $false; refuseMove = @()
        calls = [System.Collections.Generic.List[string]]::new()
    }
}
function Get-Arg($a, $flag) { $i = [array]::IndexOf($a, $flag); if ($i -ge 0) { $a[$i + 1] } }
function Get-Users($wp) {
    @($F.apps.Keys | Where-Object { $F.apps[$_].wp -eq $wp }) + @($F.jobs.Keys | Where-Object { $F.jobs[$_].wp -eq $wp })
}
function Set-Profile($kind, $a) {
    $name = Get-Arg $a '-n'; $to = Get-Arg $a '--workload-profile-name'
    if ($F.refuseMove -contains $name) {
        throw "(ContainerAppInvalidWorkloadProfile) The workload profile of $name cannot be changed."
    }
    if ($kind -eq 'app') {
        $F.apps[$name].wp = $to
        if ($F.updateStartsApps) { $F.apps[$name].s = 'Running' }
    }
    else { $F.jobs[$name].wp = $to }
}
function Invoke-Az {
    param([Parameter(Mandatory, Position = 0)][string[]]$Arguments,
        [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    $j = $Arguments -join ' '
    $usersOf = if ($j -match "workloadProfileName=='([^']+)'") { $Matches[1] } else { $null }
    switch -Wildcard ($j) {
        'containerapp list*' { return (@($F.apps.Keys | Where-Object { $F.apps[$_].wp -eq $usersOf }) -join "`n") }
        'containerapp job list*' {
            if ($usersOf) { return (@($F.jobs.Keys | Where-Object { $F.jobs[$_].wp -eq $usersOf }) -join "`n") }
            return @($F.jobs.Keys | ForEach-Object {
                    $x = $F.jobs[$_]
                    $config = @{ triggerType = $x.trigger; scheduleTriggerConfig = @{ cronExpression = $x.cron } }
                    $containers = @(@{ resources = @{ cpu = $x.cpu; memory = $x.mem } })
                    @{ name = $_; properties = @{ workloadProfileName = $x.wp; configuration = $config
                            template = @{ containers = $containers } } } })
        }
        'containerapp job update*' {
            $F.calls.Add($j)
            if ($Arguments -contains '--workload-profile-name') { Set-Profile 'job' $Arguments; return }
            $F.jobs[(Get-Arg $Arguments '-n')].cron = Get-Arg $Arguments '--cron-expression'; return
        }
        'containerapp job execution list*' { return ($F.bootstrapRunning ? 'rag-bootstrap-abc' : '') }
        'containerapp env show*' {
            return @($F.profiles.Keys | ForEach-Object {
                    @{ name = $_; workloadProfileType = $F.profiles[$_].t
                        minimumCount = $F.profiles[$_].min; maximumCount = $F.profiles[$_].max } })
        }
        'containerapp env workload-profile update*' {
            $F.calls.Add($j)
            $p = $F.profiles[(Get-Arg $Arguments '--workload-profile-name')]
            $p.min = [int](Get-Arg $Arguments '--min-nodes'); $p.max = [int](Get-Arg $Arguments '--max-nodes'); return
        }
        'containerapp env workload-profile add*' {
            $F.calls.Add($j)
            $F.profiles[(Get-Arg $Arguments '--workload-profile-name')] = @{ t = Get-Arg $Arguments '--workload-profile-type'
                min = [int](Get-Arg $Arguments '--min-nodes'); max = [int](Get-Arg $Arguments '--max-nodes') }
            return
        }
        'containerapp env workload-profile delete*' {
            $F.calls.Add($j)
            $wp = Get-Arg $Arguments '--workload-profile-name'
            $users = @(Get-Users $wp)
            if ($users.Count) { throw "(WorkloadProfileInUse) Workload profile $wp is used by $($users -join ', ')." }
            $F.profiles.Remove($wp); return
        }
        'containerapp update*' {
            $F.calls.Add($j)
            if ($Arguments -contains '--workload-profile-name') { Set-Profile 'app' $Arguments; return }
            $app = $F.apps[(Get-Arg $Arguments '-n')]
            $v = Get-Arg $Arguments '--min-replicas'; if ($v) { $app.min = [int]$v }
            $v = Get-Arg $Arguments '--max-replicas'; if ($v) { $app.max = [int]$v }; return
        }
        'search service show*' { return "$($F.search)" }
        'search service update*' { $F.calls.Add($j); $F.search = [int](Get-Arg $Arguments '--replica-count'); return }
        'postgres flexible-server show*' { return $F.pg }
        'postgres flexible-server start*' { $F.calls.Add($j); $F.pg = 'Ready'; return }
        'postgres flexible-server stop*' { $F.calls.Add($j); $F.pg = 'Stopped'; return }
        'servicebus queue show*' { return '0' }
        default { throw "unexpected az $j" }
    }
}
function Invoke-AzRest {
    param([string]$Method, [string]$Url, [object]$Body, [switch]$AllowNotFound, [switch]$Sensitive)
    $toApp = {
        param($name)
        $x = $F.apps[$name]
        $template = @{ scale = @{ minReplicas = $x.min; maxReplicas = $x.max }
            containers = @(@{ resources = @{ cpu = $x.cpu; memory = $x.mem } }) }
        @{ name = $name; properties = @{ runningStatus = $x.s; workloadProfileName = $x.wp; template = $template } }
    }
    if ($Url -match '/containerApps\?') { return @{ value = @($F.apps.Keys | ForEach-Object { & $toApp $_ }) } }
    if ($Url -match '/containerApps/([^/?]+)/(stop|start)\?') {
        $F.calls.Add("$($Matches[2]) $($Matches[1])")
        $F.apps[$Matches[1]].s = ($Matches[2] -eq 'stop') ? 'Stopped' : 'Running'; return
    }
    if ($Url -match '/containerApps/([^/?]+)\?') { return (& $toApp $Matches[1]) }
    throw "unexpected rest $Method $Url"
}
function State { Get-CurrentState | ConvertTo-Json -Depth 10 -Compress }
function Run($mode) {
    $script:Mode = $mode; $F.calls.Clear()
    $snapshot = Read-Snapshot; $current = Get-CurrentState
    switch ($mode) {
        'Status' { $null = Show-Status -Current $current -Snapshot $snapshot }
        'Resume' { Invoke-Resume -Current $current -Snapshot $snapshot }
        default { Invoke-Saving -Current $current -Snapshot $snapshot }
    }
    return @($F.calls)
}
function Snapshot { Get-Content $SnapshotPath -Raw | ConvertFrom-Json -AsHashtable }
function SnapshotState { (Snapshot).state | ConvertTo-Json -Depth 10 -Compress }
function Clear-Snapshots { Get-ChildItem $work -Filter 't.costsaver*.json' | Remove-Item }
$r = [ordered]@{}

# ------------------------------------------------- 1. the full cycle; the profile move also starts the app
Reset-Fake; $F.updateStartsApps = $true
$r.initial = State
$r.hibernateCalls = @(Run 'Hibernate')
$r.afterHibernate = State
$r.optionsAfterHibernate = (Snapshot).options
$r.snapshotAfterFirst = SnapshotState
$r.rehibernateCalls = @(Run 'Hibernate')   # @(): an empty array returned from a function arrives as $null
$r.snapshotAfterSecond = SnapshotState
$null = Run 'Status'
$F.pg = 'Ready'                                   # Azure's automatic start after 7 days
$r.gapsAfterAutoStart = @(Get-SavingGaps -Snapshot (Read-Snapshot) -Current (Get-CurrentState))
$F.profiles['query'] = @{ t = 'D4'; min = 2; max = 6 }   # a step 07 deploy re-adds a profile
$r.gapsAfterReAdd = @(Get-SavingGaps -Snapshot (Read-Snapshot) -Current (Get-CurrentState))
$F.profiles.Remove('query')
$r.scaleDownCalls = @(Run 'ScaleDown')
$r.afterScaleDown = State
$r.snapshotAfterScaleDown = SnapshotState
$r.resumeCalls = @(Run 'Resume')
$r.final = State
$r.snapshotLeft = Test-Path $SnapshotPath
$r.archived = @(Get-ChildItem $work -Filter 't.costsaver.*.resumed.json').Count

# ------------------------------------------------- 2. -WhatIf: lists, changes nothing, writes nothing
Clear-Snapshots; Reset-Fake; $script:DryRun = $true
$r.dryRunCalls = @(Run 'Hibernate')
$r.dryRunWroteSnapshot = Test-Path $SnapshotPath
$script:DryRun = $false

# ------------------------------------------------- 3. the migration guard holds even with -Force
Clear-Snapshots; Reset-Fake; $F.bootstrapRunning = $true
try { $null = Run 'Hibernate'; $r.guard = 'not enforced' } catch { $r.guard = "$($_.Exception.Message)" }
$r.guardWroteSnapshot = Test-Path $SnapshotPath
$r.guardCalls = @($F.calls)

# ------------------------------------------------- 4. a worker too big for Consumption keeps 'ingest' only
Clear-Snapshots; Reset-Fake; $F.apps['rag-ingest-worker'].cpu = 3.0; $global:Warnings.Clear()
$misfitInitial = State
$null = Run 'Hibernate'
$r.misfitProfiles = @($F.profiles.Keys)
$r.misfitWarnings = @($global:Warnings)
$r.misfitKept = @((Snapshot).options.keptProfiles)
$r.misfitGaps = @(Get-SavingGaps -Snapshot (Read-Snapshot) -Current (Get-CurrentState))
$null = Run 'Resume'
$r.misfitInitial = $misfitInitial
$r.misfitResumed = State

# ------------------------------------------------- 5. Azure refusing one move keeps that profile only
Clear-Snapshots; Reset-Fake; $F.refuseMove = @('rag-api'); $global:Warnings.Clear()
$refusedInitial = State
$null = Run 'Hibernate'
$r.refusedProfiles = @($F.profiles.Keys)
$r.refusedWarnings = @($global:Warnings)
$F.refuseMove = @()
$null = Run 'Resume'
$r.refusedInitial = $refusedInitial
$r.refusedResumed = State

# ------------------------------------------------- 6. -KeepDedicatedProfiles deletes nothing
Clear-Snapshots; Reset-Fake; $KeepDedicatedProfiles = $true
$r.keepCalls = @(Run 'Hibernate')
$r.keepProfiles = @($F.profiles.Keys)
$KeepDedicatedProfiles = $false

# ------------------------------------------------- 7. an old-format snapshot, as written by the previous version
Clear-Snapshots; Reset-Fake
$original = State
# The previous version's Hibernate: everything stopped, nodes at 0, nothing moved, no assignments recorded.
foreach ($a in $F.apps.Values) { $a.s = 'Stopped' }
$F.profiles.query.min = 0; $F.profiles.ingest.min = 0; $F.search = 1; $F.pg = 'Stopped'
$F.jobs['rag-scheduler'].cron = '0 0 1 1 *'
$old = [ordered]@{
    takenAt = '2026-10-21 06:12 UTC'; env = 't'; resourceGroup = 'rg-t'
    state = [ordered]@{
        apps = [ordered]@{
            'rag-chat-ui' = @{ runningStatus = 'Running'; minReplicas = 1; maxReplicas = 3 }
            'rag-api' = @{ runningStatus = 'Running'; minReplicas = 3; maxReplicas = 10 }
            'rag-ingest-worker' = @{ runningStatus = 'Running'; minReplicas = 0; maxReplicas = 10 }
            'rag-embed-query' = @{ runningStatus = 'Running'; minReplicas = 1; maxReplicas = 4 }
            'rag-embed-ingest' = @{ runningStatus = 'Stopped'; minReplicas = 0; maxReplicas = 4 }
        }
        jobs = @{ 'rag-scheduler' = '*/5 * * * *' }
        profiles = [ordered]@{ query = @{ type = 'D4'; minimumCount = 2; maximumCount = 6 }
            ingest = @{ type = 'D8'; minimumCount = 1; maximumCount = 10 } }
        searchReplicas = 3; postgresState = 'Ready'
    }
    mode = 'Hibernate'; updatedAt = '2026-10-21 06:12 UTC'
    options = @{ schedulerCron = '0 0 1 1 *'; keepSearchReplicas = $false; keepPostgres = $false }
}
$old | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $SnapshotPath -Encoding utf8NoBOM
$r.oldGapsBefore = @(Get-SavingGaps -Snapshot (Read-Snapshot) -Current (Get-CurrentState))
$null = Run 'Hibernate'
$r.oldProfilesAfter = @($F.profiles.Keys)
$r.oldSnapshotAfter = (Snapshot).state
$null = Run 'Resume'
$r.oldOriginal = $original
$r.oldResumed = State

$r | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $work 'result.json') -Encoding utf8NoBOM
"""


@pytest.fixture(scope="module")
def result() -> dict[str, Any]:
    work = Path(tempfile.mkdtemp())
    body = (HARNESS.replace("__COMMON__", COMMON.as_posix()).replace("__SCRIPT__", SCRIPT.as_posix())
            .replace("__SAMPLE__", SAMPLE.as_posix()).replace("__WORK__", work.as_posix()))
    harness = work / "harness.ps1"
    harness.write_text(body, encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(harness)],
                          capture_output=True, text=True, cwd=REPO, timeout=300)
    out = work / "result.json"
    assert done.returncode == 0 and out.exists(), f"harness failed\n{done.stdout[-4000:]}\n{done.stderr[-4000:]}"
    return json.loads(out.read_text(encoding="utf-8"))


def state(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def profiles_of(s: dict[str, Any]) -> dict[str, str]:
    """Every workload's profile assignment, apps and jobs."""
    return ({f"app {k}": v["workloadProfileName"] for k, v in s["apps"].items()}
            | {f"job {k}": v["workloadProfileName"] for k, v in s["jobProfiles"].items()})


# ------------------------------------------------------------------------------------------- the full cycle
def test_resume_restores_exactly_what_was_running(result: dict[str, Any]) -> None:
    assert state(result["final"]) == state(result["initial"])


def test_hibernate_stops_everything_that_bills(result: dict[str, Any]) -> None:
    s = state(result["afterHibernate"])
    assert all(app["runningStatus"] == "Stopped" for app in s["apps"].values()), (
        "the profile move started apps in this run; Hibernate must stop them again")
    assert s["jobs"]["rag-scheduler"] == "0 0 1 1 *"
    assert s["searchReplicas"] == 1 and s["postgresState"] == "Stopped"


def test_hibernate_deletes_the_dedicated_profiles_so_the_management_fee_stops(result: dict[str, Any]) -> None:
    s = state(result["afterHibernate"])
    assert s["profiles"] == {}, "no Dedicated profile may remain - the fee is charged while any exists"
    assignments = profiles_of(s)
    assert assignments.pop("app rag-embed-ingest") == "gpu-t4", "the serverless GPU profile carries no fee"
    assert set(assignments.values()) == {"Consumption"}, f"everything else moves to Consumption: {assignments}"
    assert assignments["job rag-bootstrap"] == "Consumption", "a MANUAL job sits on 'ingest' too and must move"
    assert result["optionsAfterHibernate"]["removedProfiles"] == ["ingest", "query"]


def test_resume_re_adds_the_profiles_and_moves_everything_back_before_starting(result: dict[str, Any]) -> None:
    final, initial = state(result["final"]), state(result["initial"])
    assert final["profiles"] == initial["profiles"]
    assert profiles_of(final) == profiles_of(initial)
    calls = result["resumeCalls"]
    adds = [i for i, c in enumerate(calls) if "workload-profile add" in c]
    moves = [i for i, c in enumerate(calls) if "--workload-profile-name" in c and "env workload-profile" not in c]
    starts = [i for i, c in enumerate(calls) if c.startswith("start ")]
    assert adds and moves, calls
    assert max(adds) < min(moves), "a profile must exist before anything is moved onto it"
    assert not starts or min(moves) < min(starts), "move a stopped app, never a running one"


def test_hibernate_stops_the_front_door_first(result: dict[str, Any]) -> None:
    stops = [c.split()[1] for c in result["hibernateCalls"] if c.startswith("stop ")]
    assert stops[:4] == ["rag-chat-ui", "rag-api", "rag-ingest-worker", "rag-embed-query"], (
        "front door first, and an app that was already stopped (rag-embed-ingest) is not touched")
    assert result["hibernateCalls"][-1].startswith("postgres flexible-server stop"), "the database goes last"


def test_the_snapshot_is_never_overwritten_while_saving(result: dict[str, Any]) -> None:
    """Re-recording the scaled-down state as the 'original' would make Resume restore the scaled-down state."""
    first = state(result["snapshotAfterFirst"])
    assert first == state(result["initial"])
    assert state(result["snapshotAfterSecond"]) == first
    assert state(result["snapshotAfterScaleDown"]) == first


def test_repeating_a_mode_changes_nothing(result: dict[str, Any]) -> None:
    assert result["rehibernateCalls"] == []


def test_postgres_auto_start_is_reported_as_a_gap(result: dict[str, Any]) -> None:
    assert any("PostgreSQL is running again" in g for g in result["gapsAfterAutoStart"])


def test_a_re_added_profile_is_reported_as_billing_the_fee(result: dict[str, Any]) -> None:
    assert any("Dedicated profile query exists" in g and "management fee" in g for g in result["gapsAfterReAdd"])


def test_scale_down_from_hibernate_brings_back_only_what_was_running(result: dict[str, Any]) -> None:
    s = state(result["afterScaleDown"])
    assert s["apps"]["rag-embed-ingest"]["runningStatus"] == "Stopped", "it was stopped before cost saving began"
    for name in ("rag-chat-ui", "rag-api", "rag-ingest-worker", "rag-embed-query"):
        assert s["apps"][name]["runningStatus"] == "Running" and s["apps"][name]["minReplicas"] == 0
    assert s["jobs"]["rag-scheduler"] == "0 * * * *"
    assert s["postgresState"] == "Ready"


def test_resume_archives_the_snapshot(result: dict[str, Any]) -> None:
    assert result["snapshotLeft"] is False and result["archived"] == 1


# ------------------------------------------------------------------------------------------- safety
def test_what_if_changes_nothing_and_writes_nothing(result: dict[str, Any]) -> None:
    assert result["dryRunCalls"] == [] and result["dryRunWroteSnapshot"] is False


def test_force_does_not_override_the_running_migration_guard(result: dict[str, Any]) -> None:
    assert "rag-bootstrap is running" in result["guard"]
    assert result["guardCalls"] == [] and result["guardWroteSnapshot"] is False


def test_a_workload_too_big_for_consumption_keeps_only_its_own_profile(result: dict[str, Any]) -> None:
    assert result["misfitProfiles"] == ["ingest", "gpu-t4"], "query is still removed; only ingest is kept"
    assert any("rag-ingest-worker 3 vCPU" in w and "profile ingest kept" in w for w in result["misfitWarnings"])
    assert result["misfitKept"] == ["ingest"]
    assert not any("Dedicated profile" in g for g in result["misfitGaps"]), (
        "a profile kept for a stated reason is not reported again as an unexplained gap")
    assert state(result["misfitResumed"]) == state(result["misfitInitial"])


def test_a_refused_move_keeps_that_profile_and_the_rest_proceed(result: dict[str, Any]) -> None:
    assert result["refusedProfiles"] == ["query", "gpu-t4"], "ingest is still removed"
    assert any("rag-api" in w and "query" in w for w in result["refusedWarnings"])
    assert state(result["refusedResumed"]) == state(result["refusedInitial"])


def test_keep_dedicated_profiles_deletes_nothing(result: dict[str, Any]) -> None:
    assert result["keepProfiles"] == ["query", "ingest", "gpu-t4"]
    assert not any("--workload-profile-name" in c and "env workload-profile" not in c for c in result["keepCalls"])
    assert not any("workload-profile delete" in c for c in result["keepCalls"])


# ------------------------------------------------------------------------------------------- old snapshots
def test_an_old_format_snapshot_is_reported_as_still_billing_the_fee(result: dict[str, Any]) -> None:
    assert any("Dedicated profile query exists" in g for g in result["oldGapsBefore"])


def test_an_old_format_snapshot_is_completed_not_replaced(result: dict[str, Any]) -> None:
    snap = result["oldSnapshotAfter"]
    assert snap["apps"]["rag-api"]["minReplicas"] == 3 and snap["apps"]["rag-api"]["runningStatus"] == "Running", (
        "the recorded original values must survive")
    assert snap["apps"]["rag-api"]["workloadProfileName"] == "query"
    assert snap["jobProfiles"]["rag-bootstrap"]["workloadProfileName"] == "ingest"
    assert snap["profiles"]["query"]["minimumCount"] == 2, "the original node floor, not the hibernated 0"
    assert result["oldProfilesAfter"] == ["gpu-t4"]


def test_resume_from_an_old_format_snapshot_restores_the_original(result: dict[str, Any]) -> None:
    assert state(result["oldResumed"]) == state(result["oldOriginal"])
