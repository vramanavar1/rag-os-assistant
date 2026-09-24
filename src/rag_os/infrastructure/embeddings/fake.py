"""Deterministic fake embedder - FOR AUTOMATED TESTS ONLY (never used by a deployed profile).

Vectors are feature-hashed bag-of-words so similar texts are near each other, making retrieval tests
meaningful without a model server.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any

from rag_os.application.ports import EmbeddingProvider
from rag_os.domain.answers import TokenUsage
from rag_os.domain.embedding import EmbedderInfo, EmbeddingProfile
from rag_os.infrastructure.registry import EMBEDDERS

_W = re.compile(r"\w+")


def _vec(text: str, dims: int) -> list[float]:
    v = [0.0] * dims
    for w in _W.findall(text.lower()):
        h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "big")
        v[h % dims] += 1.0 if (h >> 63) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@EMBEDDERS.register("fake", description="Deterministic hashing embedder for tests only.")
class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, profile: EmbeddingProfile, **_: Any) -> None:
        super().__init__(profile)
        self.calls = 0

    async def info(self) -> EmbedderInfo:
        return EmbedderInfo(model=self.profile.model, revision=self.profile.model_revision,
                            dimensions=self.profile.dimensions)

    async def embed_documents(self, texts: Sequence[str]) -> tuple[list[list[float]], TokenUsage]:
        self.calls += 1
        return [_vec(self.profile.document_prefix + t, self.profile.dimensions) for t in texts], TokenUsage(
            embedding=sum(len(t.split()) for t in texts), calls=1
        )

    async def embed_query(self, text: str) -> tuple[list[float], TokenUsage]:
        self.calls += 1
        # query prefix intentionally NOT hashed in so fake queries match fake documents
        return _vec(text, self.profile.dimensions), TokenUsage(embedding=len(text.split()), calls=1)
