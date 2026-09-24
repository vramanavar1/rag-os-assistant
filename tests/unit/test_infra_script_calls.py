"""Every parameter passed to a repo-defined PowerShell function must exist on it.

PowerShell parses in command-argument mode after a command name, so an operator written directly on a call binds
as a *parameter name* instead:

    Invoke-Az @('keyvault','secret','list','-o','tsv') -split '[\\t\\r\\n]+'

reads `-split` as a parameter of `Invoke-Az`. Because `Invoke-Az` is an advanced function it rejects the unknown
parameter and throws; a *simple* function would instead swallow it as a surplus argument and hand back the
unsplit string. That idiom was written eight times across the provisioning scripts and was wrong in all eight,
which is the whole reason this file exists. Parentheses fix it - `(Invoke-Az @(...)) -split '...'` - and
Get-AzTsvValues means it no longer has to be written by hand.

PSScriptAnalyzer does not catch this, so the check is built on the PowerShell parser directly and validated
against the real function metadata rather than a reimplementation of the binding rules.
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

# Parameters that PowerShell supplies to every [CmdletBinding()] function. Get-Command reports these for
# common.ps1's functions, but a script-local FunctionDefinitionAst has to have them added back by hand.
COMMON_PARAMETERS = [
    "Verbose", "Debug", "ErrorAction", "WarningAction", "InformationAction", "ProgressAction",
    "ErrorVariable", "WarningVariable", "InformationVariable", "OutVariable", "OutBuffer",
    "PipelineVariable", "WhatIf", "Confirm",
]

# Walks every repo .ps1 and reports each -Parameter that no declared parameter can satisfy. Two subtleties it
# has to respect, both of which produced wrong answers while this was being written:
#   * PowerShell accepts unambiguous prefixes, so -Arg satisfies -Arguments; matching must be by prefix.
#   * `function F([int]$x)` keeps its parameters on the AST node, while `function F { param(...) }` keeps them
#     in a ParamBlock. Reading only the second form reports every inline-declared parameter as unknown.
SCAN = r"""
$ErrorActionPreference = 'Stop'
. '{common}'
$commonFns = @{{}}
Get-Command -CommandType Function |
    Where-Object {{ $_.ScriptBlock.File -eq '{common_native}' }} |
    ForEach-Object {{ $commonFns[$_.Name] = @($_.Parameters.Keys) }}
$commonParams = @({common_params})

$files = Get-ChildItem -Recurse -Filter *.ps1 '{repo}' |
    Where-Object {{ $_.FullName -notmatch 'node_modules|\.venv' }}
$findings = [System.Collections.Generic.List[object]]::new()

foreach ($file in $files) {{
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$null, [ref]$null)
    $known = @{{}}
    foreach ($k in $commonFns.Keys) {{ $known[$k] = $commonFns[$k] }}

    foreach ($fn in $ast.FindAll({{ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }}, $true)) {{
        $declared = if ($fn.Parameters) {{ $fn.Parameters }}
                    elseif ($fn.Body.ParamBlock) {{ $fn.Body.ParamBlock.Parameters }}
                    else {{ @() }}
        $names = @($declared | ForEach-Object {{ $_.Name.VariablePath.UserPath }})
        $attrs = if ($fn.Body.ParamBlock) {{ $fn.Body.ParamBlock.Attributes }} else {{ @() }}
        if ($attrs | Where-Object {{ $_.TypeName.Name -match 'CmdletBinding' }}) {{ $names += $commonParams }}
        $known[$fn.Name] = $names
    }}

    foreach ($call in $ast.FindAll({{ $args[0] -is [System.Management.Automation.Language.CommandAst] }}, $true)) {{
        $name = $call.GetCommandName()
        if (-not $name -or -not $known.ContainsKey($name)) {{ continue }}
        $valid = @($known[$name])
        foreach ($el in $call.CommandElements) {{
            if ($el -isnot [System.Management.Automation.Language.CommandParameterAst]) {{ continue }}
            $p = $el.ParameterName
            if (-not ($valid | Where-Object {{ $_ -like "$p*" }})) {{
                $findings.Add([pscustomobject]@{{
                    file = $file.Name; line = $el.Extent.StartLineNumber
                    called = $name; parameter = "-$p"
                }})
            }}
        }}
    }}
}}
ConvertTo-Json -InputObject @($findings) -Depth 4 |
    Set-Content -LiteralPath '{out}' -Encoding utf8NoBOM
