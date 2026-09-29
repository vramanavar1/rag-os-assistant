"""Waiting for a dependency that is still starting, and refusing to spend 30 minutes on a job that cannot win.

Step 08 runs immediately after 07. On a fresh deployment `rag-embed-query` is still pulling a ~3 GB image and
loading a model, so `/api/readyz` - which probes that pool through the embedding-profile guard - legitimately
cannot answer for several minutes. The old code polled it with a 20s client timeout while readyz budgets more
than that (database + guard), so every slow-but-working attempt was abandoned before it could reply. nginx logged
each one as:

    path:/api/readyz  status:499  upstream_status:"-"  upstream_time_s:19.540  user_agent:PowerShell/7.6.6

499 is nginx's code for *the client hung up*, and `upstream_status:"-"` means no response headers ever arrived.
It reads exactly like a broken network, and it was neither a network fault nor a dependency failure - it was us
giving up 5.5 seconds early, on a dependency that had not finished starting.

Three things are tested here, all without Azure:

* `Wait-Until`             - waits for "not ready yet", narrating only when the reason CHANGES
* the timeout relationship - 08's client timeout must exceed readyz's own internal budget (two files, two
                             languages, no way to see the relationship by reading either one)
* the pre-flight verdict   - which unreachable hop stops 08 and which is only a warning
"""

from __future__ import annotations

import ast
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
BOOTSTRAP = SCRIPTS / "08-bootstrap.ps1"
CONNECTIVITY = SCRIPTS / "Test-Connectivity.ps1"
HEALTH = REPO / "src" / "rag_os" / "api" / "routers" / "health.py"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")

# A fake clock that Start-Sleep advances. Stubbing Start-Sleep alone is the trap: Wait-Until's deadline is read
# from Get-Date, so a stubbed sleep with a real clock turns a 15-minute timeout into a 15-minute test run.
CLOCK = """
$script:now = [datetime]'2026-01-01T00:00:00'
$script:slept = 0
function Get-Date { param([datetime]$Date) if ($PSBoundParameters.ContainsKey('Date')) { $Date } else { $script:now } }
function Start-Sleep { param([int]$Seconds) $script:slept++; $script:now = $script:now.AddSeconds($Seconds) }
"""


