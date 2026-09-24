"""Hybrid scoring for local backends: BM25 over text fields + cosine over vectors, fused with RRF
(the same reciprocal-rank-fusion approach Azure AI Search uses for hybrid queries)."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

import numpy as np

_WORD = re.compile(r"[\w]+", re.UNICODE)
_STOP = frozenset(
    "a an the and or of to in on for is are was were be by with as at from this that it what which who how do "
    "does did can could should would i you we they our your".split()
)
_RRF_K = 60


def tokens(text: str) -> list[str]:
    return [t for t in (w.lower() for w in _WORD.findall(text or "")) if len(t) > 1 and t not in _STOP]


def _bm25(query: list[str], docs: Sequence[list[str]], k1: float = 1.2, b: float = 0.75) -> list[float]:
    if not query or not docs:
        return [0.0] * len(docs)
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df: Counter[str] = Counter()
    for d in docs:
        df.update(set(d))
    scores = []
    q = set(query)
    for d in docs:
        tf = Counter(d)
        dl = len(d) or 1
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


def hybrid_rank(
    candidates: Sequence[dict[str, Any]],
    vectors: Sequence[list[float] | None],
    text: str | None,
    vector: list[float] | None,
    top: int,
) -> list[tuple[int, float]]:
    """Return [(candidate_index, fused_score)] best first."""
    n = len(candidates)
    if n == 0:
        return []
    fused = [0.0] * n
    if text and text.strip() and text.strip() != "*":
        q = tokens(text)
        docs = [tokens(" ".join(str(c.get(f) or "") for f in ("title", "heading", "content", "path"))) for c in candidates]
        kw = _bm25(q, docs)
        order = sorted((i for i in range(n) if kw[i] > 0), key=lambda i: -kw[i])
        for rank, i in enumerate(order):
            fused[i] += 1.0 / (_RRF_K + rank + 1)
    if vector is not None:
        qv = np.asarray(vector, dtype=np.float32)
        qn = float(np.linalg.norm(qv)) or 1.0
        sims = np.full(n, -2.0, dtype=np.float32)
        for i, v in enumerate(vectors):
            if v is None:
                continue
            dv = np.asarray(v, dtype=np.float32)
            dn = float(np.linalg.norm(dv)) or 1.0
            sims[i] = float(qv @ dv) / (qn * dn)
        order = [int(i) for i in np.argsort(-sims) if sims[int(i)] > -2.0]
        for rank, i in enumerate(order):
            fused[i] += 1.0 / (_RRF_K + rank + 1)
    ranked = sorted((i for i in range(n) if fused[i] > 0), key=lambda i: -fused[i])
    return [(i, fused[i]) for i in ranked[:top]]


def facet_counts(docs: Sequence[dict[str, Any]], fields: Sequence[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {f: {} for f in fields}
    for d in docs:
        for f in fields:
            vals = d.get(f)
            if vals is None:
                continue
            for v in vals if isinstance(vals, list) else [vals]:
                out[f][str(v)] = out[f].get(str(v), 0) + 1
    return out
