"""ProfileGuard: refuse to ingest or query when the embedding model, its revision/dimensions and the
index's recorded profile disagree. There is deliberately NO fallback embedder."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

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
    embedder: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Things worth saying that are NOT failures. The ingestion pool scales to zero when idle, so "not running"
    # is its normal resting state; folding that into `ok` would put readyz at 503 on a healthy system and the
    # signal would be worthless within a day.
    notes: list[str] = field(default_factory=list)
    index_profile_fp: str | None = None
    # Search itself could not answer. Distinct from "no profile recorded", which also leaves the fingerprint
    # None but means the index is simply not bootstrapped yet.
    index_unreadable: bool = False


class ProfileGuard:
    def __init__(self, profile: EmbeddingProfile, recheck_seconds: int = 60) -> None:
        self.profile = profile
        self.fp = profile.fingerprint()
        self.recheck_seconds = recheck_seconds
        # Keyed by which pools were asked about, and how. One process asks different questions - the chat path
        # asks about the query pool, readyz asks about both - and a verdict reached for one question is not an
        # answer to the other. A single slot silently served whichever was cached first.
        self._cache: dict[tuple[frozenset[str], frozenset[str]], GuardStatus] = {}

    def invalidate(self) -> None:
        """Drop every cached verdict so the next check re-probes."""
        self._cache.clear()

    def observed(self, pool: str) -> dict[str, Any] | None:
        """What `pool` last reported about itself, or None if it has not been asked.

        The reported model, not the configured one - which is the whole point. Everything else in the system
        records what was configured, so this is the only thing that can contradict it.
        """
        for status in self._cache.values():
            if pool in status.embedder:
                return dict(status.embedder[pool])
        return None

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

    async def _read_index_profile(self, index: SearchIndex) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """(stored profile, its fingerprint, reason it could not be read).

        This call used to be unguarded. A Search failure that is not "index not found" - an expired role
        assignment, a network error, a throttle - therefore escaped check() entirely, and /api/readyz reported it
        from its own outer handler: the caller lost `fingerprint` and `index` from the body and got a bare
        exception name with no indication that Search was the thing at fault.

        The message is deliberately left out of the reason. /api/readyz is reachable unauthenticated through the
        chat UI, and an Azure error string carries endpoints and identity details; the full exception goes to the
        log, which is not public.
        """
        try:
            stored = await index.read_profile()
        except Exception as e:
            log.error("could not read the index embedding profile", exc_info=True,
                      extra={"index": getattr(index, "index_name", None)})
            return None, None, f"index profile unreadable ({type(e).__name__})"
        return stored, (str(stored.get("fingerprint")) if stored else None), None

    async def _index_exists(self, index: SearchIndex) -> bool:
        """Never let the follow-up question turn a reportable state into an exception."""
        try:
            return await index.index_exists()
        except Exception:
            log.warning("could not determine whether the index exists", exc_info=True)
            return False

    async def check(
        self, index: SearchIndex, embedders: dict[str, EmbeddingProvider], *, force: bool = False,
        advisory_pools: frozenset[str] = frozenset(),
    ) -> GuardStatus:
        """Compare every supplied pool, and the index's recorded profile, against the configured one.

        `advisory_pools` names pools that are allowed to be absent. A pool listed there which cannot be reached
        produces a NOTE instead of a failure - `rag-embed-ingest` scales to zero when idle, so "not running" is
        its normal state and treating it as broken would make this verdict useless.

        A pool that IS reachable is always compared in full, advisory or not. Serving the wrong model is a
        failure wherever it happens: those vectors and the query vectors would occupy different spaces, and the
        answers would be confidently wrong rather than merely missing.
        """
        now = time.monotonic()
        key = (frozenset(embedders), frozenset(advisory_pools))
        cached = self._cache.get(key)
        if not force and cached and now - cached.checked_at < self.recheck_seconds:
            return cached
        reasons: list[str] = []
        notes: list[str] = []
        emb_info: dict[str, dict[str, Any]] = {}
        for pool, emb in embedders.items():
            try:
                info = await emb.info()
                emb_info[pool] = info.model_dump()
                reasons += self._compare_embedder(info, pool)
            except Exception as e:
                # Type, not message. These strings reach /api/readyz, which is served unauthenticated through
                # the chat UI, and an adapter's error text carries endpoints. The full exception goes to the
                # log and to `rag-os doctor`, neither of which is public.
                log.warning("embedding pool unreachable", exc_info=True, extra={"pool": pool})
                unavailable = f"{pool}: embedder unavailable ({type(e).__name__})"
                if pool in advisory_pools:
                    notes.append(f"{unavailable} - expected when it has scaled to zero")
                else:
                    reasons.append(unavailable)
        stored, stored_fp, unreadable = await self._read_index_profile(index)
        if unreadable:
            reasons.append(unreadable)
        elif stored is None:
            # read_profile() returns None for two different situations with two different remedies, so ask which
            # one it is rather than reporting a sentence that fits both.
            name = getattr(index, "index_name", "the index")
            if await self._index_exists(index):
                reasons.append(f"index '{name}' exists but has no recorded embedding profile "
                               "(run `rag-os bootstrap`)")
            else:
                reasons.append(f"index '{name}' does not exist (run `rag-os bootstrap`)")
        elif stored_fp != self.fp:
            reasons.append(f"index profile {stored_fp} != configured profile {self.fp}")
        status = GuardStatus(ok=not reasons, checked_at=now, reasons=reasons, notes=notes, embedder=emb_info,
                             index_profile_fp=stored_fp, index_unreadable=bool(unreadable))
        self._cache[key] = status
        if reasons:
            log.error("embedding profile guard failed", extra={"reasons": reasons, "notes": notes})
        elif notes:
            log.info("embedding profile ok, with notes", extra={"notes": notes})
        return status

    async def refusal_reason(self, index: SearchIndex, embedders: dict[str, EmbeddingProvider]) -> str | None:
        """None when queries may proceed, else the reason code to refuse a query with.

        Classified from index_profile_fp rather than by matching reason strings:

          unreadable  - Search could not be asked at all, so nothing is known about the index. Refusing as
                        'index_not_ready' here would tell the user to run bootstrap when the real fault is the
                        search service.
          fp is None  - the index has no recorded profile, so nothing has ever been ingested into it. A brand-new
                        deployment whose bootstrap has not run looks exactly like this.
          fp differs  - the index holds vectors from a different embedding model. Queries must NOT be served:
                        a different model is a different vector space, so results would be confidently wrong
                        rather than merely empty.
          otherwise   - the embedder pools themselves are unreachable.

        Note an index that was bootstrapped but holds no documents is NOT any of these - the profile matches, so
        the query runs and returns no hits, which the caller already answers politely.
        """
        st = await self.check(index, embedders)
        if st.ok:
            return None
        if st.index_unreadable:
            return "search_unavailable"
        if st.index_profile_fp is None:
            return "index_not_ready"
        if st.index_profile_fp != self.fp:
            return "embedding_profile_mismatch"
        return "search_unavailable"

    async def require(self, index: SearchIndex, embedders: dict[str, EmbeddingProvider]) -> None:
        st = await self.check(index, embedders)
        if not st.ok:
            raise ProfileMismatch("embedding profile mismatch", detail={"reasons": st.reasons})
