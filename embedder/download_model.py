"""Populate a Hugging Face hub cache at a pinned commit, at image build time.

Run by embedder/Dockerfile's first stage. Baking the weights in means containers never download at startup and
every replica serves byte-identical vectors - which is the whole point, because two replicas on different
weights would return subtly different embeddings for the same text and nothing downstream would notice.

MODEL_ID and MODEL_REVISION arrive as build args, which Docker exposes to RUN as environment variables.

This lives in a file rather than inside `RUN python -c "..."` because ACR Tasks scans the Dockerfile before
building it, with a stricter parser than Docker's that does not join backslash continuations - so a
continuation line beginning with `from` was read as a FROM instruction and the build failed before it started.
"""

from __future__ import annotations

import os
import pathlib
import re

from huggingface_hub import HfApi, snapshot_download

CACHE = "/data"
# Weight formats TEI does not read. Downloading them would add gigabytes to every image for nothing.
SKIP_ALWAYS = ["*.onnx", "onnx/*", "openvino/*", "*.gguf", "*.h5", "*.msgpack", "*.ot", "*.tflite", "coreml/*"]
# When safetensors are published, the .bin/.pt copies are duplicates of the same weights.
SKIP_IF_SAFETENSORS = ["*.bin", "*.pt", "*.pth"]


def main() -> None:
    repo = os.environ["MODEL_ID"]
    revision = os.environ.get("MODEL_REVISION", "")
    # A branch or tag name would make the build non-reproducible: the same Dockerfile would bake different
    # weights on different days, and the embedding-profile fingerprint would stop meaning anything.
    if not re.fullmatch("[0-9a-f]{40}", revision):
        raise SystemExit(f"MODEL_REVISION must be a full 40-character commit SHA, got: {revision!r}")

    files = HfApi().list_repo_files(repo, revision=revision)
    skip = list(SKIP_ALWAYS)
    if any(f.endswith(".safetensors") for f in files):
        skip += SKIP_IF_SAFETENSORS

    snapshot = pathlib.Path(snapshot_download(repo, revision=revision, cache_dir=CACHE, ignore_patterns=skip))
    if snapshot.name != revision:
        raise SystemExit(f"unexpected snapshot folder {snapshot.name}, expected {revision}")
    for required in ("config.json", "tokenizer.json"):
        if not (snapshot / required).exists():
            raise SystemExit(f"{required} missing from the downloaded snapshot - TEI cannot start without it")

    # huggingface_hub only records refs/ for branch and tag names, but the older Rust hub client bundled in some
    # TEI releases resolves `--revision <x>` by reading refs/<x> before looking at snapshots/<commit>/. Writing
    # it keeps the cache hit on those versions and is harmless on the others.
    ref = pathlib.Path(CACHE, "models--" + repo.replace("/", "--"), "refs", revision)
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(revision)

    cached = sorted(str(p.relative_to(snapshot)) for p in snapshot.rglob("*") if p.is_file())
    print(f"Cached {repo} @ {revision}: {cached}")


if __name__ == "__main__":
    main()