def run_wait(condition_body: str, args: str = "-TimeoutMinutes 1") -> dict:
    """Run Wait-Until against a stubbed clock and return its result plus the lines it printed.

    Write-Host writes to the information stream, so `6>&1` captures the narration alongside the returned
    hashtable; the two are told apart by type rather than by position.
    """
    work = Path(tempfile.mkdtemp())
    out, script = work / "out.json", work / "probe.ps1"
    script.write_text(f""". '{COMMON.as_posix()}'
{CLOCK}
$script:attempts = 0
$all = @(Wait-Until -Activity 'probe' {args} -Condition {{
    $script:attempts++
{condition_body}
}} 6>&1)
$lines = @($all | Where-Object {{ $_ -is [System.Management.Automation.InformationRecord] }} | ForEach-Object {{ "$_" }})
$state = @($all | Where-Object {{ $_ -is [hashtable] }})[-1]
ConvertTo-Json -Depth 5 -InputObject @{{
    ok = [bool]$state.Ok; detail = "$($state.Detail)"; elapsed = [int]$state.Elapsed.TotalSeconds
    attempts = $script:attempts; slept = $script:slept; lines = $lines
}} | Set-Content -LiteralPath '{out.as_posix()}' -Encoding utf8NoBOM
""", encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n{done.stdout}\n{done.stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


def waits(lines: list[str]) -> list[str]:
    return [ln for ln in lines if "[wait]" in ln]


@needs_pwsh
def test_a_condition_that_is_already_true_does_not_wait() -> None:
    got = run_wait("    @{ Ok = $true; Detail = 'HTTP 200' }")
    assert got["ok"] is True
    assert got["attempts"] == 1, "a ready dependency must be asked once"
    assert got["slept"] == 0, "nothing should sleep when the condition is already satisfied"
    assert not waits(got["lines"]), f"no wait line belongs here: {got['lines']}"
    assert any("ready after 0s" in ln for ln in got["lines"]), got["lines"]


@needs_pwsh
def test_only_a_change_of_reason_is_printed() -> None:
    """The whole point of the helper: a ten-minute wait as a few meaningful lines, not forty identical ones."""
    got = run_wait("""
    $detail = switch ($script:attempts) {
        1 { '0/2 replicas ready' } 2 { '0/2 replicas ready' } 3 { '0/2 replicas ready' }
        4 { 'query: embedder unavailable (ConnectError)' } 5 { 'query: embedder unavailable (ConnectError)' }
        default { 'index has no recorded embedding profile' }
    }
    @{ Ok = ($script:attempts -ge 7); Detail = $detail }
""", args="-TimeoutMinutes 5")
    assert got["ok"] is True, got
    assert got["attempts"] == 7
    printed = waits(got["lines"])
    assert len(printed) == 3, f"one line per CHANGE, not per attempt (7 attempts, 3 reasons): {printed}"
    assert "0/2 replicas ready" in printed[0]
    assert "embedder unavailable" in printed[1]
    assert "no recorded embedding profile" in printed[2]


@needs_pwsh
def test_it_gives_up_at_the_deadline_and_reports_why() -> None:
    got = run_wait("    @{ Ok = $false; Detail = 'HTTP 503' }", args="-TimeoutMinutes 1 -IntervalSeconds 15")
    assert got["ok"] is False, "a condition that is never true must not return success"
    assert got["detail"] == "HTTP 503", "the caller needs the last reason to report or throw with"
    # 1 minute at 15s: polls at 0/15/30/45/60 - the last one is at the deadline, so it stops there.
    assert got["attempts"] == 5, f"expected 5 polls in a 1-minute budget at 15s, got {got['attempts']}"
    assert got["elapsed"] == 60, got["elapsed"]


@needs_pwsh
def test_a_throwing_condition_means_not_yet_not_crashed() -> None:
    """A service that is still starting refuses the connection rather than answering politely."""
    got = run_wait("""
    if ($script:attempts -lt 3) { throw "No such host is known. (rag-embed-query:80)`nat line 1" }
    @{ Ok = $true; Detail = 'HTTP 200' }
""")
    assert got["ok"] is True, "the wait must survive a condition that throws"
    printed = waits(got["lines"])
    assert printed, "the reason it is not ready yet still has to be reported"
    assert "No such host is known" in printed[0]
    assert "at line 1" not in printed[0], "only the first line of the exception belongs in a status line"


@needs_pwsh
def test_ready_label_is_configurable_because_finished_is_not_ready() -> None:
    """A job wait is satisfied by a TERMINAL status, which may be 'Failed'. Calling that 'ready' would mislead."""
    got = run_wait("    @{ Ok = $true; Detail = 'status: Failed' }", args="-TimeoutMinutes 1 -ReadyLabel 'finished'")
    assert any("probe finished after" in ln for ln in got["lines"]), got["lines"]
    assert not any("probe ready after" in ln for ln in got["lines"]), got["lines"]


# --------------------------------------------------------------------------------- ready replica counting
# "0/2 replicas ready" is the one thing an HTTP probe cannot tell you: a replica whose image is still pulling and
# a replica that is running and refusing connections both look like a connection that goes nowhere.
def run_replica_wait(replica_json: str, min_replicas: int = 1) -> dict:
    """Run Wait-ContainerAppReady with az stubbed out, returning the first wait line and the result."""
    work = Path(tempfile.mkdtemp())
    out, script = work / "out.json", work / "probe.ps1"
    script.write_text(f""". '{COMMON.as_posix()}'
{CLOCK}
# Same shape as the real Invoke-Az, so a change to its signature breaks this loudly rather than silently.
function Invoke-Az {{
    [CmdletBinding()]
    param([Parameter(Mandatory, Position = 0)][string[]]$Arguments,
          [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    # -AsHashtable, because that is what the real Invoke-Az returns (common.ps1) - PSCustomObjects would be
    # a different shape to index into, and the test would then prove nothing about production.
    return (ConvertFrom-Json -InputObject '{replica_json}' -AsHashtable -Depth 100)
}}
$all = @(Wait-ContainerAppReady -ResourceGroup 'rg' -Name 'rag-embed-query' `
    -MinReplicas {min_replicas} -TimeoutMinutes 1 6>&1)
$lines = @($all | Where-Object {{ $_ -is [System.Management.Automation.InformationRecord] }} | ForEach-Object {{ "$_" }})
$state = @($all | Where-Object {{ $_ -is [hashtable] }})[-1]
ConvertTo-Json -Depth 5 -InputObject @{{ ok = [bool]$state.Ok; detail = "$($state.Detail)"; lines = $lines }} |
    Set-Content -LiteralPath '{out.as_posix()}' -Encoding utf8NoBOM
""", encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n{done.stdout}\n{done.stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


@needs_pwsh
def test_a_replica_counts_only_when_every_container_in_it_is_ready() -> None:
    both = '[{"name":"r1","containers":[true,true]}]'
    assert run_replica_wait(both)["ok"] is True, "all containers ready means the replica is ready"

    one = '[{"name":"r1","containers":[true,false]}]'
    got = run_replica_wait(one)
    assert got["ok"] is False, "a replica with a container that is not ready is not a ready replica"
    assert got["detail"] == "0/1 replicas ready", got["detail"]


@needs_pwsh
def test_no_replicas_at_all_is_reported_as_such_not_as_an_error() -> None:
    """A scaled-to-zero or still-pulling app returns an empty list; that is a wait, not a failure."""
    got = run_replica_wait("[]")
    assert got["ok"] is False
    assert got["detail"] == "0/0 replicas ready", got["detail"]
    assert any("0/0 replicas ready" in ln for ln in got["lines"]), got["lines"]


@needs_pwsh
def test_a_replica_with_no_container_data_is_not_counted_as_ready() -> None:
    """An empty containers array would make `-notcontains $false` true - ready by absence of evidence."""
    got = run_replica_wait('[{"name":"r1","containers":[]}]')
    assert got["ok"] is False, "no container readiness data is not the same as ready"


@needs_pwsh
def test_min_replicas_is_honoured() -> None:
    one_of_two = '[{"name":"r1","containers":[true]},{"name":"r2","containers":[false]}]'
    assert run_replica_wait(one_of_two, min_replicas=1)["ok"] is True
    got = run_replica_wait(one_of_two, min_replicas=2)
    assert got["ok"] is False, "asking for two ready replicas must not be satisfied by one"
    assert got["detail"] == "1/2 replicas ready", got["detail"]


# ------------------------------------------------------------------ the timeout relationship across two files
# 08 polls /api/readyz with a client timeout. readyz spends its own budget on asyncio.wait_for calls. If the
# client timeout is the smaller number, a slow-but-working answer can never be received - and the failure looks
# like a network fault, not like a number that is too small. Nothing in either file hints at the other.
def readyz_internal_budget_seconds() -> int:
    """Sum of the asyncio.wait_for timeouts inside readyz(). They run one after another, so they add up.

    The timeouts are named module-level constants rather than literals, because each one has to be reasoned
    about against a timeout in another file and a bare number in a call cannot carry that. So resolve names
    too - accepting only a literal here would have made this check fail the moment the code got clearer.
    """
    tree = ast.parse(HEALTH.read_text(encoding="utf-8"))
    constants = {t.id: node.value.value
                 for node in tree.body if isinstance(node, ast.Assign)
                 for t in node.targets
                 if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant)}
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "readyz"), None)
    assert fn, "readyz() not found in health.py"
    total = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "wait_for"):
            continue
        timeout = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
        if isinstance(timeout, ast.Constant):
            total += int(timeout.value)
        elif isinstance(timeout, ast.Name):
            assert timeout.id in constants, (
                f"readyz uses {timeout.id} as a timeout but it is not a module-level constant in health.py")
            total += int(constants[timeout.id])
        else:
            raise AssertionError("every wait_for in readyz must state a literal or a module-level constant")
    assert total > 0, "no asyncio.wait_for timeouts found - this check would pass vacuously"
    return total


