"""Embedding profile: the contract that ingestion and query MUST share."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ChunkingConfig(BaseModel):
    chunker_version: str = "1"
    chunk_tokens: int = 450
    overlap_tokens: int = 60
    max_chunk_tokens: int = 1200  # hard cap (tables may exceed chunk_tokens up to this)


class EmbeddingProfile(BaseModel):
    """Everything that changes the vector space. Change any field -> new fingerprint -> new index."""

    name: str
    provider: Literal["tei", "azure_openai", "fake"]
    model: str
    model_revision: str = ""
    server_image: str = ""  # image digest for self-hosted servers (tei)
    dimensions: int
    native_dimensions: int | None = None  # set when truncating an MRL model to fewer dims
    normalize: bool = True
    query_prefix: str = ""
    document_prefix: str = ""
    max_input_tokens: int = 8192
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)

    @model_validator(mode="after")
    def _check_prefix_direction(self) -> EmbeddingProfile:
        """Catch a query/document prefix that has been filled in backwards.

        Instruction-tuned embedders are asymmetric: the *query* carries the instruction and the document carries
        nothing (Qwen3) or a short marker (E5-style "passage:"). A profile that prefixes documents but not
        queries is the signature of the two being swapped, and is almost never deliberate.

        This catches the one-sided case only. A full swap where BOTH prefixes are non-empty is undetectable
        here - and undetectable at runtime too, because re-ingesting under swapped prefixes is internally
        consistent: same fingerprint, same index, quietly worse recall. `scripts/smoke.py` is the only thing
        that would notice, because it asserts on what a real question actually retrieves.
        """
        if self.document_prefix and not self.query_prefix:
            raise ValueError(
                f"embedding profile '{self.name}': document_prefix is set but query_prefix is empty. "
                "Asymmetric embedders instruct the QUERY, not the document - are these the wrong way round? "
                'Set query_prefix, or clear document_prefix if the model really is symmetric.'
            )
        return self

    def fingerprint(self) -> str:
        payload = self.model_dump(exclude={"name"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]

    def index_name(self, prefix: str, domain: str) -> str:
        safe_domain = "".join(c for c in domain.lower() if c.isalnum() or c == "-")[:40] or "default"
        return f"{prefix}-{safe_domain}-{self.fingerprint()}"


class EmbedderInfo(BaseModel):
    """What the running embedding server reports about itself (checked against the profile)."""

    model: str
    revision: str = ""
    dimensions: int
    max_input_tokens: int | None = None
