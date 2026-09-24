"""ProfileGuard: refuse to ingest or query when the embedding model, its revision/dimensions and the
index's recorded profile disagree. There is deliberately NO fallback embedder."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from rag_os.application.ports import EmbeddingProvider, SearchIndex
from rag_os.domain.embedding import EmbedderInfo, EmbeddingProfile
from rag_os.domain.errors import ProfileMismatch

log = logging.getLogger(__name__)


def _model_matches(reported: str, expected: str) -> bool:
    """Exact id match, or same model name when a server reports a cache/snapshot path."""
    if not reported:
        return False
    r, e = reported.strip().lower().rstrip("/"), expected.strip().lower()
    return r == e or r.split("/")[-1] == e.split("/")[-1]


@dataclass
class GuardStatus:
    ok: bool
    checked_at: float
    reasons: list[str] = field(default_factory=list)
    embedder: dict[str, object] = field(default_factory=dict)
    index_profile_fp: str | None = None


class ProfileGuard:
    def __init__(self, profile: EmbeddingProfile, recheck_seconds: int = 60) -> None:
        self.profile = profile
        self.fp = profile.fingerprint()
        self.recheck_seconds = recheck_seconds
        self._status: GuardStatus | None = None

    def profile_record(self) -> dict[str, object]:
        return {"fingerprint": self.fp, **self.profile.model_dump()}

    def _compare_embedder(self, info: EmbedderInfo, pool: str) -> list[str]:
        p = self.profile
        reasons: list[str] = []
        if p.provider == "fake":
            return reasons
        if not _model_matches(info.model, p.model):
            reasons.append(f"{pool}: model '{info.model}' != profile '{p.model}'")
        if p.model_revision and info.revision and info.revision != p.model_revision:
            reasons.append(f"{pool}: revision '{info.revision}' != profile '{p.model_revision}'")
        expected_native = p.native_dimensions or p.dimensions
        if info.dimensions != expected_native:
            reasons.append(f"{pool}: dimensions {info.dimensions} != expected {expected_native}")
        return reasons

    async def check(
        self, index: SearchIndex, embedders: dict[str, EmbeddingProvider], *, force: bool = False
    ) -> GuardStatus:
        now = time.monotonic()
        if not force and self._status and now - self._status.checked_at < self.recheck_seconds:
            return self._status
        reasons: list[str] = []
        emb_info: dict[str, object] = {}
        for pool, emb in embedders.items():
            try:
                info = await emb.info()
                emb_info[pool] = info.model_dump()
                reasons += self._compare_embedder(info, pool)
            except Exception as e:
                reasons.append(f"{pool}: embedder unavailable ({type(e).__name__}: {e})")
        stored = await index.read_profile()
        stored_fp = str(stored.get("fingerprint")) if stored else None
        if stored is None:
            reasons.append("index has no recorded embedding profile (run `rag-os bootstrap`)")
        elif stored_fp != self.fp:
            reasons.append(f"index profile {stored_fp} != configured profile {self.fp}")
        self._status = GuardStatus(ok=not reasons, checked_at=now, reasons=reasons, embedder=emb_info,
                                   index_profile_fp=stored_fp)
        if reasons:
            log.error("embedding profile guard failed", extra={"reasons": reasons})
        return self._status

    async def require(self, index: SearchIndex, embedders: dict[str, EmbeddingProvider]) -> None:
        st = await self.check(index, embedders)
        if not st.ok:
            raise ProfileMismatch("embedding profile mismatch", detail={"reasons": st.reasons})
