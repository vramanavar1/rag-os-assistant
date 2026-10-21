"""QueryPlan / IngestPlan: which workload profile each tier's templates name, and what the config refuses.

Moving a tier between the Dedicated and Consumption plans is a psd1 edit plus step 07, so the guarantees live in
three places that drift independently: the templates must not pin a profile by name, step 07 must define every
token they use, and Import-RagOsConfig must refuse a worker size the Consumption profile would reject - before
step 07 has moved some apps and not others.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "infra" / "containerapps"
SCRIPTS = REPO / "infra" / "scripts"
ENV_DIR = REPO / "infra" / "env"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")

QUERY_TIER = ("rag-chat-ui", "rag-api", "rag-embed-query")
INGEST_TIER = ("rag-ingest-worker", "rag-scheduler", "rag-bootstrap")


def profile_line(name: str) -> str:
    text = (TEMPLATES / f"{name}.yaml.tmpl").read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*workloadProfileName:\s*(.+?)\s*$", text)
    assert match, f"{name} declares no workloadProfileName"
    return match.group(1)


@pytest.mark.parametrize("name", QUERY_TIER)
def test_query_tier_templates_take_their_profile_from_query_plan(name: str) -> None:
    assert profile_line(name) == '"{{QUERY_PROFILE}}"', (
        f"{name} must not pin a profile by name, or QueryPlan = 'Consumption' would leave it on the D4 nodes")


@pytest.mark.parametrize("name", INGEST_TIER)
def test_ingest_tier_templates_take_their_profile_from_ingest_plan(name: str) -> None:
    assert profile_line(name) == '"{{INGEST_PROFILE}}"', (
        f"{name} must not pin a profile by name, or IngestPlan = 'Consumption' would leave it on the D8 nodes")


def test_step_07_defines_the_profile_tokens_and_cpu_fallback_follows_ingest_plan() -> None:
    src = (SCRIPTS / "07-container-apps.ps1").read_text(encoding="utf-8")
    for token in ("QUERY_PROFILE", "INGEST_PROFILE"):
        assert re.search(rf"(?m)^\s*{token}\s*=", src), f"07's token map does not define {token}"
    assert "EMBED_INGEST_PROFILE          = ($useGpu ? 'gpu-t4' : $ingestProfile)" in src, (
        "on CPU, rag-embed-ingest belongs to the ingest tier and must follow IngestPlan")


@pytest.mark.parametrize("script", ["07-container-apps.ps1", "Set-CostSavingMode.ps1"])
def test_a_profile_is_only_deleted_after_checking_nothing_is_on_it(script: str) -> None:
    """Both delete paths share one emptiness check, so they cannot drift apart."""
    src = (SCRIPTS / script).read_text(encoding="utf-8")
    delete_at = src.index("'workload-profile', 'delete'")
    check = src.rfind("Get-WorkloadProfileUsers -ResourceGroup $rg -ProfileName", 0, delete_at)
    guard = src.rfind("if ($users.Count -gt 0)", check, delete_at)
    assert check != -1 and guard != -1, f"{script}: the delete must sit behind the 'is anything still on it' check"
    common = (SCRIPTS / "common.ps1").read_text(encoding="utf-8")
    helper = common[common.index("function Get-WorkloadProfileUsers"):]
    assert "properties.workloadProfileName==" in helper[:1500] and "'job', 'list'" in helper[:1500], (
        "the check must cover apps AND jobs - a manual job on the profile blocks the delete just the same")


def write_env(settings: dict[str, str]) -> tuple[str, Path]:
    """A throwaway infra/env/<name>.psd1 derived from the sample (which is committed, unlike dev.psd1)."""
    name = "zz" + uuid.uuid4().hex[:6]
    src = (ENV_DIR / "dev.sample.psd1").read_text(encoding="utf-8")
    base = {"Env": f"'{name}'", "SubscriptionId": f"'{uuid.uuid4()}'", "TenantId": f"'{uuid.uuid4()}'"}
    for key, value in {**base, **settings}.items():
        src, count = re.subn(rf"(?m)^(\s*){re.escape(key)}(\s*)=\s*[^\r\n#]*",
                             lambda m, k=key, v=value: f"{m.group(1)}{k}{m.group(2)}= {v} ", src, count=1)
        assert count == 1, f"dev.sample.psd1 has no setting named {key!r}"
    path = ENV_DIR / f"{name}.psd1"
    path.write_text(src, encoding="utf-8")
    return name, path


def load(settings: dict[str, str]) -> str:
    """'LOADED <QueryPlan> <IngestPlan>' or 'ERR: <message>' from the real Import-RagOsConfig."""
    name, path = write_env(settings)
    probe = path.with_suffix(".probe.ps1")
    probe.write_text(
        f". '{(SCRIPTS / 'common.ps1').as_posix()}'\n"
        f"try {{ $c = Import-RagOsConfig -Env '{name}'; \"LOADED $($c.QueryPlan) $($c.IngestPlan)\" }}\n"
        "catch { \"ERR: $($_.Exception.Message)\" }\n", encoding="utf-8")
    try:
        done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(probe)],
                              capture_output=True, text=True, cwd=REPO, timeout=180)
        return done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr
    finally:
        path.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)


@needs_pwsh
def test_the_defaults_keep_todays_dedicated_profiles() -> None:
    assert load({}) == "LOADED Dedicated Dedicated"


@needs_pwsh
def test_both_tiers_can_move_to_consumption() -> None:
    assert load({"QueryPlan": "'Consumption'", "IngestPlan": "'Consumption'"}) == "LOADED Consumption Consumption"


@needs_pwsh
def test_an_unknown_plan_is_refused() -> None:
    out = load({"QueryPlan": "'Serverless'"})
    assert out.startswith("ERR:") and "QueryPlan must be 'Dedicated' or 'Consumption'" in out, out


@needs_pwsh
@pytest.mark.parametrize(("cpu", "memory"), [("3.0", "'4Gi'"), ("6.0", "'12Gi'"), ("2.1", "'4.2Gi'")])
def test_consumption_refuses_a_worker_size_it_cannot_run(cpu: str, memory: str) -> None:
    out = load({"IngestPlan": "'Consumption'", "WorkerCpu": cpu, "WorkerMemory": memory})
    assert out.startswith("ERR:") and "Consumption pair" in out, out


@needs_pwsh
def test_a_dedicated_ingest_tier_keeps_any_worker_size() -> None:
    """The pair rule is a Consumption constraint; a D8 node runs a 3 vCPU / 4Gi worker happily."""
    assert load({"WorkerCpu": "3.0", "WorkerMemory": "'4Gi'"}) == "LOADED Dedicated Dedicated"
