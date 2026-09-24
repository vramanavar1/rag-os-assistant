"""Re-running a provisioning step must converge, not just survive.

The scripts were idempotent in the weak sense - nothing errored on a second run - but `Ensure-AzResource` was
show-then-create with no reconciliation, so every SKU, retention day and tag in `-Create` was applied once and
silently ignored forever after, while the transcript still reported a green `(exists)`. `Sync-AzResource` is the
missing half: compare the live resource against the psd1 and update only what differs.

The comparison is a pure function over a hashtable, so all of it tests without Azure. What cannot be tested here
is whether each az flag is spelled correctly - that needs a real subscription - so these tests pin the decision
(update or not, which flags, which warning) rather than the wire call.
"""

from __future__ import annotations

import json
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


def run_pwsh(body: str) -> str:
    """Dot-source common.ps1 and run `body`. Returns stdout; raises with both streams on failure."""
    path = Path(tempfile.mkdtemp()) / "probe.ps1"
    path.write_text(f". '{COMMON.as_posix()}'\n{body}\n", encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(path)],
                          capture_output=True, text=True, cwd=REPO, timeout=180)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n--- stdout ---\n{done.stdout}\n"
                             f"--- stderr ---\n{done.stderr}")
    return done.stdout


def sync(resource: dict, specs: str, *, apply_changes: bool = False) -> dict:
    """Run Sync-AzResource against a literal resource, with Invoke-Az stubbed. Returns what it decided.

    The stub records the command instead of calling az, so the test sees exactly which flags would go over the
    wire - including, crucially, that no call happens at all when nothing drifted.
    """
    work = Path(tempfile.mkdtemp())
    src, dst = work / "resource.json", work / "result.json"
    src.write_text(json.dumps(resource), encoding="utf-8")
    flag = "-ApplyChanges" if apply_changes else ""
    out = run_pwsh(f"""
$script:calls = [System.Collections.Generic.List[object]]::new()
function Invoke-Az {{ param([string[]]$Arguments, [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    $script:calls.Add(($Arguments -join ' ')); return $null
}}
$resource = Get-Content -LiteralPath '{src.as_posix()}' -Raw | ConvertFrom-Json -AsHashtable
$drift = Sync-AzResource -Description 'thing' -Resource $resource -Update @('svc', 'update') {flag} -Desired @(
{specs}
)
ConvertTo-Json -Depth 5 -InputObject @{{
    applied  = @($drift.Applied)
    deferred = @($drift.Deferred)
    calls    = @($script:calls)
}} | Set-Content -LiteralPath '{dst.as_posix()}' -Encoding utf8NoBOM
""")
    result = json.loads(dst.read_text(encoding="utf-8"))
    result["console"] = out
    return result


@needs_pwsh
def test_matching_values_produce_no_call_at_all() -> None:
    """The whole point: a converged resource must be read, not written.

    Blindly re-applying is what 03-data.ps1 did to its queues - an unconditional PUT on every run whose comment
    claimed it brought 'mutable properties to the desired state' without ever comparing anything.
    """
    result = sync({"sku": {"name": "standard"}, "retentionDays": 90},
                  "(New-DesiredProperty -Path 'sku.name' -Desired 'standard' -Arg '--sku'),\n"
                  "(New-DesiredProperty -Path 'retentionDays' -Desired 90 -Arg '--retention-days')")
    assert result["calls"] == [], "nothing drifted, so nothing should have been sent"
    assert result["applied"] == []
    assert "(exists)" in result["console"]


@needs_pwsh
def test_only_the_changed_attribute_is_sent() -> None:
    """'those alone need to be updated' - one call, carrying only the flags that differ."""
    result = sync({"sku": {"name": "standard"}, "retentionDays": 90},
                  "(New-DesiredProperty -Path 'sku.name' -Desired 'standard' -Arg '--sku'),\n"
                  "(New-DesiredProperty -Path 'retentionDays' -Desired 7 -Arg '--retention-days')")
    assert result["calls"] == ["svc update --retention-days 7 -o none"], "only the drifted flag belongs on the call"
    assert result["applied"] == ["retentionDays 90->7"]
    assert "(updated: retentionDays 90->7)" in result["console"]