def client_timeouts_against_readyz() -> dict[str, int]:
    """Every client-side timeout used to call /api/readyz, by the file it lives in."""
    found: dict[str, int] = {}
    boot = BOOTSTRAP.read_text(encoding="utf-8")
    m = re.search(r"/api/readyz\"\s+-TimeoutSec\s+(\d+)", boot)
    assert m, "could not find the /api/readyz client timeout in 08-bootstrap.ps1"
    found[BOOTSTRAP.name] = int(m.group(1))

    conn = CONNECTIVITY.read_text(encoding="utf-8")
    m = re.search(r"Path\s*=\s*'/api/readyz'.*?Timeout\s*=\s*(\d+)", conn)
    assert m, "could not find the /api/readyz probe timeout in Test-Connectivity.ps1"
    found[CONNECTIVITY.name] = int(m.group(1))
    return found


def test_every_readyz_client_timeout_exceeds_readyz_own_budget() -> None:
    budget = readyz_internal_budget_seconds()
    offenders = [f"{name}: {secs}s client timeout vs readyz's own {budget}s worst case"
                 for name, secs in client_timeouts_against_readyz().items() if secs <= budget]
    assert not offenders, (
        "these cut off a slow-but-working answer, which nginx logs as a 499 with no upstream response - "
        "indistinguishable from a broken hop:\n  " + "\n  ".join(offenders)
        + f"\n  Raise the client timeout above {budget}s, or lower the wait_for budgets in health.py.")


