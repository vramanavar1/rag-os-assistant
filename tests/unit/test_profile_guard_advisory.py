"""An idle ingestion pool is not a broken one.

`rag-embed-ingest` runs with `GpuMinReplicas = 0` — it scales to zero when there is nothing to ingest, and the
first batch after that waits a minute or two for a cold start. That is the design, not a fault.

So adding the ingestion pool to the readiness verdict is only useful if "not running" and "running the wrong
model" are told apart. Fold them together and `/api/readyz` sits at 503 on a perfectly healthy idle system,
every operator learns to ignore it, and the one case it exists to catch — a pool quietly serving a different
model — arrives looking exactly like the noise everybody has stopped reading.

The distinction is per *reason*, not per pool:

  unreachable + advisory  -> a note. `ok` stays true.
  unreachable + required  -> a failure.
  reachable but deviating -> a failure, advisory or not. Different model means a different vector space.
"""

from __future__ import annotations

import pytest

from rag_os.application.services.profile_guard import ProfileGuard
from rag_os.domain.embedding import EmbedderInfo, EmbeddingProfile

PROFILE = EmbeddingProfile(name="p", provider="tei", model="Qwen/Qwen3-Embedding-0.6B",
                           model_revision="97b0c614", dimensions=1024)


class FakeIndex:
    """Just enough SearchIndex for the guard: a stamped, matching index so only the pools are under test."""

    index_name = "kb-enterprise-deadbeef"

    def __init__(self, fingerprint: str) -> None:
        self._fp = fingerprint

    async def index_exists(self) -> bool:
        return True

    async def read_profile(self) -> dict[str, object]:
        return {"fingerprint": self._fp}


class Pool:
    def __init__(self, info: EmbedderInfo | None = None, error: Exception | None = None) -> None:
        self._info, self._error = info, error
        self.probes = 0

    async def info(self) -> EmbedderInfo:
        self.probes += 1
        if self._error:
            raise self._error
        assert self._info is not None
        return self._info


MATCHING = EmbedderInfo(model="Qwen/Qwen3-Embedding-0.6B", revision="97b0c614", dimensions=1024)
DEVIATING = EmbedderInfo(model="BAAI/bge-small-en-v1.5", revision="zzz", dimensions=1024)


def guard() -> tuple[ProfileGuard, FakeIndex]:
    g = ProfileGuard(PROFILE)
    return g, FakeIndex(g.fp)


async def test_an_idle_advisory_pool_does_not_fail_the_verdict() -> None:
    """The regression that would make readyz cry wolf on every idle deployment."""
    g, index = guard()
    st = await g.check(index, {"query": Pool(MATCHING), "ingest": Pool(error=ConnectionError("no replicas"))},
                       advisory_pools=frozenset({"ingest"}))
    assert st.ok is True, f"a pool that is allowed to be absent must not fail readiness: {st.reasons}"
    assert st.reasons == [], st.reasons
    assert any("ingest" in n for n in st.notes), f"it still has to be reported: {st.notes}"
    assert any("scaled to zero" in n for n in st.notes), st.notes


async def test_the_same_pool_still_fails_when_it_is_required() -> None:
    """Advisory is a property of the question being asked, not of the pool."""
    g, index = guard()
    st = await g.check(index, {"ingest": Pool(error=ConnectionError("no replicas"))})
    assert st.ok is False, "the worker asks about the ingest pool because it is about to use it"
    assert any("embedder unavailable" in r for r in st.reasons), st.reasons
    assert st.notes == [], st.notes


async def test_an_advisory_pool_that_answers_is_still_compared() -> None:
    """The whole point. Reachable means judged - being allowed to be absent is not being allowed to differ."""
    g, index = guard()
    st = await g.check(index, {"query": Pool(MATCHING), "ingest": Pool(DEVIATING)},
                       advisory_pools=frozenset({"ingest"}))
    assert st.ok is False, "a pool serving a different model writes vectors into a different space"
    joined = " ".join(st.reasons)
    assert "ingest: model" in joined, joined
    assert "BAAI/bge-small-en-v1.5" in joined, joined


async def test_a_matching_advisory_pool_is_silent() -> None:
    g, index = guard()
    st = await g.check(index, {"query": Pool(MATCHING), "ingest": Pool(MATCHING)},
                       advisory_pools=frozenset({"ingest"}))
    assert st.ok is True, st.reasons
    assert st.notes == [], f"nothing to say when it is running and correct: {st.notes}"


# ------------------------------------------------------------------- the cache must answer the right question
async def test_a_verdict_for_one_set_of_pools_is_not_reused_for_another() -> None:
    """One process asks two different questions within the 60s window.

    The chat path asks about the query pool; readyz asks about both. A single cache slot served whichever ran
    first, so readyz could report "ready" having never looked at the ingestion pool at all - the exact check
    being added here, silently skipped.
    """
    g, index = guard()
    ingest = Pool(DEVIATING)

    query_only = await g.check(index, {"query": Pool(MATCHING)})
    assert query_only.ok is True and ingest.probes == 0

    both = await g.check(index, {"query": Pool(MATCHING), "ingest": ingest})
    assert ingest.probes == 1, "the second question must actually be asked, not answered from the first"
    assert both.ok is False, "and its answer must reflect the pool it asked about"


async def test_the_same_question_inside_the_window_is_cached() -> None:
    """The caching is still worth having: chat asks this on every question."""
    g, index = guard()
    pool = Pool(MATCHING)
    await g.check(index, {"query": pool})
    await g.check(index, {"query": pool})
    assert pool.probes == 1, "a repeated identical question should not re-probe within recheck_seconds"

    await g.check(index, {"query": pool}, force=True)
    assert pool.probes == 2, "force must always re-probe"


async def test_invalidate_drops_every_cached_verdict() -> None:
    g, index = guard()
    pool = Pool(MATCHING)
    await g.check(index, {"query": pool})
    g.invalidate()
    await g.check(index, {"query": pool})
    assert pool.probes == 2


# ------------------------------------------------------------------------------ what the pool actually said
async def test_observed_returns_what_the_pool_reported_not_what_was_configured() -> None:
    """Everything else in the system records configuration. This is the only thing that can contradict it."""
    g, index = guard()
    assert g.observed("ingest") is None, "nothing has been asked yet"

    await g.check(index, {"ingest": Pool(DEVIATING)})
    seen = g.observed("ingest")
    assert seen is not None
    assert seen["model"] == "BAAI/bge-small-en-v1.5", seen
    assert seen["model"] != PROFILE.model, "a copy of the configured model would prove nothing"


@pytest.mark.parametrize("pool_name", ["query", "ingest"])
async def test_observed_is_per_pool(pool_name: str) -> None:
    g, index = guard()
    await g.check(index, {pool_name: Pool(MATCHING)})
    assert g.observed(pool_name) is not None
    other = "ingest" if pool_name == "query" else "query"
    assert g.observed(other) is None, "asking about one pool says nothing about the other"
