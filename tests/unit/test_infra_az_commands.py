"""Every az flag the provisioning scripts use must exist on the command they send it to.

A wrong flag is only discovered when the script runs, which for these scripts means several minutes into a
deploy against a real subscription - three separate run-and-fail cycles so far. It did not have to be that way:
`az <command> --help` is served entirely from the installed CLI and never touches the network, so the whole
class is checkable offline, including from a machine that cannot reach Azure at all.

Three things get checked, because the flags arrive by three different routes:

* literal invocations - the argument arrays passed to Invoke-Az and friends;
* runtime drift flags - what Sync-AzResource adds from `New-DesiredProperty -Arg`, which never appear in an
  argument array at all;
* the az commands printed in warning text, which an operator copies and runs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "infra" / "scripts"
PWSH = shutil.which("pwsh")
AZ = shutil.which("az")
needs_tools = pytest.mark.skipif(PWSH is None or AZ is None, reason="needs both pwsh and the az CLI")

# Top-level az groups the scripts use. Used to tell an az argument array apart from any other array of strings.
AZ_GROUPS = ("postgres", "storage", "keyvault", "search", "servicebus", "acr", "containerapp", "cognitiveservices",
             "monitor", "group", "role", "tag", "identity", "provider", "account", "extension", "rest", "ad",
             "consumption")
# Accepted everywhere; az lists them under "Global Arguments" for every command anyway.
GLOBAL_FLAGS = {"--debug", "--help", "-h", "--only-show-errors", "--output", "-o", "--query", "--subscription",
                "--verbose", "--ids"}


@cache
def accepted_flags(command: str) -> frozenset[str]:
    """Every flag and alias `az <command>` accepts, or an empty set when the command does not exist.

    az prints all aliases of one argument on a single line before the colon:

        --name --resource-group -g -n [Required] : Name of resource group.

    so the parse is "everything left of the colon, split on whitespace, keep what starts with a dash". Reading
    only the first token of each line is the obvious mistake and it silently turns this into a test that reports
    dozens of correct flags as wrong - see test_the_help_parser_reads_every_alias.
    """
    done = subprocess.run([str(AZ), *command.split(), "--help"], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        return frozenset()
    flags: set[str] = set()
    for line in re.findall(r"(?m)^\s{2,}(-{1,2}\S[^:]*?)\s+:", done.stdout):
        flags.update(tok for tok in line.split() if tok.startswith("-"))
    return frozenset(flags)


def warm(commands: set[str]) -> None:
    """Populate the help cache concurrently.

    Each `az --help` is about a second and a half of Python interpreter startup, and there are around fifty
    distinct commands, so doing them one at a time made this file several times slower than the rest of the
    suite put together. They are independent read-only subprocesses, so they can all go at once.
    """
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(accepted_flags, sorted(commands)))


def run_pwsh_json(body: str) -> list[dict]:
    """Run an extraction script and read back the JSON it writes."""
    work = Path(tempfile.mkdtemp())
    out, script = work / "out.json", work / "extract.ps1"
    script.write_text(body.replace("__OUT__", out.as_posix()), encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, text=True, cwd=REPO, timeout=180)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n{done.stdout}\n{done.stderr}")
    raw = json.loads(out.read_text(encoding="utf-8") or "null")
    if raw is None:
        return []
    return [raw] if isinstance(raw, dict) else raw


# Each array literal that starts with an az group: its command path and the literal flags in it. Non-constant
# elements become <expr>: they are values, and a value never decides which flag was used.
EXTRACT_LITERAL = """
$groups = @(%s)
$rows = [System.Collections.Generic.List[object]]::new()
Get-ChildItem '%s\\*.ps1' | ForEach-Object {
    $file = $_.Name
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$null)
    foreach ($arr in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.ArrayLiteralAst] }, $true)) {
        $elems = $arr.Elements
        if ($elems.Count -lt 2) { continue }
        $head = $elems[0]
        if ($head -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { continue }
        if ($groups -notcontains $head.Value) { continue }
        $path = @(); $flags = @(); $seen = $false
        foreach ($e in $elems) {
            $isConst = $e -is [System.Management.Automation.Language.StringConstantExpressionAst]
            $v = if ($isConst) { $e.Value } else { '<expr>' }
            if ($v -like '-*') { $seen = $true; $flags += $v }
            elseif (-not $seen -and $v -ne '<expr>') { $path += $v }
        }
        if ($path.Count -eq 0) { continue }
        $rows.Add([pscustomobject]@{
            file = $file; line = $arr.Extent.StartLineNumber
            command = ($path -join ' '); flags = @($flags)
        })
    }
}
ConvertTo-Json -InputObject @($rows) -Depth 5 | Set-Content -LiteralPath '__OUT__' -Encoding utf8NoBOM
"""

# The -Update command paths and the New-DesiredProperty -Arg flags in each file. Paired per file rather than per
# statement because a -Desired list is sometimes assembled into a variable first (02 does exactly that).
EXTRACT_DRIFT = """
$rows = [System.Collections.Generic.List[object]]::new()
Get-ChildItem '%s\\*.ps1' | ForEach-Object {
    $file = $_.Name
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$null)
    $paths = @()
    foreach ($cmd in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $els = $cmd.CommandElements
        for ($i = 0; $i -lt $els.Count - 1; $i++) {
            $el = $els[$i]
            if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $el.ParameterName -eq 'Update') {
                $isArr = { $args[0] -is [System.Management.Automation.Language.ArrayLiteralAst] }
                $inner = $els[$i + 1].FindAll($isArr, $true)
                if ($inner) {
                    $p = @()
                    foreach ($e in $inner[0].Elements) {
                        $isConst = $e -is [System.Management.Automation.Language.StringConstantExpressionAst]
                        if ($isConst -and $e.Value -notlike '-*') { $p += $e.Value } else { break }
                    }
                    if ($p.Count) { $paths += ($p -join ' ') }
                }
            }
        }
    }
    $isNdp = { $args[0] -is [System.Management.Automation.Language.CommandAst] -and
               $args[0].GetCommandName() -eq 'New-DesiredProperty' }
    foreach ($ndp in $ast.FindAll($isNdp, $true)) {
        $ne = $ndp.CommandElements
        for ($j = 0; $j -lt $ne.Count - 1; $j++) {
            $el = $ne[$j]
            if ($el -is [System.Management.Automation.Language.CommandParameterAst] -and $el.ParameterName -eq 'Arg') {
                $rows.Add([pscustomobject]@{
                    file = $file; line = $ndp.Extent.StartLineNumber
                    flag = $ne[$j + 1].Value; commands = @($paths | Sort-Object -Unique)
                })
            }
        }
    }
}
ConvertTo-Json -InputObject @($rows) -Depth 5 | Set-Content -LiteralPath '__OUT__' -Encoding utf8NoBOM
"""


@needs_tools
def test_the_help_parser_reads_every_alias() -> None:
    """Guards the parser itself. A check that reports correct code as broken gets ignored, and then it is worse
    than nothing - the first version of this reported 36 problems of which 33 were false, all because az puts
    every alias of an argument on one line.
    """
    group_show = accepted_flags("group show")
    assert group_show, "az group show should resolve"
    for alias in ("--name", "--resource-group", "-g", "-n"):
        assert alias in group_show, f"{alias} is a real alias of az group show but the parser missed it"
    assert "-r" not in group_show, "the parser must still reject a flag that does not exist"
    assert accepted_flags("group definitely-not-a-command") == frozenset(), "an unknown command resolves to nothing"


@needs_tools
def test_every_literal_az_flag_exists() -> None:
    rows = run_pwsh_json(EXTRACT_LITERAL % (", ".join(f"'{g}'" for g in AZ_GROUPS), SCRIPTS))
    assert rows, "no az invocations were extracted - the extractor is broken, not the scripts"
    warm({row["command"] for row in rows})
    checked, bad = 0, []
    for row in rows:
        allowed = accepted_flags(row["command"])
        if not allowed:
            continue  # an array that is a command fragment, not a whole command (e.g. @('containerapp','job'))
        checked += 1
        for flag in sorted(set(row["flags"])):
            if flag not in allowed and flag not in GLOBAL_FLAGS:
                bad.append(f"{row['file']}:{row['line']} az {row['command']} does not accept {flag}")
    assert checked > 50, f"only {checked} commands resolved; the extractor or az is not working"
    assert not bad, "flags az will reject:\n  " + "\n  ".join(bad)


@needs_tools
def test_every_drift_flag_exists() -> None:
    """The flags Sync-AzResource adds at runtime never appear in an argument array, so nothing else sees them."""
    rows = run_pwsh_json(EXTRACT_DRIFT % SCRIPTS)
    assert rows, "no New-DesiredProperty -Arg flags were extracted"
    warm({c for row in rows for c in (row["commands"] if isinstance(row["commands"], list) else [row["commands"]])})
    bad = []
    for row in rows:
        commands = row["commands"] if isinstance(row["commands"], list) else [row["commands"]]
        if not commands:
            bad.append(f"{row['file']}:{row['line']} {row['flag']} has no -Update command to be sent to")
            continue
        if not any(row["flag"] in accepted_flags(c) for c in commands):
            bad.append(f"{row['file']}:{row['line']} {row['flag']} is accepted by none of: "
                       + ", ".join(f"az {c}" for c in commands))
    assert not bad, "drift flags az will reject:\n  " + "\n  ".join(bad)


# `az foo bar -g ... -n ...` inside a string. Deliberately conservative: a token is only checked when it is a
# literal dash flag, and a command is skipped entirely once interpolation appears in its path.
PRINTED = re.compile(r"\baz ((?:[a-z][a-z0-9-]*\s+){1,4})(-[^\"'`]*)")


@needs_tools
def test_az_commands_printed_for_the_operator_are_real() -> None:
    """These are copied and pasted by whoever hits the warning, so a wrong flag wastes their time instead of ours.

    This is how the `firewall-rule delete -r` in a remediation message was found - it had the same root cause as
    the bug that stopped the deployment, but nothing executed it, so nothing else would ever have caught it.
    """
    printed = []
    for script in sorted(SCRIPTS.glob("*.ps1")):
        for n, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            printed.extend((script.name, n, m) for m in PRINTED.finditer(line))
    warm({m.group(1).strip() for _, _, m in printed})
    bad = []
    for script_name, n, m in printed:
        command = m.group(1).strip()
        allowed = accepted_flags(command)
        if not allowed:
            continue  # not a real command path, or truncated by interpolation - not our business here
        for tok in m.group(2).split():
            if not re.fullmatch(r"-{1,2}[a-z][a-z0-9-]*", tok):
                continue  # a value, or something with a $variable in it
            if tok not in allowed and tok not in GLOBAL_FLAGS:
                bad.append(f"{script_name}:{n} printed command 'az {command}' does not accept {tok}")
    assert not bad, "az commands printed for the operator that will not run:\n  " + "\n  ".join(bad)