@needs_pwsh
def test_types_that_look_different_but_are_not() -> None:
    """az returns numbers and booleans typed; the psd1 and the command line deal in strings.

    Comparing them raw reports drift that is not there and then 'corrects' it on every single run, which is
    exactly the phantom-update trap 04-search.ps1 sits on with its null semanticSearch.
    """
    result = sync({"capacity": 50, "enabled": True, "tier": "Standard", "absent": None},
                  "(New-DesiredProperty -Path 'capacity' -Desired '50' -Arg '--capacity'),\n"
                  "(New-DesiredProperty -Path 'enabled' -Desired $true -Arg '--enabled'),\n"
                  "(New-DesiredProperty -Path 'tier' -Desired 'standard' -Arg '--tier'),\n"
                  "(New-DesiredProperty -Path 'absent' -Desired '' -Arg '--absent')")
    assert result["calls"] == [], f"none of these differ; got {result['calls']}"


@needs_pwsh
def test_a_disruptive_change_is_reported_but_not_applied() -> None:
    """Resizing a database or scaling Search costs money and restarts things. It waits to be asked."""
    specs = "(New-DesiredProperty -Path 'storageGb' -Desired 128 -Arg '--storage-size' -Class 'gated')"
    held = sync({"storageGb": 32}, specs)
    assert held["calls"] == [], "a gated change must not be applied without -ApplyChanges"
    assert held["deferred"] == ["storageGb 32->128"]
    assert "-ApplyChanges" in held["console"], "the operator has to be told how to apply it"

    applied = sync({"storageGb": 32}, specs, apply_changes=True)
    assert applied["calls"] == ["svc update --storage-size 128 -o none"]
    assert applied["deferred"] == []


@needs_pwsh
def test_an_immutable_change_warns_with_the_remedy_and_never_writes() -> None:
    """SearchSku cannot change in place - it needs a new service and a full re-index.

    Today the script says nothing at all and prints the desired SKU in its header, so the transcript claims a
    SKU the service does not have.
    """
    result = sync({"sku": {"name": "standard"}},
                  "(New-DesiredProperty -Path 'sku.name' -Desired 'standard2' -Class 'immutable' "
                  "-Remediation 'Create a new search service and re-index.')")
    assert result["calls"] == [], "an immutable property must never be written"
    assert result["applied"] == [] and result["deferred"] == []
    assert "cannot be changed in place" in result["console"]
    assert "Create a new search service and re-index." in result["console"]


@needs_pwsh
def test_a_property_spec_must_be_actionable() -> None:
    """A spec that cannot be applied and does not say how to fix it by hand is worse than no check."""
    for bad, why in [
        ("-Path 'x' -Desired 'y' -Class 'immutable'", "immutable without -Remediation"),
        ("-Path 'x' -Desired 'y'", "mutable without -Arg"),
    ]:
        done = subprocess.run(
            [str(PWSH), "-NoProfile", "-NonInteractive", "-Command",
             f". '{COMMON.as_posix()}'; New-DesiredProperty {bad}"],
            capture_output=True, text=True, cwd=REPO, timeout=60)
        assert done.returncode != 0, f"{why} should have been rejected"


@needs_pwsh
def test_tags_reach_resources_that_already_exist(tmp_path: Path) -> None:
    """Editing Tags in the psd1 was a complete no-op against an existing environment, for every resource.

    Tags were only ever passed to `create`, and Ensure-AzResource discards `-Create` when the resource is there.
    """
    out = tmp_path / "calls.json"
    run_pwsh(f"""
$script:calls = [System.Collections.Generic.List[object]]::new()
function Invoke-Az {{ param([string[]]$Arguments, [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    $script:calls.Add(($Arguments -join ' ')); return $null
}}
$cfg = @{{ AllTags = [ordered]@{{ app = 'rag-os'; env = 'dev'; owner = 'platform' }} }}
$matching = @{{ id = '/subscriptions/s/rg/r'; tags = @{{ app = 'rag-os'; env = 'dev'; owner = 'platform' }} }}
$stale    = @{{ id = '/subscriptions/s/rg/r'; tags = @{{ app = 'rag-os'; env = 'dev'; extra = 'keep-me' }} }}
$a = Sync-AzTags -Description 'thing' -Config $cfg -Resource $matching
$b = Sync-AzTags -Description 'thing' -Config $cfg -Resource $stale
ConvertTo-Json -Depth 5 -InputObject @{{ matched = $a; drifted = $b; calls = @($script:calls) }} |
    Set-Content -LiteralPath '{out.as_posix()}' -Encoding utf8NoBOM
""")
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["matched"] is False and result["drifted"] is True
    assert len(result["calls"]) == 1, "only the resource whose tags differed should have been written"
    call = result["calls"][0]
    assert "tag update --resource-id /subscriptions/s/rg/r --operation merge --tags owner=platform" in call
    assert "extra" not in call, "merge must not try to remove tags we do not own"


