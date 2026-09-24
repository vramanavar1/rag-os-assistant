# RAG-OS embedder image

[Text Embeddings Inference (TEI)](https://github.com/huggingface/text-embeddings-inference) with the embedding model
**baked into the image** at a pinned commit. Containers never download a model at startup, and every replica of both
pools serves identical weights.

| Image | Base | Runs as | Used by |
|---|---|---|---|
| `rag-embedder-cpu:<tag>` | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9` | `rag-embed-query` (query path, `query` profile); `rag-embed-ingest` when no GPU is available | `TEI_QUERY_URL` |
| `rag-embedder-turing:<tag>` | `ghcr.io/huggingface/text-embeddings-inference:turing-1.9` | `rag-embed-ingest` on the serverless T4 profile | `TEI_INGEST_URL` |

Default model: `Qwen/Qwen3-Embedding-0.6B` (Apache-2.0, 1024 dimensions, multilingual), pinned by
`EmbedderModelRevision` in `infra/env/<env>.psd1`.

## How it works
1. **Build stage** (`python:3.12-slim`): `snapshot_download(MODEL_ID, revision=MODEL_REVISION, cache_dir=/data)` fills a
   Hugging Face hub cache, skipping alternative weight formats. The build fails unless `MODEL_REVISION` is a full 40-character SHA.
   It also writes `refs/<sha>` so TEI's Rust hub client finds the cached snapshot.
2. **Runtime stage** (TEI): `/data` is copied in. `MODEL_ID`, `REVISION`, `HUGGINGFACE_HUB_CACHE=/data`, `HF_HUB_OFFLINE=1`,
   `AUTO_TRUNCATE=true` and `--port 80` start the router from the cache.
3. `GET /info` then reports `model_id = Qwen/Qwen3-Embedding-0.6B` and `model_sha = <revision>`. The API and the workers
   compare these with the embedding profile and refuse to run on a mismatch.

## Build
In Azure, `infra/scripts/06-registry-build.ps1` builds both images with `az acr build` and records their digests in
`infra/env/<env>.images.json`. To build locally with Docker:

```powershell
$rev = '97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3'
docker build embedder -t rag-embedder-cpu:local `
  --build-arg TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 `
  --build-arg MODEL_ID=Qwen/Qwen3-Embedding-0.6B --build-arg MODEL_REVISION=$rev
docker run --rm -p 8081:80 rag-embedder-cpu:local
curl http://localhost:8081/info          # model_id / model_sha / max_input_length
curl http://localhost:8081/embed -H 'Content-Type: application/json' -d '{"inputs":["hello"]}'
```

## Runtime settings (env vars)
| Variable | Image default | Notes |
|---|---|---|
| `MAX_BATCH_TOKENS` | 16384 | Tokens per batch. The Container Apps templates set the CPU and GPU values from the psd1. Longer inputs are truncated (`AUTO_TRUNCATE=true`). |
| `MAX_CLIENT_BATCH_SIZE` | 64 | Inputs per request. Keep it at or above `INGEST_EMBED_BATCH`. |
| `MAX_CONCURRENT_REQUESTS` | 512 (TEI) | Back-pressure limit. |
| `RAYON_NUM_THREADS`, `TOKENIZATION_WORKERS` | 8 / #cores | CPU image only. Set them to the container's vCPU count. |

## Changing the model
Change `EmbedderModelId` and `EmbedderModelRevision` (or the dimensions), add a new profile to
`config/embedding/profiles.yaml`, rebuild with `06`, and follow **Deployment.md section 7 → Migrating to a different embedding model**.
A new profile always means a new index, and the running index is never re-embedded in place.

## Licences
TEI: Apache-2.0. Qwen3-Embedding-0.6B: Apache-2.0. Check the licence of any other model before you bake it into the image.
