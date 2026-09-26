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


# --------------------------------------------------------- what ACR's dependency scanner can parse
# Before building, ACR Tasks scans the Dockerfile to record its base images. That scanner is a separate, stricter
# parser than Docker's, and it does not join backslash continuations first - so a continuation line starting with
# a Dockerfile keyword is read as an instruction. `from huggingface_hub import ...` inside a RUN python -c block
# was taken for a FROM, and the run died in three seconds with "unable to understand line", before the build
# began. Invisible locally: docker build parses the same file without complaint.
INSTRUCTIONS = ("FROM", "RUN", "COPY", "ADD", "ENV", "ARG", "LABEL", "USER", "WORKDIR", "EXPOSE", "CMD",
                "ENTRYPOINT", "VOLUME", "HEALTHCHECK", "SHELL", "STOPSIGNAL", "ONBUILD", "MAINTAINER")


def continuation_lines(text: str) -> list[tuple[int, str]]:
    """(line number, text) for lines that continue the previous one via a trailing backslash."""
    out, continuing = [], False
    for n, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if continuing and stripped:
            out.append((n, stripped))
        continuing = stripped.endswith("\\") and not stripped.startswith("#")
    return out


def first_word(line: str) -> str:
    return re.split(r"[\s(]", line, maxsplit=1)[0].upper().rstrip(":,;")


def test_no_continuation_line_looks_like_an_instruction() -> None:
    offenders = []
    for path in dockerfiles():
        rel = path.relative_to(REPO).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in continuation_lines(path.read_text(encoding="utf-8")):
            word = first_word(line)
            if word not in INSTRUCTIONS:
                continue
            # `HEALTHCHECK --interval=30s \` / `CMD wget ...` is the documented form of that instruction, not a
            # stray CMD - and chat-ui, which uses it, scans and builds in ACR without complaint.
            if word == "CMD" and any("HEALTHCHECK" in prior for prior in lines[max(0, n - 3):n - 1]):
                continue
            offenders.append(f"{rel}:{n} continuation begins with {word!r}: {line[:64]}")
    assert not offenders, (
        "ACR Tasks' dependency scanner reads these as instructions and the build never starts:\n  "
        + "\n  ".join(offenders)
        + "\n  Move the body into a file and COPY it in, rather than reordering to dodge the keyword.")


def test_the_continuation_scan_finds_the_shape_that_broke_the_build() -> None:
    """The exact block from embedder/Dockerfile, so the guard is pinned to a real failure, not a guess."""
    backslash = "\\"
    broke = "\n".join([
        'RUN python -c "' + backslash,
        "import os, pathlib, re; " + backslash,
        "from huggingface_hub import HfApi, snapshot_download; " + backslash,
        'print(1)"',
    ])
    found = [line for _, line in continuation_lines(broke)]
    assert any(ln.startswith("from huggingface_hub") for ln in found), f"continuations not detected: {found}"
    assert first_word(found[1]) == "FROM", "the scanner reads that line as a FROM instruction; so must the guard"

    safe = "\n".join(["RUN groupadd --gid 10001 app " + backslash, "    && mkdir -p /data/raw"])
    assert not any(first_word(ln) in INSTRUCTIONS for _, ln in continuation_lines(safe)), \
        "an ordinary && continuation must not be reported"


# ------------------------------------------------------------------- container app probes
# A probe is a contract between a YAML timeout and a handler's own budget. When the two disagree the platform
# wins, and the result is a replica that can never become Ready - which is how one missing search index removed
# every rag-api replica from the ingress at once and left the chat UI hanging for two minutes with no diagnosis.
TEMPLATES = REPO / "infra" / "containerapps"


def probes(path: Path) -> list[dict[str, str]]:
    """Each probe as a flat dict of its scalar keys. Enough for the invariants below, without a YAML parser."""
    found: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- type:"):
            found.append({"type": stripped.split(":", 1)[1].strip()})
        elif found and ":" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.lstrip("- ").partition(":")
            if value.strip() and key.strip() in {"path", "port", "periodSeconds", "timeoutSeconds",
                                                 "failureThreshold", "initialDelaySeconds"}:
                found[-1].setdefault(key.strip(), value.strip())
    return found


def test_every_probe_states_its_own_timeout() -> None:
    """An omitted timeoutSeconds inherits the platform default of 1 second, which nobody intends."""
    missing = [f"{p.name}: {pr['type']}" for p in sorted(TEMPLATES.glob("*.yaml.tmpl"))
               for pr in probes(p) if "timeoutSeconds" not in pr]
    assert not missing, "probes with no explicit timeoutSeconds (defaults to 1s): " + ", ".join(missing)


