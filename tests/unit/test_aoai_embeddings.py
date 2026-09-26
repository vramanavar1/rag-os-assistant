"""The Azure OpenAI embedding adapter, which had no tests at all.

It is the alternative to self-hosting, so it is reached exactly when someone is already having a bad day - a GPU
quota refusal, a model that will not start, a cost review. The failure modes that matter are not exotic:

* **A 429 is the normal condition, not an exception.** Back-filling a large corpus against a token-per-minute
  quota throttles constantly. Unretried, a throttle propagated out of the worker's handler and the *whole
  document* went back on the queue, to be dead-lettered after `queue_max_delivery` deliveries - so a throughput
  limit looked like a corpus of permanently broken documents.
* **`info()` costs money.** There is nothing free to ask Azure: the only way to observe the vector width is to
  embed something, and the readiness check asks on every refresh.
* **Keyless is the default**, so the credential is part of the object's lifetime and has to be closed.

The client is stubbed rather than mocked at the HTTP layer. respx does not intercept the openai SDK's own client
(the first attempt at these tests reached the real endpoint and got a 401), and the behaviour under test is this
adapter's - batching, retry, caching, error mapping, prefixes, normalisation - not the SDK's transport. The SDK's
real exception types are used, so the retry predicate is exercised against the thing it will actually see.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, RateLimitError
from tenacity import wait_none

from rag_os.domain.embedding import EmbeddingProfile
from rag_os.domain.errors import DependencyUnavailable
from rag_os.infrastructure.embeddings import openai_compatible
from rag_os.infrastructure.embeddings.openai_compatible import AzureOpenAIEmbeddingProvider, _retryable


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real retry COUNT and predicate, drop only the waiting.

    Left alone these tests spend the genuine exponential backoff - about 15 seconds for one exhausted retry -
    which is time spent proving that tenacity can sleep.
    """
    monkeypatch.setattr(openai_compatible, "_RETRY_WAIT", wait_none())


ENDPOINT = "https://example-foundry.openai.azure.com"
DEPLOYMENT = "text-embedding-3-small"
PROFILE = EmbeddingProfile(name="aoai", provider="azure_openai", model="text-embedding-3-small",
                           dimensions=8, normalize=True)


def throttle() -> RateLimitError:
    request = httpx.Request("POST", f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}/embeddings")
    return RateLimitError("token rate limit exceeded", response=httpx.Response(429, request=request), body=None)


def vectors(n: int, first: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        data=[SimpleNamespace(index=i, embedding=[first] + [0.0] * 7) for i in range(n)],
        usage=SimpleNamespace(prompt_tokens=7 * n),
    )


class StubEmbeddings:
    def __init__(self, script: list[Any]) -> None:
        self._script, self.calls = list(script), []

    async def create(self, *, model: str, input: list[str], dimensions: int) -> Any:
        self.calls.append({"model": model, "input": list(input), "dimensions": dimensions})
        result = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(result, BaseException):
            raise result
        return result if not callable(result) else result(len(input))


class StubClient:
    def __init__(self, script: list[Any]) -> None:
        self.embeddings = StubEmbeddings(script)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def provider(script: list[Any] | None = None, **kwargs: Any) -> AzureOpenAIEmbeddingProvider:
    """Keyed auth by default - it avoids reaching for a real credential chain inside a unit test."""
    options: dict[str, Any] = {"api_key": "test-key", "batch_size": 2, "concurrency": 2}
    options.update(kwargs)
    p = AzureOpenAIEmbeddingProvider(PROFILE, ENDPOINT, DEPLOYMENT, "2024-10-21", **options)
    p._client = StubClient(script if script is not None else [lambda n: vectors(n)])  # type: ignore[assignment]
    return p


# ------------------------------------------------------------------------------------------- configuration
def test_a_missing_deployment_is_refused_at_construction() -> None:
    """Better here than on the first embedding call, which is a user's question or an ingested document."""
    with pytest.raises(DependencyUnavailable, match="AOAI_EMBED_DEPLOYMENT"):
        AzureOpenAIEmbeddingProvider(PROFILE, ENDPOINT, "", "2024-10-21", api_key="k")
    with pytest.raises(DependencyUnavailable, match="AOAI_ENDPOINT"):
        AzureOpenAIEmbeddingProvider(PROFILE, "", DEPLOYMENT, "2024-10-21", api_key="k")


def test_keyless_is_the_default_and_builds_a_token_provider() -> None:
    """An empty key is not a misconfiguration - it selects Entra auth, which is how the deployment runs."""
    p = AzureOpenAIEmbeddingProvider(PROFILE, ENDPOINT, DEPLOYMENT, "2024-10-21", api_key=None)
    assert getattr(p, "_cred", None) is not None, "keyless must construct a credential to close later"


def test_keyed_auth_does_not_build_a_credential() -> None:
    assert getattr(provider(), "_cred", None) is None