@needs_pwsh
def test_an_output_can_be_cleared_but_not_accidentally_erased(tmp_path: Path) -> None:
    """Turning DeployAoaiEmbedding off left the stale deployment name in outputs, so 07 kept injecting it.

    The empty-value guard is right - a probe that came back empty must not erase a good value - so removal has
    to be something a step asks for by name.
    """
    outputs = tmp_path / "dev.outputs.json"
    run_pwsh(f"""
$cfg = @{{ OutputsPath = '{outputs.as_posix()}' }}
Save-Outputs -Config $cfg -Values @{{ embeddingDeployment = 'text-embedding-3-small'; searchName = 'srch-dev' }}
Save-Outputs -Config $cfg -Values @{{ searchName = '' }} -Clear @('embeddingDeployment')
""")
    saved = json.loads(outputs.read_text(encoding="utf-8"))
    assert "embeddingDeployment" not in saved, "an explicitly cleared key must be gone"
    assert saved["searchName"] == "srch-dev", "an empty value must still not erase a stored one"


# --------------------------------------------------------------------------- structural (no pwsh needed)
# Flags that express *configuration* rather than identity. Passing one to `create` and nowhere else makes it a
# create-time-only setting: editing it in the psd1 afterwards does nothing and the run still reports "(exists)".
CONFIG_FLAGS = ("--sku", "--tags", "--retention-time", "--retention-days", "--storage-size", "--tier",
                "--min-tls", "--min-tls-version", "--semantic-search")


def test_creating_a_resource_with_configuration_also_reconciles_it() -> None:
    """Every Ensure-AzResource that sets configuration must be able to correct it later.

    This is the guard against the whole class coming back. Ensure-AzResource discards -Create entirely when the
    resource already exists, so a resource added with a --sku and no -Desired/-SyncTags silently ignores that
    setting from the second run onwards - while still printing a green (exists).
    """
    import re
    offenders = []
    for script in sorted(SCRIPTS.glob("*.ps1")):
        text = script.read_text(encoding="utf-8")
        for call in re.finditer(r"Ensure-AzResource\b", text):
            # A call runs to the first blank line; these statements are multi-line, joined with backticks.
            chunk = text[call.start():]
            end = chunk.find("\n\n")
            chunk = chunk[:end] if end != -1 else chunk
            flags = sorted({f for f in CONFIG_FLAGS if f"'{f}'" in chunk})
            # Tags arrive as `+ $tags` from Get-TagArgs rather than a literal flag, and they were the most
            # widespread case of all: every resource in the repo took tags at create time and none could ever
            # be updated, so editing Tags in the psd1 did nothing anywhere.
            if re.search(r"\+\s*\$tags\b", chunk):
                flags.append("--tags (via $tags)")
            if not flags or "-Desired" in chunk or "-SyncTags" in chunk:
                continue
            line = text[:call.start()].count("\n") + 1
            offenders.append(f"{script.name}:{line} sets {', '.join(flags)} at create time only")
    assert not offenders, ("these settings could never be corrected after creation - add -Desired and/or "
                          "-SyncTags:\n  " + "\n  ".join(offenders))


@needs_pwsh
def test_a_property_the_cli_has_no_flag_for_goes_through_generic_set() -> None:
    """A Key Vault SKU is changeable in Azure, but `az keyvault update` has no --sku.

    Passing one anyway is not a no-op - az rejects the whole command, so the gated change would have failed the
    moment anyone asked for it. The generic --set reaches the property instead.
    """
    result = sync({"properties": {"sku": {"name": "standard"}}},
                  "(New-DesiredProperty -Path 'properties.sku.name' -Desired 'premium' -Label 'sku' "
                  "-Arg '--set' -ArgTemplate 'properties.sku.name={0}')")
    assert result["calls"] == ["svc update --set properties.sku.name=premium -o none"]
    assert result["applied"] == ["sku standard->premium"], "the transcript still names the property, not the flag"