def test_the_guard_would_have_caught_the_timeout_that_produced_the_499s() -> None:
    """08 polled with 20s against a 25s budget. That is the defect this guard exists for, so prove it fires.

    The value in the file is 30 now, so asserting on the file alone would pass for the wrong reason - it would
    pass just as happily if the comparison were inverted.
    """
    budget = readyz_internal_budget_seconds()
    historical = 20
    assert historical <= budget, "20s against readyz's own budget is exactly the case that must be reported"
    current = client_timeouts_against_readyz()
    assert all(secs > budget for secs in current.values()), (
        f"and the current values must sit on the other side of that same comparison: {current}")


def test_the_budget_reader_sees_the_numbers_it_claims_to() -> None:
    """A sum computed from an AST walk is worth checking against the file, or the guard could pass on zero."""
    assert readyz_internal_budget_seconds() == 35, (
        "expected 12s (database) + 3s (schema revision) + 20s (embedding profile guard); "
        "if health.py changed deliberately, "
        "update this and re-check the client timeouts that depend on it")


# ------------------------------------------------------------------------------- pre-flight classification
# The bootstrap job reaches PostgreSQL, Search and Blob from its own container. Those are worth stopping for: a
# 30-minute job that cannot succeed. The chat-ui -> rag-api hop is not - the job never uses it - so treating it
# as fatal would block a deployment over a workload that simply had not finished starting.
def verdict_block() -> str:
    text = CONNECTIVITY.read_text(encoding="utf-8")
    marker = "# ---------------------------------------------------------------------------------- verdict"
    assert marker in text, "the verdict section marker moved; this test reads that section directly"
    return text[text.index(marker):]


def run_verdict(results: list[tuple[str, bool, bool]]) -> tuple[int, str]:
    """Run the real verdict section over synthetic results. Returns (exit code, output)."""
    work = Path(tempfile.mkdtemp())
    script = work / "verdict.ps1"
    rows = ", ".join(f"[pscustomobject]@{{ Hop = '{hop}'; Ok = ${str(ok).lower()}; "
                     f"Blocking = ${str(blocking).lower()} }}"
                     for hop, ok, blocking in results)
    script.write_text(f""". '{COMMON.as_posix()}'
$SnippetsOnly = $false
$results = @({rows})
{verdict_block()}
""", encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=60)
    return done.returncode, done.stdout + done.stderr


@needs_pwsh
def test_an_unreachable_search_service_stops_08() -> None:
    code, out = run_verdict([("AI Search :443", False, True), ("chat UI (nginx, no API involved)", True, False)])
    assert code == 1, f"a hop the job needs must fail the pre-flight:\n{out}"
    assert "AI Search" in out


@needs_pwsh
def test_a_broken_chat_ui_hop_only_warns() -> None:
    code, out = run_verdict([("AI Search :443", True, True), ("chat-ui -> rag-api hop", False, False)])
    assert code == 0, f"the job does not use that hop, so it must not block the deployment:\n{out}"
    assert "chat-ui -> rag-api hop" in out, "it still has to be reported"
    assert "do not block" in out


@needs_pwsh
def test_all_clear_exits_zero_so_the_caller_can_trust_the_code() -> None:
    code, out = run_verdict([("AI Search :443", True, True), ("PostgreSQL :5432", True, True)])
    assert code == 0, out
    assert "Every hop" in out


def test_08_checks_the_preflight_exit_code_rather_than_assuming_it() -> None:
    """`&` does not fail the caller when the child exits non-zero, and a script that falls off its end leaves
    $LASTEXITCODE set by whatever ran last - so the call site has to test it, and the callee has to set it."""
    boot = BOOTSTRAP.read_text(encoding="utf-8")
    assert "Test-Connectivity.ps1') -Env $Env -Preflight" in boot, "08 should run the pre-flight before the job"
    assert "$LASTEXITCODE -ne 0" in boot, "08 must test the pre-flight's exit code"
    assert boot.index("-Preflight") < boot.index("Start-JobAndWait -JobName 'rag-bootstrap'"), \
        "the pre-flight has to run BEFORE the job, or it saves nothing"

    conn = CONNECTIVITY.read_text(encoding="utf-8")
    assert conn.rstrip().endswith("exit 0"), (
        "Test-Connectivity must end with an explicit exit code; falling off the end leaves $LASTEXITCODE from "
        "the last az call, which would make 08's check meaningless")
    assert "if ($SnippetsOnly) { exit 0 }" in conn, "the -SnippetsOnly path needs an explicit code too"


