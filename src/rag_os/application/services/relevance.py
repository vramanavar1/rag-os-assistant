"""The relevance bar: which search hits are good enough to answer from.

One function, used by the retriever and by the near-miss probe. The probe's whole claim - "these documents
would have been found if access allowed" - holds only if it applies exactly the bar the caller's own search
did, so the two must not each carry a copy.
"""

from __future__ import annotations

from rag_os.domain.answers import SearchHit


def apply_relevance_bar(hits: list[SearchHit], min_reranker: float,
                        min_score: float) -> tuple[list[SearchHit], list[SearchHit]]:
    """(kept, dropped), both in ranked order."""
    kept: list[SearchHit] = []
    dropped: list[SearchHit] = []
    for h in hits:
        ok = True
        # A hit without a reranker score (no semantic ranking on this query or backend) is not judged by it.
        if min_reranker > 0 and h.reranker_score is not None and h.reranker_score < min_reranker:
            ok = False
        # Raw hybrid score. On Azure this is an RRF score (~0.01-0.03), NOT a similarity - see Deployment.md.
        if min_score > 0 and h.score < min_score:
            ok = False
        (kept if ok else dropped).append(h)
    return kept, dropped