"""


def run_pwsh(body: str) -> None:
    """Run `body` as a script file. Raises with both streams when it fails."""
    path = Path(tempfile.mkdtemp()) / "scan.ps1"
    path.write_text(body, encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(path)],
                          capture_output=True, text=True, cwd=REPO, timeout=180)
    if done.returncode != 0:
        raise AssertionError(f"pwsh exited {done.returncode}\n--- stdout ---\n{done.stdout}\n"
                             f"--- stderr ---\n{done.stderr}")


def scan_calls() -> list[dict[str, object]]:
    out = Path(tempfile.mkdtemp()) / "findings.json"
    run_pwsh(SCAN.format(
        common=COMMON.as_posix(),
        common_native=str(COMMON).replace("'", "''"),
        common_params=", ".join(f"'{p}'" for p in COMMON_PARAMETERS),
        repo=REPO.as_posix(),
        out=out.as_posix(),
    ))
    # ConvertTo-Json unwraps: no findings serialises as 'null' and exactly one as a bare object, so normalise
    # rather than relying on -AsArray, which double-wraps when combined with -InputObject.
    raw = json.loads(out.read_text(encoding="utf-8") or "null")
    if raw is None:
        return []
    return [raw] if isinstance(raw, dict) else raw


@needs_pwsh
def test_no_call_passes_a_parameter_the_function_does_not_have() -> None:
    """Catches the operator-as-parameter trap, and any mistyped parameter, before it reaches Azure.

    These scripts take tens of minutes to reach their later steps, so a name that only fails at runtime is
    expensive to find the slow way - which is exactly how the eight -split calls were found.
    """
    findings = scan_calls()
    listed = [f"{f['file']}:{f['line']} {f['called']} {f['parameter']}" for f in findings]
    assert not listed, "parameters that will not bind:\n  " + "\n  ".join(listed)


@needs_pwsh
def test_the_scan_understands_inline_parameter_declarations() -> None:
    """Guards the scan itself against the false positive it produced while being written.

    `function Test-Model([string]$Label, ..., [switch]$Optional)` declares its parameters on the AST node rather
    than in a ParamBlock. A scan that reads only ParamBlocks reports every such parameter as unknown, which would
    make the test above fire on correct code and train everyone to ignore it.
    """
    source = (SCRIPTS / "00-prereqs.ps1").read_text(encoding="utf-8")
    assert "function Test-Model([string]$Label" in source, "the inline-declaration example moved; pick another"
    assert "-Optional" in source, "Test-Model is no longer called with -Optional; pick another example"
    reported = [f for f in scan_calls() if f["called"] == "Test-Model"]
    assert not reported, f"inline-declared parameters reported as unknown: {reported}"


@needs_pwsh
def test_get_az_tsv_values_actually_splits(tmp_path: Path) -> None:
    """The behaviour every one of the eight broken sites was supposed to have and did not.

    Invoke-Az is shadowed with a stub, so this exercises the real helper without an Azure call.
    """
    out = tmp_path / "result.json"
    run_pwsh(f"""