def test_the_job_wait_never_abandons_a_running_job() -> None:
    """Moved onto Wait-Until, the poll must still swallow control-plane errors instead of letting them out."""
    boot = BOOTSTRAP.read_text(encoding="utf-8")
    wait = boot[boot.index("$poll = @{"):boot.index("$status = $poll.Status")]
    assert "catch {" in wait, "a failed poll must be caught inside the condition, not surface as a wait error"
    assert "$poll.Failures++" in wait, "the failure count is what decides not to stop a job of unknown state"
    assert "Ok = $false" in wait, "an unreadable status is 'not yet', never 'finished'"


# ------------------------------------------------------------------------ the Console snippets must be runnable
# These are pasted into a container's Monitoring -> Console, where there is no package manager and no second try.
# The images differ in what they contain, and the difference is not guessable: python:3.13-slim has no curl at
# all, and the TEI image has no python3. Each inventory below was taken by running that image
# (`docker run --rm <image> sh -c 'command -v curl python3 ...'`), not from its documentation.
IMAGE_TOOLS = {
    # nginx-unprivileged:1.27-alpine - busybox, so nslookup and nc come for free
    "rag-chat-ui": {"curl", "wget", "nc", "nslookup", "getent", "ping", "sh", "cat"},
    # python:3.13-slim - Debian slim with no network tooling whatsoever, plus coreutils
    "rag-api": {"python3", "bash", "sh", "getent", "openssl", "tail", "head", "sed", "awk", "cat"},
    # ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 - curl, but no python3
    "rag-embed-query": {"curl", "bash", "sh", "getent", "openssl", "cat"},
}
# What each image does NOT have. Asserted explicitly, because the failure it prevents is a snippet that looks
# perfectly reasonable and dies with "curl: not found" while someone is mid-incident.
IMAGE_MISSING = {
    "rag-chat-ui": {"python3"},
    "rag-api": {"curl", "wget", "nc", "nslookup"},
    "rag-embed-query": {"python3", "wget", "nc", "nslookup"},
}

SNIPPET_PROBE = """
$ast = [System.Management.Automation.Language.Parser]::ParseFile('%(file)s', [ref]$null, [ref]$null)
$assignments = $ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $args[0].Left.Extent.Text -like '$snippets*'
}, $true)
if (-not $assignments) { throw 'no $snippets assignment found' }
$pg = 'pg.example.com'
$search = 'search.example.net'
$useTei = $true
foreach ($a in $assignments) { Invoke-Expression $a.Extent.Text }
$result = @{}
foreach ($k in $snippets.Keys) { $result[$k] = @($snippets[$k]) }
ConvertTo-Json -Depth 5 -InputObject $result | Set-Content -LiteralPath '%(out)s' -Encoding utf8NoBOM
"""


