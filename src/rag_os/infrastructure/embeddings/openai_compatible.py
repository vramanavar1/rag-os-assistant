"""Embedding providers speaking the OpenAI embeddings protocol.

* ``tei``          - self-hosted Hugging Face Text Embeddings Inference (open-weights model, no token cost).
                     Uses ``/v1/embeddings`` (returns token usage) and ``/info`` (model id + revision).
* ``azure_openai`` - Azure OpenAI / Foundry deployment (keyless Entra auth).

Profile semantics applied here (identically for ingestion and query): query/document prefixes,
optional Matryoshka truncation to ``profile.dimensions`` and L2 normalisation.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
from collections.abc import Sequence
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from rag_os.application.ports import EmbeddingProvider
from rag_os.domain.answers import TokenUsage
from rag_os.domain.embedding import EmbedderInfo, EmbeddingProfile
from rag_os.domain.errors import DependencyUnavailable, ProfileMismatch
from rag_os.infrastructure.registry import EMBEDDERS

log = logging.getLogger(__name__)


def _post_process(vec: list[float], profile: EmbeddingProfile) -> list[float]:
    if len(vec) < profile.dimensions:
        raise ProfileMismatch(f"embedding has {len(vec)} dims, profile requires {profile.dimensions}")
    if len(vec) > profile.dimensions:
        if profile.native_dimensions is None:
            raise ProfileMismatch(
                f"embedding has {len(vec)} dims but profile declares {profile.dimensions} "
                "without native_dimensions (truncation not allowed)"
            )
        vec = vec[: profile.dimensions]
    if profile.normalize:
        n = math.sqrt(sum(x * x for x in vec)) or 1.0
        vec = [x / n for x in vec]
    return vec


_RETRY_STATUS = (408, 429, 500, 502, 503, 504)
# One policy for both adapters. Named so there is a single place to reason about how long a throttled batch
# blocks a worker slot - and so a test can make the waiting instant without reaching inside a function.
_RETRY_ATTEMPTS = 6
_RETRY_WAIT = wait_exponential_jitter(initial=0.5, max=20)


@functools.lru_cache(maxsize=1)
def _openai_transient() -> tuple[type[BaseException], ...]:
    """The openai SDK's connection errors, imported lazily so a TEI-only deployment never pays for it."""
    try:
        from openai import APIConnectionError, APITimeoutError
    except Exception:  # pragma: no cover - the dependency is declared; absence is not a retry decision
        return ()
    return (APIConnectionError, APITimeoutError)


def _retryable(e: BaseException) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in _RETRY_STATUS
    if isinstance(e, httpx.TransportError):
        return True
    # The Azure OpenAI path raises openai SDK errors, not httpx ones. They carry the status code, so a 429 is
    # matched the same way a TEI 429 is - which matters because a token-per-minute throttle is the *normal*
    # condition when back-filling a large corpus, not an exceptional one.
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return status in _RETRY_STATUS
    return isinstance(e, _openai_transient())


@EMBEDDERS.register("tei", description="Self-hosted TEI server (e.g. Qwen3-Embedding) - zero token cost.")
class TeiEmbeddingProvider(EmbeddingProvider):
    def __init__(self, profile: EmbeddingProfile, base_url: str, *, batch_size: int = 32, concurrency: int = 4,
                 timeout_s: float = 60.0, **_: Any) -> None:
        super().__init__(profile)
        if not base_url:
            raise DependencyUnavailable("TEI url is not configured")
        self.base_url = base_url.rstrip("/")
        self.batch_size = max(1, batch_size)
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def info(self) -> EmbedderInfo:
        try:
            r = await self._http.get("/info")
            r.raise_for_status()
            data = r.json()
            probe, _ = await self._raw_embed(["dimension probe"])
        except httpx.HTTPError as e:
            raise DependencyUnavailable(f"TEI at {self.base_url} not reachable", detail={"error": str(e)}) from e
        return EmbedderInfo(
            model=str(data.get("model_id") or ""),
            revision=str(data.get("model_sha") or ""),
            dimensions=len(probe[0]),
            max_input_tokens=data.get("max_input_length"),
        )

    async def _raw_embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_retryable),
            wait=_RETRY_WAIT,
            stop=stop_after_attempt(_RETRY_ATTEMPTS),
            reraise=True,
        ):
            with attempt:
                r = await self._http.post("/v1/embeddings", json={"input": texts, "model": self.profile.model})
                if r.status_code == 413 and len(texts) > 1:  # batch too large for server limits -> split
                    mid = len(texts) // 2
                    a, ua = await self._raw_embed(texts[:mid])
                    b, ub = await self._raw_embed(texts[mid:])
                    return a + b, ua + ub
                r.raise_for_status()
                body = r.json()
                data = sorted(body["data"], key=lambda d: d["index"])
                usage = int((body.get("usage") or {}).get("prompt_tokens") or 0)
                return [d["embedding"] for d in data], usage
        raise AssertionError("unreachable")

    async def _embed(self, texts: Sequence[str], prefix: str) -> tuple[list[list[float]], TokenUsage]:
        batches = [list(texts[i:i + self.batch_size]) for i in range(0, len(texts), self.batch_size)]

        async def run(batch: list[str]) -> tuple[list[list[float]], int]:
            async with self._sem:
                return await self._raw_embed([prefix + t for t in batch])

        try:
            results = await asyncio.gather(*(run(b) for b in batches))
        except httpx.HTTPError as e:
            raise DependencyUnavailable("embedding request failed", detail={"error": str(e)}) from e
        vectors = [_post_process(v, self.profile) for vs, _ in results for v in vs]
        usage = TokenUsage(embedding=sum(u for _, u in results), calls=len(batches))
        return vectors, usage

    async def embed_documents(self, texts: Sequence[str]) -> tuple[list[list[float]], TokenUsage]:
        return await self._embed(texts, self.profile.document_prefix)

    async def embed_query(self, text: str) -> tuple[list[float], TokenUsage]:
        vecs, usage = await self._embed([text], self.profile.query_prefix)
        return vecs[0], usage


