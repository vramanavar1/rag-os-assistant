"""Retrievers. ``direct`` = query embedding (self-hosted model) + hybrid search with the access filter
applied INSIDE the query + semantic reranking (Azure). Foundry IQ knowledge-base retrieval is phase 2."""

from __future__ import annotations

import time
from typing import Any

from rag_os.application.ports import EmbeddingProvider, RetrievalResult, Retriever, SearchIndex, SearchRequest
from rag_os.application.services.index_schema import BASE_SELECT
from rag_os.infrastructure.registry import RETRIEVERS


@RETRIEVERS.register("direct", description="Embed query + hybrid/semantic search with in-query ACL filter.")
class DirectSearchRetriever(Retriever):
    def __init__(self, index: SearchIndex, embedder: EmbeddingProvider, candidates: int = 50,
                 min_reranker_score: float = 0.0, min_score: float = 0.0, semantic: bool = True,
                 **_: Any) -> None:
        self.index = index
        self.embedder = embedder
        self.candidates = candidates
        self.min_reranker = min_reranker_score
        self.min_score = min_score
        self.semantic = semantic

    async def retrieve(
        self, *, query: str, keyword_query: str, odata_filter: str | None, top: int
    ) -> RetrievalResult:
        t0 = time.perf_counter()
        vector, usage = await self.embedder.embed_query(query)
        t1 = time.perf_counter()
        hits = await self.index.search(SearchRequest(
            text=keyword_query, vector=vector, odata_filter=odata_filter, top=top, candidates=self.candidates,
            semantic=self.semantic, select=BASE_SELECT,
        ))
        t2 = time.perf_counter()
        if self.min_reranker > 0:
            hits = [h for h in hits if h.reranker_score is None or h.reranker_score >= self.min_reranker]
        # Raw hybrid score. On Azure this is an RRF score (~0.01-0.03), NOT a similarity - see Deployment.md.
        if self.min_score > 0:
            hits = [h for h in hits if h.score >= self.min_score]
        return RetrievalResult(
            hits=hits, usage=usage,
            timings_ms={"embed_query": (t1 - t0) * 1000, "search": (t2 - t1) * 1000},
        )