$ErrorActionPreference = 'Stop'
. '{COMMON.as_posix()}'
function Invoke-Az {{ param([string[]]$Arguments, [switch]$AllowNotFound, [switch]$Sensitive, [switch]$Stream)
    if ($Arguments -contains 'empty') {{ return '' }}
    return "alpha`tbeta`r`ngamma`n`n"
}}
[pscustomobject]@{{
    values = @(Get-AzTsvValues @('some', 'command'))
    empty  = @(Get-AzTsvValues @('empty'))
}} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath '{out.as_posix()}' -Encoding utf8NoBOM
""")
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["values"] == ["alpha", "beta", "gamma"], "tabs, CRLF and LF must all separate values"
    assert result["empty"] == [], "no output must give an empty array, not a one-element array holding ''"


# --------------------------------------------------------------------------- structural (no pwsh needed)
# Kept as a plain-text check so the exact idiom cannot come back where the AST test is skipped for want of pwsh.
CALL = re.compile(r"\b(Invoke-Az|Get-AzTsvValues)\b")


def bare_split_calls(line: str) -> list[str]:
    """Calls where `-split` still sits in command-argument mode, by counting parentheses.

    Between the command name and the operator, `Invoke-Az @(...) -split` closes exactly what it opened, so the
    call has not been closed and `-split` binds as a parameter. `(Invoke-Az @(...)) -split` closes one more than
    it opens - that surplus `)` is the wrapper ending the call and switching to expression mode.
    """
    found = []
    for m in CALL.finditer(line):
        operator = line.find(" -split ", m.end())
        if operator == -1:
            continue
        between = line[m.end():operator]
        if between.count("(") - between.count(")") == 0:
            found.append(m.group(1))
    return found


def code_lines(text: str) -> list[tuple[int, str]]:
    """(line number, code) with comments removed and continuations folded.

    Comments have to go before anything is matched: Get-AzTsvValues documents the broken spelling in its own
    <# #> block, precisely so the next reader knows why the helper exists, and a scan that reads comments would
    report that explanation as the bug. Block comments are blanked in place so line numbers still line up.
    """
    text = re.sub(r"<#.*?#>", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)
    out: list[tuple[int, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        code = raw if raw.lstrip().startswith("#") is False else ""
        if out and re.search(r"[`,]\s*$", out[-1][1]):
            out[-1] = (out[-1][0], out[-1][1] + " " + code.strip())
        else:
            out.append((n, code))
    return out


def test_no_command_call_applies_split_without_parentheses() -> None:
    offenders = []
    for script in sorted(SCRIPTS.glob("*.ps1")):
        for n, line in code_lines(script.read_text(encoding="utf-8")):
            for name in bare_split_calls(line):
                offenders.append(f"{script.name}:{n} - {name} needs parentheses, or use Get-AzTsvValues")
    assert not offenders, "-split will bind as a parameter name here:\n  " + "\n  ".join(offenders)


def test_the_parenthesis_rule_distinguishes_the_two_forms() -> None:
    """The backstop is only worth having if it separates the broken spelling from the fixed one.

    Also runs both through code_lines, because a comment stripper that swallowed real code would leave the check
    passing on a file that had the bug in it - a silent hole is worse than no check.
    """
    broken = "@(Invoke-Az @('keyvault', 'secret', 'list', '-o', 'tsv') -split '[\\t\\r\\n]+' | Where-Object { $_ })"
    fixed = "@((Invoke-Az @('keyvault', 'secret', 'list', '-o', 'tsv')) -split '[\\t\\r\\n]+' | Where-Object { $_ })"
    assert bare_split_calls(broken) == ["Invoke-Az"], "the broken form must be reported"
    assert bare_split_calls(fixed) == [], "the parenthesised form must not be reported"

    script = f"<#\n    example of the trap:\n    {broken}\n#>\n# {broken}\n{broken}\n"
    surviving = [(n, line) for n, line in code_lines(script) if bare_split_calls(line)]
    assert [n for n, _ in surviving] == [6], f"only the real code line should survive stripping, got {surviving}"


@needs_pwsh
def test_every_script_parses() -> None:
    """A syntax error in a provisioning script must not reach a deployment.

    ruff and mypy do not read PowerShell, so a broken script passed every gate the repo had and only showed up
    when someone ran it - which for these scripts can be twenty minutes into a deploy. The PowerShell parser is
    already installed; this just asks it.
    """
    out = Path(tempfile.mkdtemp()) / "parse.json"
    body = rf"""
$bad = [System.Collections.Generic.List[object]]::new()
Get-ChildItem -Recurse -Filter *.ps1 '{REPO.as_posix()}' |
    Where-Object {{ $_.FullName -notmatch 'node_modules|\.venv' }} | ForEach-Object {{
    $errs = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$errs)
    foreach ($e in @($errs)) {{
        $bad.Add([pscustomobject]@{{ file = $_.Name; line = $e.Extent.StartLineNumber; message = "$($e.Message)" }})
    }}
}}
ConvertTo-Json -Depth 4 -InputObject @($bad) | Set-Content -LiteralPath '{out.as_posix()}' -Encoding utf8NoBOM
"""
    path = Path(tempfile.mkdtemp()) / "parse.ps1"
    path.write_text(body, encoding="utf-8")
    done = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive", "-File", str(path)],
                          capture_output=True, text=True, cwd=REPO, timeout=180)
    assert done.returncode == 0, f"parse sweep failed:\n{done.stdout}\n{done.stderr}"
    raw = json.loads(out.read_text(encoding="utf-8") or "null") or []
    if isinstance(raw, dict):
        raw = [raw]
    listed = [f"{e['file']}:{e['line']} {e['message']}" for e in raw]
    assert not listed, "PowerShell syntax errors:\n  " + "\n  ".join(listed)