@needs_pwsh
def test_an_arg_template_must_say_where_the_value_goes() -> None:
    """`--set properties.sku.name` without the value silently sends a malformed argument."""
    done = subprocess.run(
        [str(PWSH), "-NoProfile", "-NonInteractive", "-Command",
         f". '{COMMON.as_posix()}'; New-DesiredProperty -Path 'x' -Desired 'y' -Arg '--set' "
         f"-ArgTemplate 'properties.sku.name'"],
        capture_output=True, text=True, cwd=REPO, timeout=60)
    assert done.returncode != 0, "a template with no {0} placeholder should be rejected"



@needs_pwsh
def test_a_disabled_postgres_endpoint_is_re_enabled_on_a_re_run() -> None:
    """The server an earlier run created with --public-access None has to heal, not be rebuilt.

    Six minutes of provisioning is already spent on it, and while its endpoint is off nothing can reach it -
    not the API, not the workers, not the bootstrap job, none of which are VNet-integrated.
    """
    result = sync({"network": {"publicNetworkAccess": "Disabled"}, "state": "Ready"},
                  "(New-DesiredProperty -Path 'network.publicNetworkAccess' -Desired 'Enabled' "
                  "-Arg '--public-access' -Label 'public network access')")
    assert result["calls"] == ["svc update --public-access Enabled -o none"]
    assert result["applied"] == ["public network access Disabled->Enabled"]


@needs_pwsh
def test_an_already_public_endpoint_is_left_alone() -> None:
    """Re-enabling something already enabled would rewrite the server's network config on every single run."""
    result = sync({"network": {"publicNetworkAccess": "Enabled"}},
                  "(New-DesiredProperty -Path 'network.publicNetworkAccess' -Desired 'Enabled' "
                  "-Arg '--public-access' -Label 'public network access')")
    assert result["calls"] == []

def test_a_server_whose_firewall_we_manage_keeps_its_public_endpoint() -> None:
    """Creating PostgreSQL with --public-access None disabled the endpoint, so no rule could exist on it.

    The CLI help actively misleads here - it says None "sets the server in public access mode but does not
    create a firewall rule" - while Azure answers firewall calls with "not supported for a server without public
    access enabled". Worse than the blocked step: nothing in this deployment is VNet-integrated, so the API, the
    workers and the bootstrap job all reach the database over that same public endpoint.
    """
    source = (SCRIPTS / "03-data.ps1").read_text(encoding="utf-8")
    assert "firewall-rule" in source, "this guard only matters while 03 manages firewall rules"
    disabling = [v for v in ("'None'", "'Disabled'")
                 if f"'--public-access', {v}" in source.replace("\n", " ")]
    assert not disabling, (f"03-data.ps1 creates the server with --public-access {disabling} and then manages "
                           "firewall rules on it, which Azure refuses. Use 'Enabled' and let Set-FirewallRule "
                           "add the rules.")


def test_a_still_disabled_endpoint_stops_the_step() -> None:
    """Reaching the firewall section without a public endpoint must fail, not warn and carry on.

    Nothing here is VNet-integrated, so without the AllowAzureServices rule the API, the workers and the
    bootstrap job cannot reach the database. Skipping the rules would let step 03 report success and surface the
    real failure in step 08, a long way from its cause - the exact shape of bug this area keeps producing.
    """
    source = (SCRIPTS / "03-data.ps1").read_text(encoding="utf-8")
    guard = source.split("Write-Step 'PostgreSQL firewall + database'")[1].split("function Set-FirewallRule")[0]
    assert "publicNetworkAccess" in guard, "the firewall section must check the endpoint before using it"
    assert "throw" in guard, "a disabled endpoint must stop the step, not warn"
    # Being told to delete a database server is alarming unless you are also told it is empty.
    assert "HOLDS NO DATA YET" in guard, "the recreate instruction must say the server has no data yet"
    assert "flexible-server delete" in guard and "--public-access Enabled" in guard, \
        "both recovery routes - retry the update, or recreate - have to be spelled out"
