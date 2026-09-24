"""The images must build on the classic Docker builder, because that is what builds them.

`06-registry-build.ps1` builds every image with `az acr build`, which runs on ACR Tasks. ACR Tasks uses the
**classic** Docker builder, not BuildKit. So BuildKit-only syntax does not merely miss an optimisation there -
the build fails outright:

    Step 6/19 : RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen ...
    the --mount option requires BuildKit.

That is easy to miss locally, because every modern Docker defaults to BuildKit: the Dockerfile builds perfectly
on a laptop and then fails in Azure, several minutes into a deploy. This is a plain text check so it runs
anywhere, with no Docker and no network.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Constructs the classic builder cannot parse or honour. The value is what to do instead, because being told
# "you cannot use this" without an alternative just invites someone to try a different spelling of it.
BUILDKIT_ONLY = {
    r"RUN\s+--mount": "drop the mount - ACR builds in a fresh container, so a cache mount buys nothing there",
    r"--mount=type=": "drop the mount, or bake the data into a build stage and COPY --from it",
    r"COPY\s+--link": "plain COPY; --link is a BuildKit optimisation",
    r"^#\s*syntax\s*=": "remove the frontend directive - it announces BuildKit features this file must not use",
    r"<<-?EOF": "heredocs are BuildKit-only; use a single RUN with && or COPY a script in",
}


def dockerfiles() -> list[Path]:
    found = [p for p in REPO.rglob("Dockerfile*")
             if not any(part in {"node_modules", ".venv", ".git"} for part in p.parts)]
    assert found, "no Dockerfiles found - this test would pass vacuously"
    return sorted(found)


# `# syntax=` is the one comment the builder reads, so it is checked on comment lines too. Every other pattern
# is checked only on instructions - the Dockerfile header names these constructs in order to warn people off
# them, and a check that fired on its own warning would have to be written around rather than read.
SYNTAX_DIRECTIVE = r"^#\s*syntax\s*="


def test_no_dockerfile_uses_buildkit_only_syntax() -> None:
    offenders = []
    for path in dockerfiles():
        rel = path.relative_to(REPO).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            is_comment = line.lstrip().startswith("#")
            for pattern, remedy in BUILDKIT_ONLY.items():
                if is_comment and pattern != SYNTAX_DIRECTIVE:
                    continue
                if re.search(pattern, line, re.MULTILINE):
                    offenders.append(f"{rel}:{n} {line.strip()[:70]}\n      -> {remedy}")
    assert not offenders, ("az acr build runs the classic builder and will reject these:\n  "
                           + "\n  ".join(offenders))


def test_the_guard_still_catches_what_broke_the_build() -> None:
    """A check narrowed to stop it firing on itself is worth re-proving against the real thing."""
    broken = ["# syntax=docker/dockerfile:1.7",
              "RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen",
              "COPY --link src ./src"]
    for line in broken:
        is_comment = line.lstrip().startswith("#")
        hit = any(re.search(p, line, re.MULTILINE) for p in BUILDKIT_ONLY
                  if not (is_comment and p != SYNTAX_DIRECTIVE))
        assert hit, f"the guard no longer catches: {line}"
    assert not any(re.search(p, "RUN uv sync --frozen --no-editable", re.MULTILINE)
                   for p in BUILDKIT_ONLY), "the fixed form must not be reported"


# ----------------------------------------------------------------- which Dockerfile gets built with which context
# 06 builds four images from three contexts. Pairing the wrong Dockerfile with a context is not a build error you
# find quickly: it fails partway through someone else's build, with a message about a file that was never
# supposed to be there.
BUILD_CONTEXTS = {"": "rag-api", "chat-ui": "rag-chat-ui", "embedder": "rag-embedder-*"}


def test_every_build_context_contains_its_own_dockerfile() -> None:
    missing = [f"{ctx or '<repo root>'} (builds {image})"
               for ctx, image in BUILD_CONTEXTS.items() if not (REPO / ctx / "Dockerfile").is_file()]
    assert not missing, "build contexts with no Dockerfile: " + ", ".join(missing)


def test_the_acr_build_dockerfile_path_carries_its_context() -> None:
    """`-f Dockerfile` does not mean "the Dockerfile in the context" to az acr build.

    az documents --file as relative to the source root, but acr/build.py uses it verbatim when supplied, checks
    it against the *current working directory*, and then force-adds that file into the uploaded tar as the
    Dockerfile to build with. Run from the repo root, a bare name therefore built every image from the API's
    Dockerfile: chat-ui died on `stat pyproject.toml: file does not exist` while running 19 API steps against
    the chat-ui context.
    """
    source = (REPO / "infra" / "scripts" / "06-registry-build.ps1").read_text(encoding="utf-8")
    build_line = next((ln for ln in source.splitlines() if "'acr', 'build'" in ln), None)
    assert build_line, "could not find the az acr build invocation in 06-registry-build.ps1"
    assert "'-f', $b.File" not in build_line, (
        "-f is a bare file name, which az resolves against the working directory, not the build context")
    assert "'-f', $dockerfile" in build_line, (
        "-f should be the context-qualified path ($dockerfile = Join-Path $b.Context $b.File)")