def test_readiness_does_not_depend_on_shared_downstream_state() -> None:
    """Readiness decides whether THIS replica stays in the ingress, so it must not ask about the whole system.

    /api/readyz checks the database, the index and the embedding pools - state shared by every replica. Gating
    readiness on it means one downstream problem takes all replicas out simultaneously and the ingress is left
    with no backend, so nothing can be reached to find out why. It stays available as a diagnostic.
    """
    api = TEMPLATES / "rag-api.yaml.tmpl"
    readiness = [pr for pr in probes(api) if pr["type"] == "Readiness"]
    assert readiness, "rag-api should declare a Readiness probe"
    assert readiness[0]["path"] == "/api/healthz", (
        f"readiness is on {readiness[0]['path']}; /api/readyz reflects shared state, not this replica's health")


# ----------------------------------------------------------------- the embedder pools must survive their own start
# A TEI pool loading a 0.6B model on CPU spends minutes before it answers anything, and two numbers decide whether
# it ever gets there. Both were wrong, and the symptom was identical to a hang: the container was OOM-killed
# (exit 137, which writes nothing) immediately after logging "Warming up model", then restarted, forever.
EMBEDDER_TEMPLATES = ["rag-embed-query.yaml.tmpl", "rag-embed-ingest.yaml.tmpl"]
# Measured: warm-up alone took 161s at 2 vCPU on the shipped settings. Azure's vCPU is not a desktop core, so the
# floor is set well above that rather than at it.
MIN_STARTUP_BUDGET_S = 480


def startup_budget_seconds(probe: dict[str, str]) -> int:
    """What the platform actually allows before it gives up - a number that appears nowhere in the file."""
    return (int(probe.get("initialDelaySeconds", 0))
            + int(probe.get("periodSeconds", 10)) * int(probe.get("failureThreshold", 3)))


def test_the_embedder_startup_budget_allows_for_a_model_load() -> None:
    too_short = []
    for name in EMBEDDER_TEMPLATES:
        startup = [p for p in probes(TEMPLATES / name) if p["type"] == "Startup"]
        assert startup, f"{name} should declare a Startup probe"
        budget = startup_budget_seconds(startup[0])
        if budget < MIN_STARTUP_BUDGET_S:
            too_short.append(f"{name}: {budget}s (needs >= {MIN_STARTUP_BUDGET_S}s)")
    assert not too_short, (
        "these restart the replica before the model has finished loading, which looks exactly like a crash "
        "loop with no error:\n  " + "\n  ".join(too_short))


def test_the_startup_budget_reader_matches_the_probe_semantics() -> None:
    """The formula is the whole check, so pin it to the shape the old, broken probe had."""
    assert startup_budget_seconds(
        {"initialDelaySeconds": "10", "periodSeconds": "10", "failureThreshold": "10"}) == 110, (
        "the old query-pool probe allowed 110s; if this arithmetic is wrong the guard proves nothing")
    assert startup_budget_seconds({"periodSeconds": "15", "failureThreshold": "40"}) == 600


def test_the_embedder_pools_bound_their_input_length() -> None:
    """Unset, TEI warms up for the model's own maximum - 32768 tokens for Qwen3 - and attention memory is
    quadratic in that. It was OOM-killed at 4Gi AND at 8Gi, so no memory limit would have rescued it."""
    missing = [name for name in EMBEDDER_TEMPLATES
               if "MAX_INPUT_LENGTH" not in (TEMPLATES / name).read_text(encoding="utf-8")]
    assert not missing, ("MAX_INPUT_LENGTH is unset, so warm-up is sized from the model maximum: " 
                         + ", ".join(missing))


def test_the_bounded_input_length_still_covers_the_largest_chunk() -> None:
    """A bound below max_chunk_tokens would reject real documents at run time instead of at start-up."""
    import re as _re

    psd1 = (REPO / "infra" / "env" / "dev.psd1").read_text(encoding="utf-8")
    profiles = (REPO / "config" / "embedding" / "profiles.yaml").read_text(encoding="utf-8")
    m = _re.search(r"EmbedderMaxInputTokens\s*=\s*(\d+)", psd1)
    assert m, "EmbedderMaxInputTokens is not set in dev.psd1"
    bound = int(m.group(1))
    largest = max(int(x) for x in _re.findall(r"max_chunk_tokens:\s*(\d+)", profiles))
    assert bound > largest, f"MAX_INPUT_LENGTH {bound} must exceed the largest max_chunk_tokens {largest}"

    batch = _re.search(r"EmbedderMaxBatchTokensCpu\s*=\s*(\d+)", psd1)
    assert batch, "EmbedderMaxBatchTokensCpu is not set"
    assert int(batch.group(1)) >= bound, (
        f"TEI requires MAX_BATCH_TOKENS ({batch.group(1)}) >= MAX_INPUT_LENGTH ({bound})")
