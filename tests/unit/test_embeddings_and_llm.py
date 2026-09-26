"""TEI embedding adapter (HTTP mocked), profile guard, and LLM usage normalisation."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
import yaml
from pydantic import ValidationError

from rag_os.application.services.profile_guard import ProfileGuard
from rag_os.composition import Container
from rag_os.domain.embedding import EmbeddingProfile
from rag_os.domain.errors import ProfileMismatch
from rag_os.infrastructure.embeddings.openai_compatible import TeiEmbeddingProvider
from rag_os.infrastructure.llm.azure_openai import normalise_openai_usage
from rag_os.infrastructure.llm.claude_foundry import normalise_anthropic_usage
from rag_os.infrastructure.search.in_memory import InMemorySearchIndex
from rag_os.infrastructure.settings import Settings

PROFILE = EmbeddingProfile(name="q", provider="tei", model="Qwen/Qwen3-Embedding-0.6B", model_revision="abc",
                           dimensions=4, query_prefix="Q: ", document_prefix="")
BASE = "http://tei.test"


def _vectors(request: httpx.Request) -> httpx.Response:
    body = __import__("json").loads(request.content)
    data = [{"index": i, "embedding": [3.0, 4.0, 0.0, float(len(t))]} for i, t in enumerate(body["input"])]
    return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 7 * len(data)}})


@respx.mock
async def test_tei_prefixes_batches_and_normalises() -> None:
    route = respx.post(f"{BASE}/v1/embeddings").mock(side_effect=_vectors)
    emb = TeiEmbeddingProvider(PROFILE, BASE, batch_size=2)
    vecs, usage = await emb.embed_documents(["a", "bb", "ccc"])
    assert route.call_count == 2 and usage.embedding == 21 and usage.calls == 2
    assert all(math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-6) for v in vecs)
    await emb.embed_query("hello")
    sent = __import__("json").loads(route.calls.last.request.content)["input"]
    assert sent == ["Q: hello"]
    await emb.aclose()


@respx.mock
async def test_tei_splits_on_413_and_reports_info() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if len(__import__("json").loads(request.content)["input"]) > 1:
            return httpx.Response(413)
        return _vectors(request)

    respx.post(f"{BASE}/v1/embeddings").mock(side_effect=handler)
    respx.get(f"{BASE}/info").mock(return_value=httpx.Response(200, json={
        "model_id": "Qwen/Qwen3-Embedding-0.6B", "model_sha": "abc", "max_input_length": 8192}))
    emb = TeiEmbeddingProvider(PROFILE, BASE, batch_size=4)
    vecs, _ = await emb.embed_documents(["a", "b", "c", "d"])
    assert len(vecs) == 4
    info = await emb.info()
    assert info.model == "Qwen/Qwen3-Embedding-0.6B" and info.dimensions == 4 and info.revision == "abc"
    await emb.aclose()


@respx.mock
async def test_profile_guard_detects_model_revision_and_index_mismatch() -> None:
    respx.post(f"{BASE}/v1/embeddings").mock(side_effect=_vectors)
    respx.get(f"{BASE}/info").mock(return_value=httpx.Response(200, json={
        "model_id": "BAAI/bge-small-en-v1.5", "model_sha": "zzz"}))
    emb = TeiEmbeddingProvider(PROFILE, BASE)
    index = InMemorySearchIndex("kb-x")
    guard = ProfileGuard(PROFILE)
    st = await guard.check(index, {"query": emb}, force=True)
    assert not st.ok
    joined = " ".join(st.reasons)
    # The index was never created here, so the reason must say that rather than the older sentence which also
    # covered an index that exists but was never stamped - those need different remedies.
    assert "model" in joined and "revision" in joined
    assert "index 'kb-x' does not exist" in joined, joined
    await index.write_profile({"fingerprint": "other"})
    with pytest.raises(ProfileMismatch):
        guard.invalidate()
        await guard.require(index, {"query": emb})
    await emb.aclose()


def test_truncation_requires_native_dimensions() -> None:
    from rag_os.infrastructure.embeddings.openai_compatible import _post_process

    with pytest.raises(ProfileMismatch):
        _post_process([1.0] * 8, PROFILE)  # 8 dims but profile says 4 without native_dimensions
    mrl = PROFILE.model_copy(update={"native_dimensions": 8})
    assert len(_post_process([1.0] * 8, mrl)) == 4


def test_usage_normalisation() -> None:
    oa = normalise_openai_usage(SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                                prompt_tokens_details=SimpleNamespace(cached_tokens=60)))
    assert (oa.input, oa.cache_read, oa.output) == (40, 60, 20)
    an = normalise_anthropic_usage(SimpleNamespace(input_tokens=30, output_tokens=10, cache_read_input_tokens=500,
                                                   cache_creation_input_tokens=0))
    assert (an.input, an.cache_read, an.output) == (30, 500, 10)


# ------------------------------------------------------------------ per-role LLM selection


def _container(**overrides: object) -> Container:
    """A Container whose settings are valid enough to resolve LLM roles (nothing is called)."""
    return Container(Settings(
        app_env="test", queue="in_memory", search_backend="in_memory", embedding_profile="test-fake-256",
        aoai_endpoint="https://example.openai.azure.com", otel_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
        **overrides))  # type: ignore[arg-type]


def test_roles_share_one_client_when_they_resolve_to_the_same_model() -> None:
    """The common case: one provider, one deployment, one client — no second connection pool."""
    c = _container(llm_answer="aoai", llm_utility="aoai", aoai_chat_deployment="gpt-5-mini")
    assert c.llm_utility is c.llm_answer


def test_a_distinct_utility_deployment_gets_its_own_client() -> None:
    """The bug this guards: llm_utility used to short-circuit on the provider NAME alone.

    With both roles on "aoai" it returned the answer client, so a cheaper utility deployment was silently
    ignored and every condense call was billed at the answer model's rate — a cost setting that looks applied
    and is not.
    """
    c = _container(llm_answer="aoai", llm_utility="aoai",
                   aoai_chat_deployment="gpt-5-mini", aoai_utility_deployment="gpt-4o-mini")
    assert c.llm_utility is not c.llm_answer
    assert c.llm_answer.deployment == "gpt-5-mini"
    assert c.llm_utility.deployment == "gpt-4o-mini"


def test_utility_deployment_defaults_to_the_answer_deployment() -> None:
    s = Settings(aoai_chat_deployment="gpt-5-mini", claude_model="claude-sonnet-5",
                 _env_file=None)  # type: ignore[call-arg]
    assert s.aoai_utility == "gpt-5-mini"
    assert s.claude_utility == "claude-sonnet-5"
    s2 = Settings(aoai_chat_deployment="gpt-5-mini", aoai_utility_deployment="gpt-4o-mini",
                  claude_model="claude-sonnet-5", claude_utility_model="claude-haiku-4-5",
                  _env_file=None)  # type: ignore[call-arg]
    assert s2.aoai_utility == "gpt-4o-mini"
    assert s2.claude_utility == "claude-haiku-4-5"


# ------------------------------------------------------------------ profile prefix direction


def test_a_backwards_prefix_pair_is_rejected() -> None:
    """Instructing the document instead of the query degrades recall with nothing complaining at runtime."""
    with pytest.raises(ValidationError, match="wrong way round"):
        EmbeddingProfile(name="backwards", provider="tei", model="m", dimensions=4,
                         query_prefix="", document_prefix="Instruct: retrieve passages\nQuery:")


def test_legitimate_prefix_shapes_still_load() -> None:
    EmbeddingProfile(name="qwen-like", provider="tei", model="m", dimensions=4,
                     query_prefix="Instruct: ...", document_prefix="")          # asymmetric, query-only
    EmbeddingProfile(name="e5-like", provider="tei", model="m", dimensions=4,
                     query_prefix="query: ", document_prefix="passage: ")       # asymmetric, both sides
    EmbeddingProfile(name="symmetric", provider="tei", model="m", dimensions=4) # neither


def test_every_shipped_profile_loads_and_the_default_fingerprint_is_stable() -> None:
    """A changed fingerprint means a renamed index and a full re-ingest - never an accident."""
    doc = yaml.safe_load(Path("config/embedding/profiles.yaml").read_text(encoding="utf-8"))
    profiles = {n: EmbeddingProfile(name=n, **body) for n, body in doc["profiles"].items()}
    assert set(profiles) == {"qwen3-0.6b-1024", "qwen3-0.6b-512", "aoai-3-small-1536",
                             "test-fake-256", "test-fake-512"}
    # Only one model is ever active: the provider field selects the adapter.
    assert profiles["qwen3-0.6b-1024"].provider == "tei"
    assert profiles["aoai-3-small-1536"].provider == "azure_openai"
    # The 512 profile is the same model truncated, so it must declare what the server actually emits.
    assert profiles["qwen3-0.6b-512"].native_dimensions == 1024
    assert profiles["qwen3-0.6b-512"].model == profiles["qwen3-0.6b-1024"].model
    assert profiles["qwen3-0.6b-1024"].fingerprint() != profiles["qwen3-0.6b-512"].fingerprint()
