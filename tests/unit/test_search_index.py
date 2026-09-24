"""Index compression config and the retriever's two score thresholds.

Both behaviours here previously failed *open*: `SEARCH_COMPRESSION=binary` built an uncompressed index, and
`RETRIEVAL_MIN_SCORE` was declared and documented but never read. Neither raised, logged or showed up anywhere.
"""

from __future__ import annotations

from typing import Any

import pytest

from rag_os.application.ports import IndexField, IndexSchema, RetrievalResult, SearchRequest
from rag_os.domain.answers import SearchHit
from rag_os.infrastructure.search.azure_search import AzureSearchIndex
from rag_os.infrastructure.search.retrievers import DirectSearchRetriever

ENDPOINT = "https://srch.test.search.windows.net"


def _schema(compression: str) -> IndexSchema:
    return IndexSchema(
        name="kb-test-0123456789",
        fields=[
            IndexField("chunk_id", "string", key=True),
            IndexField("title", "string"),
            IndexField("heading", "string"),
            IndexField("content", "string"),
            IndexField("vector", "vector", retrievable=False, dimensions=1024),
        ],
        compression=compression,
    )


def _index() -> AzureSearchIndex:
    # The credential and clients are constructed lazily and never used: _build is pure.
    return AzureSearchIndex(index_name="kb-test-0123456789", endpoint=ENDPOINT)


@pytest.mark.parametrize(("compression", "expected"), [("scalar", "sq"), ("binary", "bq")])
def test_compression_is_actually_configured(compression: str, expected: str) -> None:
    """`binary` used to produce an UNCOMPRESSED index: the branch only tested for `scalar`.

    No error, no warning - just an index quietly four times larger than asked for, which nobody would notice
    until the bill or the partition filled up.
    """
    built = _index()._build(_schema(compression), None)
    names = [c.compression_name for c in (built.vector_search.compressions or [])]
    assert names == [expected], f"{compression} produced {names}"
    assert built.vector_search.profiles[0].compression_name == expected


def test_no_compression_configures_none() -> None:
    built = _index()._build(_schema("none"), None)
    assert not built.vector_search.compressions
    assert built.vector_search.profiles[0].compression_name is None


# ------------------------------------------------------------------ retriever thresholds


class _StubIndex:
    """Returns a fixed hit list; records the request so the test can assert on it."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.request: SearchRequest | None = None

    async def search(self, request: SearchRequest) -> list[SearchHit]:
        self.request = request
        return self.hits


class _StubEmbedder:
    async def embed_query(self, text: str) -> tuple[list[float], Any]:
        from rag_os.domain.answers import TokenUsage

        return [0.1] * 4, TokenUsage()


def _hit(chunk_id: str, score: float, reranker: float | None) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, doc_id="d", title="t", heading="", content="c", path="p", page=None,
                     source_id="s", score=score, reranker_score=reranker)


async def _retrieve(hits: list[SearchHit], **kwargs: Any) -> RetrievalResult:
    r = DirectSearchRetriever(index=_StubIndex(hits), embedder=_StubEmbedder(), **kwargs)  # type: ignore[arg-type]
    return await r.retrieve(query="q", keyword_query="q", odata_filter=None, top=8)


@pytest.mark.asyncio()
async def test_min_score_drops_weak_hits() -> None:
    """RETRIEVAL_MIN_SCORE was documented as "minimum hybrid score" and read by nothing at all."""
    hits = [_hit("keep", 0.030, 3.0), _hit("drop", 0.005, 3.0)]
    out = await _retrieve(hits, min_score=0.02)
    assert [h.chunk_id for h in out.hits] == ["keep"]


@pytest.mark.asyncio()
async def test_min_score_zero_is_a_no_op() -> None:
    """The shipped default. Every existing deployment must behave exactly as before."""
    hits = [_hit("a", 0.030, 3.0), _hit("b", 0.001, 3.0)]
    out = await _retrieve(hits, min_score=0.0)
    assert [h.chunk_id for h in out.hits] == ["a", "b"]


@pytest.mark.asyncio()
async def test_reranker_threshold_still_filters_independently() -> None:
    hits = [_hit("a", 0.03, 2.0), _hit("b", 0.03, 0.5)]
    out = await _retrieve(hits, min_reranker_score=1.2)
    assert [h.chunk_id for h in out.hits] == ["a"]


@pytest.mark.asyncio()
async def test_a_hit_without_a_reranker_score_survives() -> None:
    """Local backends and semantic-off deployments report None; the threshold must not silently empty them."""
    hits = [_hit("a", 0.03, None)]
    out = await _retrieve(hits, min_reranker_score=1.2)
    assert [h.chunk_id for h in out.hits] == ["a"]