@EMBEDDERS.register("azure_openai", description="Azure OpenAI embeddings deployment on the Foundry account.")
class AzureOpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, profile: EmbeddingProfile, endpoint: str, deployment: str, api_version: str,
                 api_key: str | None = None, *, batch_size: int = 64, concurrency: int = 4, **_: Any) -> None:
        super().__init__(profile)
        from openai import AsyncAzureOpenAI

        if not endpoint or not deployment:
            raise DependencyUnavailable("AOAI_ENDPOINT / AOAI_EMBED_DEPLOYMENT not configured")
        kwargs: dict[str, Any] = {"azure_endpoint": endpoint, "api_version": api_version}
        if api_key:
            kwargs["api_key"] = api_key
        else:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            self._cred = DefaultAzureCredential()
            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                self._cred, "https://cognitiveservices.azure.com/.default"
            )
        self._client = AsyncAzureOpenAI(**kwargs)
        self.deployment = deployment
        self.batch_size = batch_size
        self._sem = asyncio.Semaphore(concurrency)
        self._info: EmbedderInfo | None = None

    async def info(self) -> EmbedderInfo:
        """Cached, because this probe is a BILLED embeddings call.

        Unlike TEI's `/info`, there is nothing free to ask Azure OpenAI: the only way to observe the vector
        width is to embed something. And the model and revision reported here are the *configured* ones echoed
        back - the SDK exposes no deployment identity - so the one genuinely observed field is the width, which
        cannot change without redeploying the model. Re-probing per readiness check bought nothing and billed
        for it, twice per check while two pools pointed at one deployment.
        """
        if self._info is None:
            vecs, _ = await self.embed_documents(["dimension probe"])
            self._info = EmbedderInfo(model=self.profile.model, revision=self.profile.model_revision,
                                      dimensions=len(vecs[0]))
        return self._info

    async def _embed(self, texts: Sequence[str], prefix: str) -> tuple[list[list[float]], TokenUsage]:
        batches = [list(texts[i:i + self.batch_size]) for i in range(0, len(texts), self.batch_size)]

        async def run(batch: list[str]) -> tuple[list[list[float]], int]:
            async with self._sem:
                # The SDK retries twice by default, which a token-per-minute throttle exhausts immediately. Left
                # at that, a 429 propagated out of the worker's handler and the WHOLE document went back on the
                # queue, to be dead-lettered after queue_max_delivery deliveries - so a throughput limit read as
                # a corpus of permanently failing documents. Same backoff as the TEI path.
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception(_retryable),
                    wait=_RETRY_WAIT,
                    stop=stop_after_attempt(_RETRY_ATTEMPTS),
                    reraise=True,
                ):
                    with attempt:
                        resp = await self._client.embeddings.create(
                            model=self.deployment, input=[prefix + t for t in batch],
                            dimensions=self.profile.dimensions,
                        )
                        return [d.embedding for d in resp.data], int(
                            resp.usage.prompt_tokens if resp.usage else 0)
            raise AssertionError("unreachable: AsyncRetrying with reraise=True either returns or raises")

        try:
            results = await asyncio.gather(*(run(b) for b in batches))
        except Exception as e:
            # Mapped to the error the rest of the system is written around, so a throttle that outlasts the
            # backoff is handled like any other unavailable dependency instead of as a bare SDK exception.
            if isinstance(e, ProfileMismatch | DependencyUnavailable):
                raise
            raise DependencyUnavailable("embedding request failed", detail={"error": str(e)}) from e
        vectors = [_post_process(list(v), self.profile) for vs, _ in results for v in vs]
        return vectors, TokenUsage(embedding=sum(u for _, u in results), calls=len(batches))

    async def embed_documents(self, texts: Sequence[str]) -> tuple[list[list[float]], TokenUsage]:
        return await self._embed(texts, self.profile.document_prefix)

    async def embed_query(self, text: str) -> tuple[list[float], TokenUsage]:
        vecs, usage = await self._embed([text], self.profile.query_prefix)
        return vecs[0], usage

    async def aclose(self) -> None:
        await self._client.close()
        # The keyless branch opens a DefaultAzureCredential with its own aio session; closing only the OpenAI
        # client leaked it for the lifetime of a worker process.
        cred = getattr(self, "_cred", None)
        if cred is not None:
            await cred.close()
