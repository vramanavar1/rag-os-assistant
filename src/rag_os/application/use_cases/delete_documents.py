"""Permanently delete documents, and everything derived from them.

The soft delete (Admin > Documents > a document, before this existed) marks a document DELETED and removes its
chunks; its stored copy and state rows wait for a purge. This removes everything now: index chunks, the stored
copy (unless another document still uses the same bytes), the state rows and status history, queued messages,
and the query traces that mention it - a trace can hold the document's title and answer text quoting it.

What it cannot do: a document crawled from a source whose file is still there comes back on that source's next
sync (by design - delete it at the source to keep it out). An upload has no source, so it is gone for good.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from rag_os.application.ports import (
    IngestionStateStore,
    MessageQueue,
    QueryTraceStore,
    RawDocumentStore,
    SearchIndex,
)

log = logging.getLogger(__name__)
audit = logging.getLogger("rag_os.audit")

MAX_BATCH = 500


@dataclass
class DeleteReport:
    documents: int = 0
    chunks: int = 0
    blobs: int = 0
    blobs_kept_shared: int = 0
    blobs_left_at_source: int = 0
    messages: int = 0
    traces: int = 0
    expectations_updated: int = 0
    not_found: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class DeleteDocuments:
    def __init__(self, state: IngestionStateStore, index: SearchIndex, raw: RawDocumentStore, queue: MessageQueue,
                 traces: QueryTraceStore | None) -> None:
        self.state = state
        self.index = index
        self.raw = raw
        self.queue = queue
        self.traces = traces

    async def run(self, doc_ids: list[str], *, by: str) -> DeleteReport:
        report = DeleteReport()
        ids = list(dict.fromkeys(d for d in doc_ids if d))[:MAX_BATCH]
        recs = {}
        for d in ids:
            rec = self.state.get(d)
            if rec is None:
                report.not_found.append(d)
            else:
                recs[d] = rec
        if not recs:
            return report
        live = list(recs)

        # 1. DELETED first: a worker that picks up one of these now skips it rather than re-indexing it.
        self.state.mark_deleted(live, stage="admin", message=f"permanently deleted by {by}")
        report.messages = await self.queue.purge_messages(live)

        # 2. Chunks before rows: chunks with no row are unreachable from the console yet still searchable.
        for d in live:
            try:
                report.chunks += await self.index.delete_doc_versions(d, None)
            except Exception as e:  # one bad document must not strand the rest
                report.errors.append(f"{d}: index: {type(e).__name__}: {e}"[:300])

        # 3. Stored copies - only ours, and only those no other live document still uses.
        uris = list(dict.fromkeys(r.blob_uri for r in recs.values() if r.blob_uri))
        shared = self.state.blob_refs(uris, live) if uris else set()
        for uri in uris:
            try:
                if not self.raw.owns(uri):
                    report.blobs_left_at_source += 1
                elif uri in shared:
                    report.blobs_kept_shared += 1
                elif self.raw.delete(uri):
                    report.blobs += 1
            except Exception as e:
                report.errors.append(f"blob {uri[:60]}: {type(e).__name__}: {e}"[:300])

        # 4. Rows, status history and facets.
        report.documents = self.state.delete_documents(live)

        # 5. What was derived from them.
        if self.traces is not None:
            forgot = self.traces.forget_documents(live)
            report.traces, report.expectations_updated = forgot["traces"], forgot["expectations"]

        # 6. Final sweep: an upsert that raced step 2 (a worker already past its own DELETED check).
        for d in live:
            try:
                report.chunks += await self.index.delete_doc_versions(d, None)
            except Exception as e:
                report.errors.append(f"{d}: sweep: {type(e).__name__}: {e}"[:300])

        audit.warning("documents permanently deleted", extra={
            "by": by, "doc_ids": live[:50], **{k: v for k, v in report.as_dict().items() if k != "not_found"}})
        return report
