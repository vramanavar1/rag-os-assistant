"""Local search backends.

* ``in_memory`` - process-local dict (unit tests).
* ``local``     - chunks stored in the state database (SQLite/PostgreSQL) so the API and worker processes
                  share one index in docker-compose. Brute-force scoring; intended for dev/test scale only.

Both evaluate the exact OData filter string sent to Azure AI Search (pre-filter, never post-filter).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import JSON, Boolean, Column, Integer, MetaData, String, Table, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from rag_os.application.ports import IndexSchema, SearchIndex, SearchRequest
from rag_os.application.services.index_schema import BASE_SELECT
from rag_os.domain.answers import SearchHit
from rag_os.domain.errors import ProfileMismatch
from rag_os.infrastructure.registry import SEARCH_INDEXES
from rag_os.infrastructure.search.local_scoring import facet_counts, hybrid_rank
from rag_os.infrastructure.search.odata_eval import compile_filter
from rag_os.infrastructure.state.db import make_engine


def _hit(doc: dict[str, Any], score: float, select: list[str] | None = None) -> SearchHit:
    extra = [f for f in (select or []) if f not in BASE_SELECT]
    return SearchHit(
        chunk_id=doc["chunk_id"],
        doc_id=doc["doc_id"],
        title=doc.get("title") or "",
        heading=doc.get("heading") or "",
        content=doc.get("content") or "",
        path=doc.get("path") or "",
        page=doc.get("page"),
        source_id=doc.get("source_id") or "",
        effective_date=doc.get("effective_date"),
        score=score,
        reranker_score=None,
        facets={},
        fields={f: doc.get(f) for f in extra},
    )


def _check_schema(existing: dict[str, str] | None, schema: IndexSchema) -> dict[str, str]:
    wanted = {f.name: f"{f.type}:{f.dimensions or ''}" for f in schema.fields}
    if existing:
        for name, typ in existing.items():
            if name in wanted and wanted[name] != typ:
                if name == "vector":
                    raise ProfileMismatch(
                        f"index '{schema.name}' vector field is {typ}, profile wants {wanted[name]}"
                    )
                raise ProfileMismatch(f"index '{schema.name}' field {name} type changed {typ} -> {wanted[name]}")
        return {**existing, **wanted}
    return wanted


@SEARCH_INDEXES.register("in_memory", description="Process-local index for unit tests.")
class InMemorySearchIndex(SearchIndex):
    def __init__(self, index_name: str, **_: Any) -> None:
        self.index_name = index_name
        self.docs: dict[str, dict[str, Any]] = {}
        self.fields: dict[str, str] | None = None
        self.profile: dict[str, Any] | None = None

    async def ensure_index(self, schema: IndexSchema) -> None:
        self.fields = _check_schema(self.fields, schema)

    async def index_exists(self) -> bool:
        return self.fields is not None

    async def read_profile(self) -> dict[str, Any] | None:
        return self.profile

    async def write_profile(self, profile: dict[str, Any]) -> None:
        self.profile = dict(profile)

    async def upsert(self, documents: Sequence[dict[str, Any]]) -> None:
        for d in documents:
            self.docs[d["chunk_id"]] = dict(d)

    async def merge(self, documents: Sequence[dict[str, Any]]) -> None:
        for d in documents:
            if d["chunk_id"] in self.docs:
                self.docs[d["chunk_id"]].update(d)

    async def delete_doc_versions(self, doc_id: str, keep_version: str | None) -> int:
        victims = [k for k, d in self.docs.items() if d["doc_id"] == doc_id and d.get("doc_version") != keep_version]
        for k in victims:
            del self.docs[k]
        return len(victims)

    async def clear(self, schema: IndexSchema, profile: dict[str, Any] | None) -> int:
        n = len(self.docs)
        self.docs.clear()
        self.fields = _check_schema(None, schema)
        if profile is not None:
            self.profile = dict(profile)
        return n

    def _filtered(self, odata: str | None) -> list[dict[str, Any]]:
        pred = compile_filter(odata)
        return [d for d in self.docs.values() if pred(d)]

    async def search(self, request: SearchRequest) -> list[SearchHit]:
        cands = self._filtered(request.odata_filter)
        ranked = hybrid_rank(cands, [c.get("vector") for c in cands], request.text, request.vector, request.top)
        return [_hit(cands[i], s, request.select) for i, s in ranked]

    async def facets(self, odata_filter: str | None, fields: Sequence[str]) -> dict[str, dict[str, int]]:
        return facet_counts(self._filtered(odata_filter), fields)

    async def count(self) -> int:
        return len(self.docs)


_meta = MetaData()
_chunks = Table(
    "search_chunks",
    _meta,
    Column("index_name", String(128), primary_key=True),
    Column("chunk_id", String(64), primary_key=True),
    Column("doc_id", String(64), index=True, nullable=False),
    Column("doc_version", String(64), nullable=False),
    Column("is_current", Boolean, nullable=False, default=True),
    Column("payload", JSON, nullable=False),
)
_index_meta = Table(
    "search_indexes",
    _meta,
    Column("index_name", String(128), primary_key=True),
    Column("fields", JSON, nullable=True),
    Column("profile", JSON, nullable=True),
    Column("generation", Integer, nullable=False, default=0),
)


@SEARCH_INDEXES.register("local", description="Index stored in the state DB; shared by API + worker (dev).")
class SqlLocalSearchIndex(SearchIndex):
    def __init__(self, index_name: str, db_url: str, entra_auth: bool = False, **_: Any) -> None:
        self.index_name = index_name
        self.engine = make_engine(db_url, entra_auth=entra_auth)
        _meta.create_all(self.engine)
        self._cache_gen = -1
        self._cache: list[dict[str, Any]] = []

    def _insert(self):  # type: ignore[no-untyped-def]
        return pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert

    def _meta_row(self) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = c.execute(select(_index_meta).where(_index_meta.c.index_name == self.index_name)).mappings().first()
            return dict(row) if row else None

    def _bump(self, conn: Any) -> None:
        conn.execute(
            update(_index_meta)
            .where(_index_meta.c.index_name == self.index_name)
            .values(generation=_index_meta.c.generation + 1)
        )

    async def ensure_index(self, schema: IndexSchema) -> None:
        def _do() -> None:
            row = self._meta_row()
            fields = _check_schema(row["fields"] if row else None, schema)
            ins = self._insert()(_index_meta).values(index_name=self.index_name, fields=fields, generation=0)
            with self.engine.begin() as c:
                c.execute(ins.on_conflict_do_update(index_elements=["index_name"], set_={"fields": fields}))

        await asyncio.to_thread(_do)

    async def index_exists(self) -> bool:
        return await asyncio.to_thread(self._meta_row) is not None

    async def read_profile(self) -> dict[str, Any] | None:
        row = await asyncio.to_thread(self._meta_row)
        return row["profile"] if row and row["profile"] else None

    async def write_profile(self, profile: dict[str, Any]) -> None:
        def _do() -> None:
            with self.engine.begin() as c:
                c.execute(
                    update(_index_meta).where(_index_meta.c.index_name == self.index_name).values(profile=profile)
                )

        await asyncio.to_thread(_do)

    async def clear(self, schema: IndexSchema, profile: dict[str, Any] | None) -> int:
        """Delete this index's chunks; the schema row and profile stay. Bumping the generation drops read caches."""
        await self.ensure_index(schema)

        def _do() -> int:
            with self.engine.begin() as c:
                n = int(c.execute(delete(_chunks).where(_chunks.c.index_name == self.index_name)).rowcount or 0)
                self._bump(c)
                if profile is not None:
                    c.execute(update(_index_meta).where(_index_meta.c.index_name == self.index_name)
                              .values(profile=profile))
                return n

        return await asyncio.to_thread(_do)

    async def upsert(self, documents: Sequence[dict[str, Any]]) -> None:
        if not documents:
            return

        def _do() -> None:
            rows = [
                {
                    "index_name": self.index_name,
                    "chunk_id": d["chunk_id"],
                    "doc_id": d["doc_id"],
                    "doc_version": d.get("doc_version") or "",
                    "is_current": bool(d.get("is_current", True)),
                    "payload": json.loads(json.dumps(d)),
                }
                for d in documents
            ]
            ins = self._insert()(_chunks)
            stmt = ins.on_conflict_do_update(
                index_elements=["index_name", "chunk_id"],
                set_={k: ins.excluded[k] for k in ("doc_id", "doc_version", "is_current", "payload")},
            )
            with self.engine.begin() as c:
                c.execute(stmt, rows)
                self._bump(c)

        await asyncio.to_thread(_do)

    async def merge(self, documents: Sequence[dict[str, Any]]) -> None:
        if not documents:
            return

        def _do() -> None:
            with self.engine.begin() as c:
                for d in documents:
                    row = c.execute(select(_chunks.c.payload).where(
                        _chunks.c.index_name == self.index_name, _chunks.c.chunk_id == d["chunk_id"])).first()
                    if row is None:
                        continue
                    payload = {**row[0], **json.loads(json.dumps(d))}
                    c.execute(update(_chunks).where(
                        _chunks.c.index_name == self.index_name, _chunks.c.chunk_id == d["chunk_id"]
                    ).values(payload=payload))
                self._bump(c)

        await asyncio.to_thread(_do)

    async def delete_doc_versions(self, doc_id: str, keep_version: str | None) -> int:
        def _do() -> int:
            cond = (_chunks.c.index_name == self.index_name) & (_chunks.c.doc_id == doc_id)
            if keep_version is not None:
                cond = cond & (_chunks.c.doc_version != keep_version)
            with self.engine.begin() as c:
                n = c.execute(delete(_chunks).where(cond)).rowcount or 0
                self._bump(c)
                return int(n)

        return await asyncio.to_thread(_do)

    def _load(self) -> list[dict[str, Any]]:
        row = self._meta_row()
        gen = int(row["generation"]) if row else 0
        if gen != self._cache_gen:
            with self.engine.connect() as c:
                res = c.execute(select(_chunks.c.payload).where(_chunks.c.index_name == self.index_name))
                self._cache = [r[0] for r in res]
            self._cache_gen = gen
        return self._cache

    async def search(self, request: SearchRequest) -> list[SearchHit]:
        def _do() -> list[SearchHit]:
            pred = compile_filter(request.odata_filter)
            cands = [d for d in self._load() if pred(d)]
            ranked = hybrid_rank(cands, [c.get("vector") for c in cands], request.text, request.vector, request.top)
            return [_hit(cands[i], s, request.select) for i, s in ranked]

        return await asyncio.to_thread(_do)

    async def facets(self, odata_filter: str | None, fields: Sequence[str]) -> dict[str, dict[str, int]]:
        def _do() -> dict[str, dict[str, int]]:
            pred = compile_filter(odata_filter)
            return facet_counts([d for d in self._load() if pred(d)], fields)

        return await asyncio.to_thread(_do)

    async def count(self) -> int:
        return len(await asyncio.to_thread(self._load))
