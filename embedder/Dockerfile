# RAG-OS self-hosted embedder: Hugging Face Text Embeddings Inference (TEI) with the model baked into the image.
#
# The model is downloaded at BUILD time into a Hugging Face hub cache (/data) at a pinned commit, so containers never
# download at startup and every replica serves byte-identical weights. TEI is started with the hub model id + revision
# (not a local path), so GET /info reports  model_id = <MODEL_ID>  and  model_sha = <MODEL_REVISION>  - exactly what the
# embedding-profile guard compares.
#
# Build (06-registry-build.ps1 runs these through `az acr build`; context = this folder):
#   CPU (query pool):  --build-arg TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9
#   T4  (ingest pool): --build-arg TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:turing-1.9
#   both:              --build-arg MODEL_ID=Qwen/Qwen3-Embedding-0.6B --build-arg MODEL_REVISION=<40-char commit sha>
#
# Runtime knobs (env vars, set per pool by the Container Apps templates): MAX_BATCH_TOKENS, MAX_CLIENT_BATCH_SIZE,
# MAX_CONCURRENT_REQUESTS, RAYON_NUM_THREADS / TOKENIZATION_WORKERS (CPU), AUTO_TRUNCATE.

ARG TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9
ARG PYTHON_IMAGE=python:3.12-slim

# ---------------------------------------------------------------------------------------------------------------------
# Stage 1: populate the hub cache at the pinned revision.
FROM ${PYTHON_IMAGE} AS model
ARG MODEL_ID=Qwen/Qwen3-Embedding-0.6B
ARG MODEL_REVISION
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1
RUN pip install "huggingface_hub>=0.30,<2"
# - Refuses anything but a full commit SHA (a branch name would make builds non-reproducible).
# - Skips alternative weight formats (ONNX/OpenVINO/GGUF/...) and, when safetensors exist, *.bin duplicates.
# - Writes refs/<sha>: huggingface_hub (Python) only records refs for branch/tag names, while older versions of the Rust
#   hub client bundled in TEI resolve `--revision <x>` by reading refs/<x> before snapshots/<commit>/. Writing it keeps
#   the cache hit on those versions and is harmless on newer ones.
RUN python -c "\
import os, pathlib, re; \
from huggingface_hub import HfApi, snapshot_download; \
repo, rev, cache = os.environ['MODEL_ID'], os.environ.get('MODEL_REVISION', ''), '/data'; \
assert re.fullmatch('[0-9a-f]{40}', rev), 'MODEL_REVISION must be a full 40-character commit SHA, got: ' + repr(rev); \
files = HfApi().list_repo_files(repo, revision=rev); \
has_st = any(f.endswith('.safetensors') for f in files); \
skip = ['*.onnx', 'onnx/*', 'openvino/*', '*.gguf', '*.h5', '*.msgpack', '*.ot', '*.tflite', 'coreml/*']; \
skip += ['*.bin', '*.pt', '*.pth'] if has_st else []; \
snap = pathlib.Path(snapshot_download(repo, revision=rev, cache_dir=cache, ignore_patterns=skip)); \
assert snap.name == rev, 'unexpected snapshot folder ' + snap.name; \
assert (snap / 'config.json').exists() and (snap / 'tokenizer.json').exists(), 'config.json / tokenizer.json missing'; \
ref = pathlib.Path(cache, 'models--' + repo.replace('/', '--'), 'refs', rev); \
ref.parent.mkdir(parents=True, exist_ok=True); \
ref.write_text(rev); \
print('Cached', repo, '@', rev, sorted(str(p.relative_to(snap)) for p in snap.rglob('*') if p.is_file()))"

# ---------------------------------------------------------------------------------------------------------------------
# Stage 2: TEI runtime. Do NOT set WORKDIR or ENTRYPOINT: the turing image starts via a relative ./entrypoint.sh.
FROM ${TEI_IMAGE}
ARG TEI_IMAGE
ARG MODEL_ID=Qwen/Qwen3-Embedding-0.6B
ARG MODEL_REVISION
LABEL org.opencontainers.image.title="rag-os-embedder" \
      org.opencontainers.image.description="Text Embeddings Inference with a pinned, pre-downloaded embedding model" \
      org.opencontainers.image.licenses="Apache-2.0" \
      ai.rag-os.model.id="${MODEL_ID}" \
      ai.rag-os.model.revision="${MODEL_REVISION}" \
      ai.rag-os.tei.base-image="${TEI_IMAGE}"

COPY --from=model /data /data

# TEI reads every CLI option from the env var of the same name, so these are equivalent to
#   --model-id $MODEL_ID --revision $MODEL_REVISION --huggingface-hub-cache /data --auto-truncate true
# AUTO_TRUNCATE is set as an env var on purpose: some TEI releases take `--auto-truncate` as a bare flag and others as
# `--auto-truncate <bool>`; the env form works for both. Inputs longer than MAX_BATCH_TOKENS are truncated instead of rejected.
# HF_HUB_OFFLINE=1 stops hub lookups; if a TEI version ignores offline mode it still resolves every file from the
# baked cache (refs/<sha> -> snapshots/<sha>/...), so no model download happens either way.
ENV HUGGINGFACE_HUB_CACHE=/data \
    HF_HUB_CACHE=/data \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    MODEL_ID=${MODEL_ID} \
    REVISION=${MODEL_REVISION} \
    AUTO_TRUNCATE=true \
    MAX_BATCH_TOKENS=16384 \
    MAX_CLIENT_BATCH_SIZE=64 \
    PORT=80

EXPOSE 80
CMD ["--json-output", "--port", "80"]