def console_snippets() -> dict[str, list[str]]:
    """The real $snippets literal from Test-Connectivity.ps1, evaluated with its inputs stubbed.

    Located through the PowerShell parser rather than by slicing the text: the assignments are nested inside an
    if/else, so any indentation-based extraction breaks the moment that block is re-shaped.
    """
    work = Path(tempfile.mkdtemp())
    out, script = work / "out.json", work / "probe.ps1"
    script.write_text(SNIPPET_PROBE % {"file": CONNECTIVITY.as_posix(), "out": out.as_posix()}, encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=60)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n{done.stdout}\n{done.stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


def commands_only(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def app_of(key: str) -> str:
    return key.split(" ")[0]


@needs_pwsh
def test_every_console_snippet_uses_only_tools_its_own_image_has() -> None:
    found = console_snippets()
    assert found, "no snippets were extracted - this test would pass vacuously"
    offenders = []
    for key, lines in found.items():
        app = app_of(key)
        allowed = IMAGE_TOOLS.get(app)
        assert allowed, f"no verified tool inventory for {app} - add one rather than skipping the check"
        for line in commands_only(lines):
            tool = line.split()[0]
            if tool not in allowed:
                offenders.append(f"{app}: {tool!r} is not in that image -> {line[:60]}")
    assert not offenders, "these would die with 'not found' inside the container:\n  " + "\n  ".join(offenders)


@needs_pwsh
def test_no_snippet_reaches_for_a_tool_the_image_is_known_to_lack() -> None:
    """Stated separately so the inventory above cannot be widened by accident."""
    found = console_snippets()
    for key, lines in found.items():
        app = app_of(key)
        body = "\n".join(commands_only(lines))
        for absent in IMAGE_MISSING[app]:
            assert not re.search(rf"(?m)^\s*{absent}\b", body), (
                f"{app} has no {absent}; that snippet dies with 'not found' at the worst moment")


@needs_pwsh
def test_the_api_snippets_separate_dns_from_reachability() -> None:
    """connect_ex returns a code for a refused port but RAISES on a name it cannot resolve.

    Verified by running the lines in python:3.13-slim: an unresolvable host produced a `socket.gaierror`
    traceback, while a resolvable-but-closed port printed a clean UNREACHABLE. DNS is one of the things being
    diagnosed here, so it is asked separately with getent, and every python line keeps its output to one line.
    """
    commands = commands_only(console_snippets()["rag-api (container: api)"])
    assert any(ln.startswith("getent hosts") for ln in commands), (
        "DNS has to be asked on its own, before anything that would raise on a name it cannot resolve")
    python_lines = [ln for ln in commands if ln.startswith("python3")]
    assert python_lines, "the reachability checks are the point of these snippets"
    # Two kinds of probe, two opposite rules. A reachability probe answers with a verdict, so a failure should
    # collapse to the single line that names the cause. A body probe answers with a document - /api/readyz's
    # JSON - and collapsing that would throw away the very thing being fetched, which is the whole defect this
    # change exists to fix.
    reachability = [ln for ln in python_lines if "http.client" not in ln]
    body_probes = [ln for ln in python_lines if "http.client" in ln]
    assert reachability and body_probes, f"expected both kinds of probe: {len(reachability)}, {len(body_probes)}"
    for ln in reachability:
        assert "2>&1" in ln, f"a traceback goes to stderr, so it must be redirected to be collapsed: {ln[:70]}"
        assert ln.rstrip().endswith("| tail -n 1"), (
            f"stderr must collapse to the one line that names the cause, not a traceback: {ln[:70]}")
    for ln in body_probes:
        assert "| tail" not in ln, (
            f"the readyz body is the answer; collapsing it to one line discards the diagnosis: {ln[:70]}")
        assert "urlopen" not in ln, (
            "urlopen raises on 503 - use http.client, which returns the response for any status")


@needs_pwsh
def test_the_dns_and_port_checks_cover_the_same_hosts() -> None:
    """A port check on a host whose DNS was never asked leaves the two-step diagnosis with a hole."""
    commands = commands_only(console_snippets()["rag-api (container: api)"])
    resolved = {ln.split()[2] for ln in commands if ln.startswith("getent hosts")}
    probed = set(re.findall(r"connect_ex\(\('([^']+)'", " ".join(commands)))
    assert probed, "no connect_ex targets found - the extraction is broken, not the script"
    missing = probed - resolved - {"localhost"}
    assert not missing, (
        f"port-checked but their DNS is never asked, so a name failure reads as a port failure: {sorted(missing)}")


# ------------------------------------------------------------------ the embedding alignment gate (step 09)
# Documents and queries have to be embedded by the same model. When they are not, nothing errors: the index has
# the right dimensionality, the search returns the right NUMBER of hits, and they are arbitrary passages
# presented with full confidence. It reads as "the answers got worse", which is indistinguishable from a dozen
# other causes and survives every health check in the system.
ALIGNMENT = SCRIPTS / "Test-EmbeddingAlignment.ps1"


def alignment_verdict(results: list[tuple[str, bool, bool]]) -> tuple[int, str]:
    """Run the real verdict section of Test-EmbeddingAlignment over synthetic results."""
    text = ALIGNMENT.read_text(encoding="utf-8")
    marker = "# --------------------------------------------------------------------------------------------- verdict"
    assert marker in text, "the verdict section marker moved; this test reads that section directly"
    work = Path(tempfile.mkdtemp())
    script = work / "verdict.ps1"
    rows = ", ".join(f"[pscustomobject]@{{ Check = '{check}'; Ok = ${str(ok).lower()}; "
                     f"Advisory = ${str(advisory).lower()} }}"
                     for check, ok, advisory in results)
    script.write_text(f""". '{COMMON.as_posix()}'
$Env = 'dev'
$results = @({rows})
{text[text.index(marker):]}
""", encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=60)
    return done.returncode, done.stdout + done.stderr


@needs_pwsh
def test_a_model_mismatch_stops_the_smoke_tests() -> None:
    code, out = alignment_verdict([("rag-embed-ingest model", False, False), ("model", True, False)])
    assert code == 1, f"a mismatch must fail the gate:\n{out}"
    assert "rag-embed-ingest model" in out


@needs_pwsh
def test_the_two_pools_disagreeing_with_each_other_is_fatal() -> None:
    """Both pools can disagree with the psd1 and still be consistent with each other, which is survivable.
    Disagreeing with each other is not: documents and queries then land in different vector spaces."""
    code, out = alignment_verdict([("both pools serve the same model", False, False)])
    assert code == 1, out
    assert "re-ingest" in out, "a wrong index is not fixed by redeploying; the content has to be rebuilt"


@needs_pwsh
def test_an_unreadable_profile_file_does_not_block_a_deployment() -> None:
    """The stated contract of the profile readers: $null means "unknown", never "fail". An unparseable config
    file must not be the thing that stops a release."""
    code, out = alignment_verdict([("model", True, True), ("dimensions", True, True)])
    assert code == 0, f"advisory results must not fail the gate:\n{out}"


@needs_pwsh
def test_everything_lined_up_exits_zero_so_09_can_trust_the_code() -> None:
    code, out = alignment_verdict([("model", True, False), ("both pools serve the same model", True, False)])
    assert code == 0, out
    assert "same embedding model" in out


def test_09_runs_the_gate_before_the_smoke_tests_and_checks_its_code() -> None:
    """Order is the point. Run after, and a mismatch shows up as the ingestion check timing out four minutes
    later - which reads like a broken worker, not like a vector-space problem."""
    smoke = (SCRIPTS / "09-smoke.ps1").read_text(encoding="utf-8")
    assert "Test-EmbeddingAlignment.ps1') -Env $Env" in smoke, "09 should run the alignment gate"
    assert "$LASTEXITCODE -ne 0" in smoke, "`&` does not fail the caller, so the exit code has to be tested"
    # Search the CODE, not the doc comment. 09's header names both the alignment script and the exact
    # `uv run python scripts/smoke.py` command line, so a naive search over the whole file finds those
    # first and the ordering assertion passes for a reason unrelated to what actually runs.
    body = smoke.split("#>", 1)[1]
    gate = body.index("Test-EmbeddingAlignment.ps1")
    run = body.index("uv run python scripts/smoke.py")
    assert gate < run, "the gate has to run BEFORE the functional tests, or it saves nothing"
    assert "-SkipAlignmentCheck" in smoke, "there has to be a deliberate override"


def test_the_alignment_script_sets_an_exit_code_on_every_path() -> None:
    text = ALIGNMENT.read_text(encoding="utf-8")
    assert text.rstrip().endswith("exit 0"), (
        "falling off the end leaves $LASTEXITCODE from the last az call, which would make 09's check meaningless")
    assert "exit 1" in text, "a failing check has to be reportable to the caller"


def test_08_records_the_index_it_confirmed() -> None:
    """08 has the live index name in hand and used to print it and throw it away. 09 needs it to tell 'the
    profile changed and the new index is empty' apart from 'the content is missing'."""
    boot = (SCRIPTS / "08-bootstrap.ps1").read_text(encoding="utf-8")
    assert "embeddingIndex" in boot and "Save-Outputs" in boot, "the confirmed index should be persisted"


# ------------------------------------------------------- the alignment gate under each embedding provider
# The self-hosted keys (EmbedderModelId, EmbedderModelRevision) describe the TEI image. For a remote profile no
# such image is built or deployed, so comparing the profile against them reports a mismatch that means nothing -
# and because 09 hard-throws on this gate, it failed every correct Azure OpenAI configuration.
ENV_DIR = REPO / "infra" / "env"


def write_env(name: str, settings: dict[str, str], outputs: dict | None = None) -> list[Path]:
    """Derive infra/env/<name>.psd1 from dev.psd1, overriding `settings` by KEY. Returns files to delete.

    The fixture has to live in infra/env because the script resolves its own env directory when it dot-sources
    common.ps1 - overriding that variable from outside does not survive the re-sourcing.

    Keys are matched by name, not by their current value. dev.psd1 is a user-editable environment file, so an
    earlier version of this helper - which replaced exact `Key = 'value'` strings - broke every test here the
    first time somebody changed the embedding profile, for reasons unrelated to what the tests assert.
    """
    src = (ENV_DIR / "dev.psd1").read_text(encoding="utf-8")
    for key, value in settings.items():
        pattern = rf"(?m)^(\s*){re.escape(key)}(\s*)=\s*[^\r\n#]*"
        src, count = re.subn(pattern, lambda m: f"{m.group(1)}{key}{m.group(2)}= {value} ", src, count=1)
        assert count == 1, f"dev.psd1 has no setting named {key!r} - the fixture needs updating"
    written = [ENV_DIR / f"{name}.psd1"]
    written[0].write_text(src, encoding="utf-8")
    if outputs is not None:
        written.append(ENV_DIR / f"{name}.outputs.json")
        written[-1].write_text(json.dumps(outputs), encoding="utf-8")
    return written


def run_alignment(name: str) -> tuple[int, str]:
    done = subprocess.run(
        [str(PWSH), "-NoProfile", "-NonInteractive", "-File",
         str(SCRIPTS / "Test-EmbeddingAlignment.ps1"), "-Env", name, "-SkipLive"],
        capture_output=True, text=True, cwd=REPO, timeout=180)
    return done.returncode, done.stdout + done.stderr


@needs_pwsh
def test_a_remote_profile_is_not_judged_against_the_self_hosted_keys() -> None:
    files = write_env("zzaoai", {
        "Env": "'zzaoai'",
        "EmbeddingProfile": "'aoai-3-small-1536'",
        "DeployAoaiEmbedding": "$true",
        # Set explicitly so the assertion below has a known value to prove is NOT compared, rather than
        # relying on whatever dev.psd1 happens to hold.
        "EmbedderModelId": "'Qwen/Qwen3-Embedding-0.6B'",
    }, outputs={"embeddingDeployment": "text-embedding-3-small", "aoaiEndpoint": "https://x.openai.azure.com"})
    try:
        code, out = run_alignment("zzaoai")
        assert "provider azure_openai" in out, out
        assert code == 0, f"a correct remote configuration must pass the gate:\n{out}"
        # The profile says text-embedding-3-small while the psd1's TEI keys still say Qwen. That is not a fault.
        assert "Qwen" not in out, f"the TEI model id must not be compared for a remote profile:\n{out}"
        assert "model_revision" not in out, "a remote profile pins no revision, so there is nothing to compare"
        assert "do not apply" in out, "say why those keys are being skipped, rather than silently skipping them"
    finally:
        for f in files:
            f.unlink(missing_ok=True)


@needs_pwsh
def test_a_remote_profile_without_a_deployment_fails_the_gate() -> None:
    """Selecting the model and deploying it are two settings. Only one of them is in the profile."""
    files = write_env("zzaoai2", {
        "Env": "'zzaoai2'",
        "EmbeddingProfile": "'aoai-3-small-1536'",
        "DeployAoaiEmbedding": "$false",
    }, outputs={"aoaiEndpoint": "https://x.openai.azure.com"})
    try:
        code, out = run_alignment("zzaoai2")
        assert code == 1, f"DeployAoaiEmbedding is false and no deployment was recorded:\n{out}"
        assert "DeployAoaiEmbedding" in out
        assert "05-foundry" in out or "re-run 05" in out, "name the step that fixes it"
    finally:
        for f in files:
            f.unlink(missing_ok=True)


@needs_pwsh
def test_a_self_hosted_profile_is_still_judged_against_the_self_hosted_keys() -> None:
    """The branch must not become a way to skip the check that catches a half-rebuilt embedder image."""
    files = write_env("zztei", {
        "Env": "'zztei'",
        "EmbeddingProfile": "'qwen3-0.6b-1024'",
        "EmbedderModelId": "'BAAI/bge-small-en-v1.5'",
    }, outputs={})
    try:
        code, out = run_alignment("zztei")
        assert code == 1, f"a psd1 that disagrees with the profile must still fail:\n{out}"
        assert "bge-small" in out and "profiles.yaml" in out, out
    finally:
        for f in files:
            f.unlink(missing_ok=True)


@needs_pwsh
def test_a_remote_profile_may_leave_the_model_revision_blank() -> None:
    """Azure OpenAI has no commit to pin, so Import-RagOsConfig must not demand a 40-character SHA."""
    files = write_env("zzblank", {
        "Env": "'zzblank'",
        "EmbeddingProfile": "'aoai-3-small-1536'",
        "DeployAoaiEmbedding": "$true",
        "EmbedderModelRevision": "''",
    }, outputs={"embeddingDeployment": "text-embedding-3-small"})
    try:
        code, out = run_alignment("zzblank")
        assert "40-character commit SHA" not in out, f"the pin is a self-hosted concern only:\n{out}"
        assert code == 0, out
    finally:
        for f in files:
            f.unlink(missing_ok=True)


@needs_pwsh
def test_a_self_hosted_profile_still_requires_a_pinned_revision() -> None:
    files = write_env("zzunpin", {
        "Env": "'zzunpin'",
        "EmbeddingProfile": "'qwen3-0.6b-1024'",
        "EmbedderModelRevision": "'main'",
    }, outputs={})
    try:
        code, out = run_alignment("zzunpin")
        assert code != 0 and "40-character commit SHA" in out, (
            f"an unpinned self-hosted model must still be refused:\n{out}")
    finally:
        for f in files:
            f.unlink(missing_ok=True)
