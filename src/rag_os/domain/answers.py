"""Query/answer value objects and token accounting."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    """Normalised token usage. `input` = uncached input tokens across providers."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    embedding: int = 0
    calls: int = 0
    by_purpose: dict[str, dict[str, int]] = Field(default_factory=dict)

    def add(self, other: TokenUsage, purpose: str | None = None) -> TokenUsage:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.embedding += other.embedding
        self.calls += other.calls
        if purpose:
            bucket = self.by_purpose.setdefault(purpose, {"input": 0, "output": 0, "cache_read": 0,
                                                          "cache_write": 0, "embedding": 0})
            bucket["input"] += other.input
            bucket["output"] += other.output
            bucket["cache_read"] += other.cache_read
            bucket["cache_write"] += other.cache_write
            bucket["embedding"] += other.embedding
        for p, b in other.by_purpose.items():
            tgt = self.by_purpose.setdefault(p, {k: 0 for k in b})
            for k, v in b.items():
                tgt[k] = tgt.get(k, 0) + v
        return self

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


class Citation(BaseModel):
    index: int
    doc_id: str
    chunk_id: str
    title: str
    path: str
    page: int | None = None
    heading: str = ""
    score: float | None = None
    snippet: str = ""
    also_at: list[str] = Field(default_factory=list)
    """Other paths holding this same passage, all of them ones the caller may already read."""


class SearchHit(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    heading: str
    content: str
    path: str
    page: int | None
    source_id: str
    # When the document states one. Shown to the model so it can prefer the newer of two that disagree.
    effective_date: str | None = None
    score: float
    reranker_score: float | None = None
    facets: dict[str, list[str]] = Field(default_factory=dict)
    # Index fields selected beyond the base set (access tags, facet fields), keyed by index field name. Only the
    # near-miss probe asks for them; an answer's own retrieval never selects access tags.
    fields: dict[str, Any] = Field(default_factory=dict)


class Answer(BaseModel):
    answer: str
    refused: bool = False
    refusal_reason: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    provider: str = ""
    model: str = ""
    timings_ms: dict[str, float] = Field(default_factory=dict)
    correlation_id: str | None = None


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    content: str
