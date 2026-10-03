"""Reclaim space from deleted documents.

Nothing in this system used to free anything. A document deleted at source lost its chunks and kept its state
row, its event timeline and its staged bytes forever - `RawDocumentStore` had no `delete` at all. On a corpus
of millions that is the largest storage leak there is, and it matters more than duplication does, because the
binding constraint on Azure AI Search is vector quota (35 GB per S1 partition) past which indexing hard-fails.

Blobs are content-addressed, so several documents can share one. A blob is therefore only freed once no LIVE
document references that content: deleting from the document's point of view would take another document's
bytes away. `IngestionStateStore.purgeable` works that out; this use case acts on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from rag_os.application.ports import IngestionStateStore, RawDocumentStore, SearchIndex

log = logging.getLogger(__name__)


@dataclass
class PurgeReport:
    documents: int = 0
    chunks: int = 0
    blobs: int = 0
    blobs_kept_shared: int = 0
    blobs_left_at_source: int = 0  # documents read in place from their source: the file is not ours to delete
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "documents": self.documents, "chunks": self.chunks, "blobs": self.blobs,
            "blobs_kept_shared": self.blobs_kept_shared, "blobs_left_at_source": self.blobs_left_at_source,
            "dry_run": self.dry_run, "errors": self.errors,
        }


class Purge:
    def __init__(self, state: IngestionStateStore, raw: RawDocumentStore, index: SearchIndex) -> None:
        self.state = state
        self.raw = raw
        self.index = index

    async def run(self, retention_days: int = 7, limit: int = 1000, dry_run: bool = True) -> PurgeReport:
        """Free deleted documents older than the retention window.

        Defaults to a dry run: this is the one operation here that destroys data, and a deletion the operator
        did not ask for is worse than a blob that lingers another day. The window exists so that a source
        briefly failing to list a file - a mount that dropped, a permissions blip - does not permanently
        destroy the document it marked DELETED.
        """
        report = PurgeReport(dry_run=dry_run)
        older_than = datetime.now(UTC) - timedelta(days=retention_days)
        candidates = self.state.purgeable(older_than, limit=limit)
        if not candidates:
            return report

        freed_doc_ids: list[str] = []
        seen_blobs: set[str] = set()
        for cand in candidates:
            try:
                if not dry_run:
                    # Chunks first: a row with no chunks is merely stale, whereas chunks with no row are
                    # unreachable and unattributable.
                    report.chunks += await self.index.delete_doc_versions(cand.doc_id, None)
                if cand.blob_uri and not self.raw.owns(cand.blob_uri):
                    # Read in place from its source (an azure_blob source): the customer's file, never ours.
                    report.blobs_left_at_source += 1
                elif cand.free_blob and cand.blob_uri and cand.blob_uri not in seen_blobs:
                    seen_blobs.add(cand.blob_uri)
                    if dry_run or self.raw.delete(cand.blob_uri):
                        report.blobs += 1
                elif cand.blob_uri and not cand.free_blob:
                    report.blobs_kept_shared += 1
                freed_doc_ids.append(cand.doc_id)
            except Exception as e:  # one bad document must not strand the rest
                report.errors.append(f"{cand.doc_id}: {type(e).__name__}: {e}"[:300])
                log.warning("purge failed for document", extra={"doc_id": cand.doc_id, "error": str(e)[:300]})

        report.documents = len(freed_doc_ids)
        if not dry_run and freed_doc_ids:
            self.state.delete_documents(freed_doc_ids)
        log.info("purge complete", extra={k: v for k, v in report.as_dict().items() if k != "errors"})
        return report