# -------------------------------------------------------------------------------------------- the request
async def test_the_vector_width_is_requested_from_the_api_not_truncated_locally() -> None:
    """Azure reduces server-side, so asking for the profile's width is what keeps the two in step - and it is
    why an MRL profile needs no native_dimensions on this path, unlike TEI."""
    p = provider()
    await p.embed_query("hello")
    assert p._client.embeddings.calls[0]["dimensions"] == 8  # type: ignore[attr-defined]
    assert p._client.embeddings.calls[0]["model"] == DEPLOYMENT, "the DEPLOYMENT name, not the model name"


async def test_the_query_prefix_is_applied_and_vectors_are_normalised() -> None:
    profile = PROFILE.model_copy(update={"query_prefix": "Q: "})
    p = AzureOpenAIEmbeddingProvider(profile, ENDPOINT, DEPLOYMENT, "2024-10-21", api_key="k")
    p._client = StubClient([SimpleNamespace(  # type: ignore[assignment]
        data=[SimpleNamespace(index=0, embedding=[3.0, 4.0] + [0.0] * 6)],
        usage=SimpleNamespace(prompt_tokens=3))])
    vec, usage = await p.embed_query("hello")
    assert p._client.embeddings.calls[0]["input"] == ["Q: hello"]  # type: ignore[attr-defined]
    assert abs(sum(x * x for x in vec) - 1.0) < 1e-6, "normalize: true must be honoured"
    assert usage.embedding == 3


async def test_documents_are_batched_to_the_configured_size() -> None:
    p = provider(batch_size=2)
    vecs, usage = await p.embed_documents(["a", "b", "c", "d", "e"])
    calls = p._client.embeddings.calls  # type: ignore[attr-defined]
    assert [len(c["input"]) for c in calls] == [2, 2, 1], calls
    assert len(vecs) == 5 and usage.calls == 3


# ---------------------------------------------------------------------------------------- rate limiting
async def test_a_throttle_is_retried_and_then_succeeds() -> None:
    """The case that made a TPM limit look like a corpus of broken documents."""
    p = provider([throttle(), throttle(), vectors(1)])
    got, _ = await p.embed_documents(["a"])
    assert len(got) == 1
    assert len(p._client.embeddings.calls) == 3, "two throttles should be absorbed, not surfaced"  # type: ignore[attr-defined]


async def test_a_persistent_throttle_becomes_a_dependency_error_not_a_bare_sdk_exception() -> None:
    """The rest of the system is written around DependencyUnavailable; an openai.RateLimitError is handled
    nowhere and would surface through the chat path unmapped."""
    p = provider([throttle()])
    with pytest.raises(DependencyUnavailable):
        await p.embed_documents(["a"])
    assert len(p._client.embeddings.calls) == 6, "the full backoff should be spent before giving up"  # type: ignore[attr-defined]


async def test_a_bad_request_is_not_retried() -> None:
    """A 400 will not become a 200 by waiting; retrying it just delays the error by the whole backoff."""
    class BadRequest(Exception):
        status_code = 400

    p = provider([BadRequest("dimensions not supported")])
    with pytest.raises(DependencyUnavailable):
        await p.embed_documents(["a"])
    assert len(p._client.embeddings.calls) == 1  # type: ignore[attr-defined]


def test_the_retry_predicate_recognises_the_sdk_errors_too() -> None:
    """It was written for httpx exceptions; the Azure path raises openai ones, which carry a status code."""
    request = httpx.Request("POST", ENDPOINT)
    assert _retryable(throttle()) is True
    assert _retryable(APITimeoutError(request=request)) is True
    assert _retryable(APIConnectionError(request=request)) is True
    assert _retryable(ValueError("unrelated")) is False

    class BadRequest(Exception):
        status_code = 400

    assert _retryable(BadRequest()) is False


# -------------------------------------------------------------------------------- the billed probe, and cleanup
async def test_info_is_probed_once_and_then_cached() -> None:
    """Every call is billed, and the readiness check asks on every refresh."""
    p = provider()
    first = await p.info()
    second = await p.info()
    assert first.dimensions == 8 and second is first
    assert len(p._client.embeddings.calls) == 1, "a second readiness check must not pay again"  # type: ignore[attr-defined]


async def test_info_reports_the_configured_model_which_is_a_known_limitation() -> None:
    """Pinned deliberately: the SDK exposes no deployment identity, so this cannot catch a repointed deployment.
    The alignment gate and the docs both say so, and this test is what keeps that claim honest."""
    p = provider()
    info = await p.info()
    assert info.model == PROFILE.model, "it echoes configuration - only `dimensions` is genuinely observed"


async def test_aclose_closes_the_credential_as_well_as_the_client() -> None:
    """A leaked aio credential session lives for the whole worker process."""
    closed: list[str] = []

    class Spy:
        async def close(self) -> None:
            closed.append("cred")

    p = provider()
    p._cred = Spy()  # type: ignore[attr-defined]
    await p.aclose()
    assert closed == ["cred"], "the credential must be closed too"
    assert p._client.closed is True, "and the client, as before"  # type: ignore[attr-defined]
