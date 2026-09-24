"""Automatic facet classification.

``embedding``      - zero token cost: cosine similarity between the document's embedding (self-hosted model)
                     and each facet value's prototype = embedding of "label: description; synonyms". Values whose
                     top-1 vs top-2 margin is small (ambiguous) are flagged for SME review.
``embedding+llm``  - same, but ambiguous facets are escalated to the utility LLM, within a token budget.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import numpy as np

from rag_os.application.ports import ClassificationResult, Classifier, EmbeddingProvider, LlmMessage, LlmProvider
from rag_os.domain.answers import TokenUsage
from rag_os.domain.classification import FacetDef, FacetSchema
from rag_os.infrastructure.registry import CLASSIFIERS

log = logging.getLogger(__name__)


def _prototype_text(fd: FacetDef, value_id: str) -> str:
    v = next(x for x in fd.values if x.id == value_id)
    parts = [f"{fd.label or fd.name}: {v.label or v.id}"]
    if v.description:
        parts.append(v.description)
    if v.synonyms:
        parts.append("Also known as: " + ", ".join(v.synonyms))
    return ". ".join(parts)


@CLASSIFIERS.register("embedding", description="Prototype-similarity classifier using the self-hosted embedder.")
class EmbeddingClassifier(Classifier):
    def __init__(self, embedder: EmbeddingProvider, min_score: float = 0.30, margin: float = 0.03, **_: Any):
        self.embedder = embedder
        self.min_score = min_score
        self.margin = margin
        self._protos: dict[str, tuple[list[str], np.ndarray]] = {}
        self._schema_version: int | None = None

    async def _prototypes(self, schema: FacetSchema, facet: str) -> tuple[list[str], np.ndarray] | None:
        if self._schema_version != schema.version:
            self._protos.clear()
            self._schema_version = schema.version
        if facet not in self._protos:
            fd = schema.get(facet)
            if fd is None or not fd.values:
                return None
            ids = [v.id for v in fd.values]
            vecs, _ = await self.embedder.embed_documents([_prototype_text(fd, i) for i in ids])
            m = np.asarray(vecs, dtype=np.float32)
            m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
            self._protos[facet] = (ids, m)
        return self._protos[facet]

    async def classify(
        self, *, facets_to_fill: Sequence[str], text_sample: str, vector: list[float] | None, schema: FacetSchema
    ) -> ClassificationResult:
        usage = TokenUsage()
        if vector is None:
            vecs, usage = await self.embedder.embed_documents([text_sample[:4000]])
            vector = vecs[0]
        q = np.asarray(vector, dtype=np.float32)
        q /= np.linalg.norm(q) + 1e-9
        facets: dict[str, list[str]] = {}
        conf: dict[str, float] = {}
        margins: dict[str, float] = {}
        needs_review = False
        for name in facets_to_fill:
            protos = await self._prototypes(schema, name)
            if protos is None:
                continue
            ids, m = protos
            sims = m @ q
            order = np.argsort(-sims)
            top = float(sims[order[0]])
            second = float(sims[order[1]]) if len(order) > 1 else -1.0
            conf[name] = round(top, 4)
            margins[name] = round(top - second, 4)
            if top >= self.min_score:
                facets[name] = [ids[int(order[0])]]
            if top < self.min_score or (top - second) < self.margin:
                needs_review = True
        return ClassificationResult(facets=facets, confidence=conf, margin=margins, method="embedding",
                                    needs_review=needs_review, usage=usage)


@CLASSIFIERS.register("embedding+llm", description="Embedding classifier; ambiguous facets escalated to the LLM.")
class HybridClassifier(Classifier):
    def __init__(self, embedder: EmbeddingProvider, llm: LlmProvider, min_score: float = 0.30, margin: float = 0.03,
                 token_budget: int = 200_000, **_: Any) -> None:
        self.base = EmbeddingClassifier(embedder, min_score, margin)
        self.llm = llm
        self.margin = margin
        self.budget_left = token_budget

    async def classify(
        self, *, facets_to_fill: Sequence[str], text_sample: str, vector: list[float] | None, schema: FacetSchema
    ) -> ClassificationResult:
        res = await self.base.classify(facets_to_fill=facets_to_fill, text_sample=text_sample, vector=vector,
                                       schema=schema)
        ambiguous = [f for f in facets_to_fill if res.margin.get(f, 0.0) < self.margin or f not in res.facets]
        if not ambiguous or self.budget_left <= 0:
            return res
        options = {
            f: [{"id": v.id, "label": v.label, "description": v.description} for v in schema.get(f).values]  # type: ignore[union-attr]
            for f in ambiguous
            if schema.get(f)
        }
        system = ("You classify enterprise documents into a controlled vocabulary. Choose exactly one id per facet "
                  "from the options, or null if none fits. Reply as JSON: {\"facets\": {name: id|null}, "
                  "\"confidence\": {name: 0..1}}.")
        prompt = f"Options:\n{json.dumps(options)}\n\nDocument excerpt:\n{text_sample[:6000]}"
        out = await self.llm.complete(system=system, messages=[LlmMessage("user", prompt)], max_tokens=800,
                                      purpose="classify", json_output=True)
        self.budget_left -= out.usage.total
        res.usage.add(out.usage, "classify")
        try:
            data = json.loads(out.text)
            for f, v in (data.get("facets") or {}).items():
                fd = schema.get(f)
                if fd and v and fd.normalise(str(v)):
                    res.facets[f] = [fd.normalise(str(v))]  # type: ignore[list-item]
                    res.confidence[f] = float((data.get("confidence") or {}).get(f, 0.5))
            res.method = "embedding+llm"
            res.needs_review = any(res.confidence.get(f, 0) < 0.6 for f in ambiguous)
        except (ValueError, TypeError, AttributeError):
            log.warning("llm classifier returned invalid JSON")
        return res


@CLASSIFIERS.register("none", description="Disable automatic classification.")
class NoClassifier(Classifier):
    def __init__(self, **_: Any) -> None:
        pass

    async def classify(
        self, *, facets_to_fill: Sequence[str], text_sample: str, vector: list[float] | None, schema: FacetSchema
    ) -> ClassificationResult:
        return ClassificationResult(facets={}, confidence={}, margin={}, method="none", needs_review=False)
