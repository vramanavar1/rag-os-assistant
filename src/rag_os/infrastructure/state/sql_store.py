"""SQL ingestion state store (PostgreSQL in Azure, SQLite for tests).

Designed for millions of rows: batched upserts, keyset pagination, composite indexes for the report
queries, and a separate (facet, value) table so "group by department/region" never parses JSON.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    nulls_last,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Connection, RowMapping

from rag_os.application.ports import (
    DiscoveryDelta,
    DocumentEvent,
    DocumentQuery,
    IngestionStateStore,
    PurgeCandidate,
)
from rag_os.domain.documents import (
    IN_FLIGHT_STATUSES,
    DocumentRecord,
    DocumentStatus,
    ReviewStatus,
    TagSet,
    can_transition,
    tags_hash,
)
from rag_os.domain.errors import Conflict, NotFound, ValidationFailed
from rag_os.domain.ingestion import IngestionControls, IngestionRun, RunStatus
from rag_os.infrastructure.state.db import make_engine

metadata = MetaData()

documents = Table(
    "documents",
    metadata,
    Column("doc_id", String(64), primary_key=True),
    Column("source_id", String(64), nullable=False),
    Column("item_id", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("blob_uri", Text),
    Column("content_hash", String(64)),
    Column("version_key", String(80), nullable=False),
    Column("indexed_version", String(80)),
    Column("tags_hash", String(64)),
    Column("indexed_content_hash", String(64)),
    Column("indexed_tags_hash", String(64)),
    Column("status", String(24), nullable=False),
    Column("stage", String(32)),
    Column("attempts", Integer, nullable=False, default=0),
    Column("error_type", String(128)),
    Column("error_message", Text),
    Column("title", Text),
    Column("content_type", String(128)),
    Column("size", BigInteger, nullable=False, default=0),
    Column("tags", JSON, nullable=False),
    Column("review_status", String(16), nullable=False, default="NONE"),
    Column("chunk_count", Integer, nullable=False, default=0),
    Column("embedding_fp", String(16)),
    Column("run_id", String(40)),
    Column("last_seen_run_id", String(40)),
    Column("tracking_id", String(40), unique=True),
    Column("correlation_id", String(64)),
    Column("discovered_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("indexed_at", DateTime(timezone=True)),
    Index("ix_documents_source_status", "source_id", "status"),
    Index("ix_documents_status_updated", "status", "updated_at"),
    # Newest-first paging. The composite carries the doc_id tie-breaker so the keyset condition is covered
    # end to end; ix_documents_status_updated cannot serve a discovered_at sort.
    Index("ix_documents_discovered", "discovered_at", "doc_id"),
    # Content identity. Nullable and unindexed until now, the sha256 was computed on every upload and then
    # only ever compared with the same document's own previous value - so the same file uploaded twice was
    # two documents, two blobs and two sets of vectors. This index is what lets one content be found.
    Index("ix_documents_content", "content_hash"),
    Index("ix_documents_status_discovered", "status", "discovered_at"),
    Index("ix_documents_run_status", "run_id", "status"),
    Index("ix_documents_review", "review_status"),
)

document_facets = Table(
    "document_facets",
    metadata,
    Column("doc_id", String(64), primary_key=True),
    Column("facet", String(64), primary_key=True),
    Column("value", String(128), primary_key=True),
    Index("ix_document_facets_fv", "facet", "value"),
)

document_events = Table(
    "document_events",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("doc_id", String(64), nullable=False, index=True),
    Column("status", String(24), nullable=False),
    Column("stage", String(32)),
    Column("message", Text),
    Column("correlation_id", String(64)),
    Column("at", DateTime(timezone=True), nullable=False),
)

ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("run_id", String(40), primary_key=True),
    Column("source_id", String(64), nullable=False, index=True),
    Column("trigger", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("finished_at", DateTime(timezone=True)),
    Column("discovered", Integer, nullable=False, default=0),
    Column("queued", Integer, nullable=False, default=0),
    Column("unchanged", Integer, nullable=False, default=0),
    Column("deleted", Integer, nullable=False, default=0),
    Column("error", Text),
)

controls = Table(
    "ingestion_controls",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("data", JSON, nullable=False),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _carry_classifier_facets(rule_tags: TagSet, prev: TagSet) -> TagSet:
    facets = {k: list(v) for k, v in rule_tags.facets.items()}
    sources = dict(rule_tags.sources)
    for key, src in prev.sources.items():
        if key.startswith("facet:") and src.startswith("classifier:"):
            name = key[len("facet:"):]
            if name not in facets and prev.facets.get(name):
                facets[name] = list(prev.facets[name])
                sources[key] = src
                conf = f"confidence:{name}"
                if conf in prev.sources:
                    sources[conf] = prev.sources[conf]
    return TagSet(facets=facets, acl=dict(rule_tags.acl), sources=sources)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _to_record(r: RowMapping) -> DocumentRecord:
    return DocumentRecord(
        doc_id=r["doc_id"],
        source_id=r["source_id"],
        item_id=r["item_id"],
        path=r["path"],
        blob_uri=r["blob_uri"],
        content_hash=r.get("content_hash"),
        version_key=r["version_key"],
        indexed_version=r["indexed_version"],
        indexed_content_hash=r.get("indexed_content_hash"),
        indexed_tags_hash=r.get("indexed_tags_hash"),
        status=DocumentStatus(r["status"]),
        stage=r["stage"],
        attempts=r["attempts"] or 0,
        error_type=r["error_type"],
        error_message=r["error_message"],
        title=r["title"],
        content_type=r["content_type"],
        size=r["size"] or 0,
        tags=TagSet.model_validate(r["tags"] or {}),
        review_status=ReviewStatus(r["review_status"] or "NONE"),
        chunk_count=r["chunk_count"] or 0,
        embedding_fp=r["embedding_fp"],
        run_id=r["run_id"],
        last_seen_run_id=r["last_seen_run_id"],
        tracking_id=r["tracking_id"],
        correlation_id=r["correlation_id"],
        discovered_at=_aware(r["discovered_at"]),
        updated_at=_aware(r["updated_at"]),
        indexed_at=_aware(r["indexed_at"]),
    )


def _encode_cursor(rec: DocumentRecord) -> str:
    """An opaque, URL-safe page cursor over (discovered_at, doc_id).

    base64url rather than the readable "<iso>|<doc_id>": an ISO timestamp carries a '+', which a query string
    decodes as a space, so the readable form silently breaks for any caller that concatenates it into a URL
    without escaping - and a cursor is exactly the kind of value people concatenate.
    """
    ts = rec.discovered_at.isoformat() if rec.discovered_at else ""
    return base64.urlsafe_b64encode(f"{ts}|{rec.doc_id}".encode()).decode().rstrip("=")


class SqlStateStore(IngestionStateStore):
    def __init__(self, db_url: str, *, entra_auth: bool = False, create: bool = True) -> None:
        self.engine = make_engine(db_url, entra_auth=entra_auth)
        if create:
            metadata.create_all(self.engine)

    # ------------------------------------------------------------------ helpers

    def _write_facets(self, c: Connection, doc_id: str, tags: TagSet) -> None:
        c.execute(delete(document_facets).where(document_facets.c.doc_id == doc_id))
        rows = [
            {"doc_id": doc_id, "facet": f[:64], "value": v[:128]}
            for f, vals in tags.facets.items()
            for v in dict.fromkeys(vals)
        ]
        if rows:
            c.execute(insert(document_facets), rows)

    def _event(self, c: Connection, doc_id: str, status: DocumentStatus, stage: str | None = None,
               message: str | None = None, correlation_id: str | None = None) -> None:
        c.execute(insert(document_events).values(
            doc_id=doc_id, status=status.value, stage=stage, message=(message or "")[:2000] or None,
            correlation_id=correlation_id, at=_now()))

    # ------------------------------------------------------------------ discovery

    def upsert_discovered(self, records: Sequence[DocumentRecord], run_id: str,
                          embedding_fp: str | None = None) -> DiscoveryDelta:
        delta = DiscoveryDelta()
        if not records:
            return delta
        now = _now()
        ids = [r.doc_id for r in records]
        with self.engine.begin() as c:
            existing = {
                row["doc_id"]: row
                for row in c.execute(select(documents).where(documents.c.doc_id.in_(ids))).mappings()
            }
            for rec in records:
                th = tags_hash(rec.tags)
                cur = existing.get(rec.doc_id)
                base = {
                    "source_id": rec.source_id, "item_id": rec.item_id, "path": rec.path,
                    "blob_uri": rec.blob_uri, "content_type": rec.content_type, "size": rec.size,
                    "content_hash": rec.content_hash,
                    "last_seen_run_id": run_id, "updated_at": now,
                }
                if cur is None:
                    c.execute(insert(documents).values(
                        doc_id=rec.doc_id, version_key=rec.version_key, tags=rec.tags.model_dump(),
                        tags_hash=th, status=DocumentStatus.DISCOVERED.value, attempts=0, review_status="NONE",
                        chunk_count=0, run_id=run_id, tracking_id=rec.tracking_id,
                        correlation_id=rec.correlation_id, discovered_at=now, **base))
                    self._write_facets(c, rec.doc_id, rec.tags)
                    delta.full.append(rec.model_copy(update={"run_id": run_id, "status": DocumentStatus.DISCOVERED}))
                    continue
                status = DocumentStatus(cur["status"])
                same_version = cur["version_key"] == rec.version_key
                # `tags_hash` fingerprints the RULE-derived layers only (defaults/path rules/sidecar/manifest), so
                # facets filled by the classifier never look like a change. When rules do change, classifier
                # facets the rules don't set are carried over. SME-approved tags always win.
                prev = TagSet.model_validate(cur["tags"] or {})
                if cur["review_status"] == ReviewStatus.APPROVED.value:
                    merged_tags = prev
                    tags_changed = False
                else:
                    merged_tags = _carry_classifier_facets(rec.tags, prev)
                    tags_changed = (cur["tags_hash"] or "") != th
                values: dict[str, Any] = dict(base)
                # Indexed under a different embedding profile == not indexed for our purposes: those vectors
                # live in another index, in another vector space. Without this, changing the model re-queues
                # nothing and the new index stays empty.
                stale_profile = embedding_fp is not None and cur["embedding_fp"] != embedding_fp
                needs_full = ((not same_version) or status == DocumentStatus.DELETED
                              or cur["indexed_version"] is None or stale_profile)
                if same_version and status == DocumentStatus.FAILED:
                    delta.skipped_failed += 1
                elif same_version and status in IN_FLIGHT_STATUSES:
                    delta.in_flight += 1
                elif needs_full:
                    values.update(version_key=rec.version_key, status=DocumentStatus.DISCOVERED.value,
                                  run_id=run_id, attempts=0, error_type=None, error_message=None,
                                  tags=merged_tags.model_dump(), tags_hash=th)
                    delta.full.append(_to_record({**cur, **values}))
                elif tags_changed:
                    values.update(tags=merged_tags.model_dump(), tags_hash=th, run_id=run_id,
                                  status=DocumentStatus.QUEUED.value)
                    delta.retag.append(_to_record({**cur, **values}))
                else:
                    delta.unchanged += 1
                c.execute(update(documents).where(documents.c.doc_id == rec.doc_id).values(**values))
                if "tags" in values:
                    self._write_facets(c, rec.doc_id, merged_tags)
        return delta

    def update_tags(self, doc_id: str, tags: TagSet, review_status: str | None = None) -> DocumentRecord:
        with self.engine.begin() as c:
            row = c.execute(select(documents).where(documents.c.doc_id == doc_id)).mappings().first()
            if row is None:
                raise NotFound(f"document {doc_id} not found")
            values: dict[str, Any] = {"tags": tags.model_dump(), "tags_hash": tags_hash(tags), "updated_at": _now()}
            if review_status:
                values["review_status"] = review_status
            c.execute(update(documents).where(documents.c.doc_id == doc_id).values(**values))
            self._write_facets(c, doc_id, tags)
            self._event(c, doc_id, DocumentStatus(row["status"]), stage="tags", message="tags updated")
            row = c.execute(select(documents).where(documents.c.doc_id == doc_id)).mappings().first()
        assert row is not None
        return _to_record(row)

    # ------------------------------------------------------------------ reads

    def get(self, doc_id: str) -> DocumentRecord | None:
        with self.engine.connect() as c:
            row = c.execute(select(documents).where(documents.c.doc_id == doc_id)).mappings().first()
        return _to_record(row) if row else None

    def by_tracking_id(self, tracking_id: str) -> DocumentRecord | None:
        with self.engine.connect() as c:
            row = c.execute(select(documents).where(documents.c.tracking_id == tracking_id)).mappings().first()
        return _to_record(row) if row else None

    def indexed_tags_hash(self, doc_id: str) -> str | None:
        with self.engine.connect() as c:
            return c.execute(select(documents.c.indexed_tags_hash).where(documents.c.doc_id == doc_id)).scalar()

    # ------------------------------------------------------------------ transitions

    def transition(self, doc_id: str, status: DocumentStatus, **fields: Any) -> DocumentRecord:
        event_message = fields.pop("event_message", None)
        with self.engine.begin() as c:
            row = c.execute(select(documents).where(documents.c.doc_id == doc_id)).mappings().first()
            if row is None:
                raise NotFound(f"document {doc_id} not found")
            cur = DocumentStatus(row["status"])
            if not can_transition(cur, status):
                raise Conflict(f"illegal transition {cur} -> {status}", detail={"doc_id": doc_id})
            values: dict[str, Any] = {"status": status.value, "updated_at": _now()}
            if "tags" in fields and isinstance(fields["tags"], TagSet):
                # final tags (incl. classifier output); tags_hash keeps fingerprinting the rule layers only
                t: TagSet = fields.pop("tags")
                values["tags"] = t.model_dump()
                self._write_facets(c, doc_id, t)
            if "review_status" in fields and isinstance(fields["review_status"], ReviewStatus):
                values["review_status"] = fields.pop("review_status").value
            values.update(fields)
            if status == DocumentStatus.INDEXED:
                values.setdefault("indexed_at", _now())
                values.setdefault("error_type", None)
                values.setdefault("error_message", None)
            c.execute(update(documents).where(documents.c.doc_id == doc_id).values(**values))
            if status != cur or status in (DocumentStatus.FAILED, DocumentStatus.INDEXED):
                self._event(c, doc_id, status, stage=values.get("stage"),
                            message=event_message or values.get("error_message"),
                            correlation_id=values.get("correlation_id") or row["correlation_id"])
            row = c.execute(select(documents).where(documents.c.doc_id == doc_id)).mappings().first()
        assert row is not None
        return _to_record(row)

    def mark_queued(self, doc_ids: Sequence[str]) -> None:
        if not doc_ids:
            return
        with self.engine.begin() as c:
            for i in range(0, len(doc_ids), 1000):
                c.execute(
                    update(documents)
                    .where(documents.c.doc_id.in_(doc_ids[i:i + 1000]))
                    .values(status=DocumentStatus.QUEUED.value, updated_at=_now())
                )

    def purgeable(self, older_than: datetime, limit: int = 1000) -> list[PurgeCandidate]:
        """Deleted documents, and whether their bytes are safe to remove with them.

        A blob is content-addressed, so several documents can point at one - deleting from the document's
        point of view would take another document's content away. `free_blob` is therefore only true once no
        LIVE document shares the content. Rows whose content_hash is NULL predate content addressing; their
        blob was keyed by doc_id and so was never shared, which is why they are safe on their own.
        """
        with self.engine.connect() as c:
            rows = list(c.execute(
                select(documents.c.doc_id, documents.c.blob_uri, documents.c.content_hash)
                .where(documents.c.status == DocumentStatus.DELETED.value, documents.c.updated_at < older_than)
                .order_by(documents.c.updated_at)
                .limit(limit)
            ).mappings())
            hashes = {r["content_hash"] for r in rows if r["content_hash"]}
            still_referenced: set[str] = set()
            if hashes:
                still_referenced = {
                    h for (h,) in c.execute(
                        select(documents.c.content_hash).where(
                            documents.c.content_hash.in_(hashes),
                            documents.c.status != DocumentStatus.DELETED.value,
                        ).distinct()
                    )
                }
        return [
            PurgeCandidate(
                doc_id=r["doc_id"], blob_uri=r["blob_uri"], content_hash=r["content_hash"],
                free_blob=bool(r["blob_uri"]) and r["content_hash"] not in still_referenced,
            )
            for r in rows
        ]

    def delete_documents(self, doc_ids: Sequence[str]) -> int:
        """Remove state rows outright. Events and facets go with them; the caller frees index and blobs."""
        if not doc_ids:
            return 0
        ids = list(doc_ids)
        with self.engine.begin() as c:
            for i in range(0, len(ids), 1000):
                batch = ids[i:i + 1000]
                c.execute(delete(document_facets).where(document_facets.c.doc_id.in_(batch)))
                c.execute(delete(document_events).where(document_events.c.doc_id.in_(batch)))
                c.execute(delete(documents).where(documents.c.doc_id.in_(batch)))
        return len(ids)

    def mark_unseen_deleted(self, source_id: str, run_id: str) -> list[str]:
        with self.engine.begin() as c:
            ids = [
                r[0]
                for r in c.execute(
                    select(documents.c.doc_id).where(
                        documents.c.source_id == source_id,
                        documents.c.status != DocumentStatus.DELETED.value,
                        (documents.c.last_seen_run_id != run_id) | documents.c.last_seen_run_id.is_(None),
                    )
                )
            ]
            self._mark_deleted(c, ids, "discovery", "not found at source")
        return ids

    def mark_deleted(self, doc_ids: Sequence[str], *, stage: str, message: str) -> list[str]:
        with self.engine.begin() as c:
            ids = [r[0] for r in c.execute(select(documents.c.doc_id).where(
                documents.c.doc_id.in_(list(doc_ids)), documents.c.status != DocumentStatus.DELETED.value))]
            self._mark_deleted(c, ids, stage, message)
        return ids

    def _mark_deleted(self, c: Connection, ids: list[str], stage: str, message: str) -> None:
        for i in range(0, len(ids), 1000):
            c.execute(update(documents).where(documents.c.doc_id.in_(ids[i:i + 1000])).values(
                status=DocumentStatus.DELETED.value, updated_at=_now()))
        for d in ids:
            self._event(c, d, DocumentStatus.DELETED, stage=stage, message=message)

    @staticmethod
    def _filters(q: DocumentQuery, *, with_status: bool = True) -> list[Any]:
        conds: list[Any] = []
        if q.status and with_status:
            conds.append(documents.c.status.in_([s.value for s in q.status]))
        if q.source_id:
            conds.append(documents.c.source_id == q.source_id)
        if q.text:
            conds.append(documents.c.path.ilike(f"%{q.text}%"))
        if q.review_pending:
            conds.append(documents.c.review_status == ReviewStatus.PENDING.value)
        if q.content_hash:
            conds.append(documents.c.content_hash == q.content_hash)
        if q.path_prefix:
            # autoescape, because this is an ownership boundary: a subject containing % or _ would otherwise
            # widen its own prefix into a wildcard and match other people's documents.
            conds.append(documents.c.path.startswith(q.path_prefix, autoescape=True))
        if q.facet:
            sub = select(document_facets.c.doc_id).where(
                document_facets.c.facet == q.facet[0], document_facets.c.value == q.facet[1])
            conds.append(documents.c.doc_id.in_(sub))
        return conds

    @staticmethod
    def _after(q: DocumentQuery) -> Any:
        """The keyset condition. Its shape follows the sort, so the two can never drift apart."""
        if not q.newest_first:
            return documents.c.doc_id > q.after
        try:
            raw = base64.urlsafe_b64decode((q.after or "") + "=" * (-len(q.after or "") % 4)).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError) as e:
            raise ValidationFailed("malformed page cursor") from e
        ts_raw, sep, doc_id = raw.partition("|")
        if not sep or not doc_id:
            raise ValidationFailed("malformed page cursor")
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError as e:
            raise ValidationFailed("malformed page cursor") from e
        # Written out rather than as a row-value comparison ((a, b) < (x, y)): SQLite only supports those from
        # 3.15, and this runs on whatever SQLite ships with the image.
        return or_(documents.c.discovered_at < ts,
                   and_(documents.c.discovered_at == ts, documents.c.doc_id < doc_id))

    def query(self, q: DocumentQuery) -> tuple[list[DocumentRecord], str | None]:
        conds = self._filters(q)
        if q.after:
            conds.append(self._after(q))
        stmt = select(documents)
        if conds:
            stmt = stmt.where(and_(*conds))
        limit = max(1, min(q.limit, 500))
        # NULLS LAST only matters in theory - upsert_discovered sets discovered_at on the one INSERT path and
        # nothing else writes the column - but an undefined position for a NULL would corrupt paging silently
        # rather than loudly, so it is pinned.
        stmt = stmt.order_by(
            *((nulls_last(documents.c.discovered_at.desc()), documents.c.doc_id.desc())
              if q.newest_first else (documents.c.doc_id,))
        ).limit(limit + 1)
        with self.engine.connect() as c:
            rows = [_to_record(r) for r in c.execute(stmt).mappings()]
        page = rows[:limit]
        if len(rows) <= limit or not page:
            return page, None
        last = page[-1]
        if not q.newest_first:
            return page, last.doc_id
        return page, _encode_cursor(last)

    def count_by_status(self, q: DocumentQuery) -> dict[str, int]:
        conds = self._filters(q, with_status=False)
        stmt = select(documents.c.status, func.count()).group_by(documents.c.status)
        if conds:
            stmt = stmt.where(and_(*conds))
        with self.engine.connect() as c:
            return {str(row[0]): int(row[1]) for row in c.execute(stmt)}

    def stale_in_flight(self, older_than: datetime, limit: int) -> list[DocumentRecord]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(documents)
                .where(documents.c.status.in_([s.value for s in IN_FLIGHT_STATUSES]),
                       documents.c.updated_at < older_than)
                .order_by(documents.c.updated_at)
                .limit(limit)
            ).mappings()
            return [_to_record(r) for r in rows]

    def events(self, doc_id: str, limit: int = 200) -> list[DocumentEvent]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(document_events).where(document_events.c.doc_id == doc_id)
                .order_by(document_events.c.id.desc()).limit(limit)
            ).mappings()
            return [
                DocumentEvent(doc_id=r["doc_id"], status=DocumentStatus(r["status"]), at=_aware(r["at"]) or _now(),
                              stage=r["stage"], message=r["message"], correlation_id=r["correlation_id"])
                for r in rows
            ]

    # ------------------------------------------------------------------ runs

    def start_run(self, source_id: str, trigger: str) -> IngestionRun:
        run = IngestionRun(run_id=uuid.uuid4().hex, source_id=source_id, trigger=trigger, started_at=_now())
        with self.engine.begin() as c:
            c.execute(insert(ingestion_runs).values(**run.model_dump(mode="python")))
        return run

    def finish_run(self, run: IngestionRun) -> None:
        with self.engine.begin() as c:
            c.execute(update(ingestion_runs).where(ingestion_runs.c.run_id == run.run_id).values(
                status=run.status.value, finished_at=run.finished_at or _now(), discovered=run.discovered,
                queued=run.queued, unchanged=run.unchanged, deleted=run.deleted, error=run.error))

    def _run(self, r: RowMapping) -> IngestionRun:
        return IngestionRun(
            run_id=r["run_id"], source_id=r["source_id"], trigger=r["trigger"], status=RunStatus(r["status"]),
            started_at=_aware(r["started_at"]) or _now(), finished_at=_aware(r["finished_at"]),
            discovered=r["discovered"], queued=r["queued"], unchanged=r["unchanged"], deleted=r["deleted"],
            error=r["error"])

    def list_runs(self, source_id: str | None, limit: int) -> list[IngestionRun]:
        stmt = select(ingestion_runs).order_by(ingestion_runs.c.started_at.desc()).limit(min(limit, 500))
        if source_id:
            stmt = stmt.where(ingestion_runs.c.source_id == source_id)
        with self.engine.connect() as c:
            return [self._run(r) for r in c.execute(stmt).mappings()]

    def get_run(self, run_id: str) -> IngestionRun | None:
        with self.engine.connect() as c:
            r = c.execute(select(ingestion_runs).where(ingestion_runs.c.run_id == run_id)).mappings().first()
        return self._run(r) if r else None

    def run_progress(self, run_id: str) -> dict[str, int]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(documents.c.status, func.count()).where(documents.c.run_id == run_id)
                .group_by(documents.c.status)
            ).all()
        return {s: int(n) for s, n in rows}

    def last_run_started(self, source_id: str) -> datetime | None:
        with self.engine.connect() as c:
            v = c.execute(select(func.max(ingestion_runs.c.started_at)).where(
                ingestion_runs.c.source_id == source_id)).scalar()
        return _aware(v)

    # ------------------------------------------------------------------ reporting

    def summary(self, group_by_facet: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            rows = [
                {"kind": "source", "key": s, "status": st, "count": int(n)}
                for s, st, n in c.execute(
                    select(documents.c.source_id, documents.c.status, func.count())
                    .group_by(documents.c.source_id, documents.c.status)
                ).all()
            ]
            if group_by_facet:
                rows += [
                    {"kind": "facet", "key": v, "status": st, "count": int(n)}
                    for v, st, n in c.execute(
                        select(document_facets.c.value, documents.c.status, func.count())
                        .join(documents, documents.c.doc_id == document_facets.c.doc_id)
                        .where(document_facets.c.facet == group_by_facet)
                        .group_by(document_facets.c.value, documents.c.status)
                    ).all()
                ]
        return rows

    def error_breakdown(self, run_id: str | None, limit: int = 20) -> list[dict[str, Any]]:
        stmt = (
            select(documents.c.stage, documents.c.error_type, func.count(), func.max(documents.c.error_message))
            .where(documents.c.status == DocumentStatus.FAILED.value)
            .group_by(documents.c.stage, documents.c.error_type)
            .order_by(func.count().desc())
            .limit(limit)
        )
        if run_id:
            stmt = stmt.where(documents.c.run_id == run_id)
        with self.engine.connect() as c:
            return [
                {"stage": s, "error_type": t, "count": int(n), "example": (m or "")[:300]}
                for s, t, n, m in c.execute(stmt).all()
            ]

    # ------------------------------------------------------------------ controls

    def get_controls(self) -> IngestionControls:
        with self.engine.connect() as c:
            v = c.execute(select(controls.c.data).where(controls.c.id == 1)).scalar()
        return IngestionControls.model_validate(v) if v else IngestionControls()

    def set_controls(self, ctl: IngestionControls) -> None:
        data = ctl.model_copy(update={"updated_at": _now()}).model_dump(mode="json")
        with self.engine.begin() as c:
            if c.execute(select(controls.c.id).where(controls.c.id == 1)).first():
                c.execute(update(controls).where(controls.c.id == 1).values(data=data))
            else:
                c.execute(insert(controls).values(id=1, data=data))
