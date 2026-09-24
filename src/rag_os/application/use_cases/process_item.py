"""ProcessItem: the worker pipeline for one queue message.

FULL:   parse -> chunk -> embed (self-hosted model) -> classify unset facets (reusing chunk vectors)
        -> index new version -> delete previous version's chunks -> INDEXED.   (idempotent: deterministic ids)
RETAG:  merge new facet/ACL fields into the existing chunks (no parsing, no embedding).
DELETE: remove all chunks of the document.

Errors are classified: permanent (bad file) -> FAILED immediately; transient (dependency) -> retried by the
queue until max delivery, then FAILED + dead-letter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from rag_os.application.ports import (
    Chunker,
    Classifier,
    DocumentParser,
    EmbeddingProvider,
    IngestionStateStore,
    RawDocumentStore,
    SearchIndex,
)
from rag_os.application.services.access_policy import AccessPolicyEngine
from rag_os.application.services.index_schema import IndexDocumentMapper
from rag_os.application.services.tagging import TagResolver
from rag_os.domain.classification import FacetSchema
from rag_os.domain.documents import (
    ChunkDraft,
    DocumentRecord,
    DocumentStatus,
    IndexedChunk,
    ReviewStatus,
    TagSet,
    stable_id,
    tags_hash,
)
from rag_os.domain.embedding import EmbeddingProfile
from rag_os.domain.errors import NotFound, NotSupported, ParseError, ValidationFailed
from rag_os.domain.ingestion import IngestMessage, MessageMode

log = logging.getLogger(__name__)

PERMANENT_ERRORS: tuple[type[BaseException], ...] = (ParseError, NotSupported, ValidationFailed, NotFound)


class EmptyDocument(ParseError):
    code = "empty_document"


@dataclass
class Outcome:
    status: str  # indexed | retagged | deleted | skipped | failed_permanent
    chunks: int = 0
    seconds: float = 0.0


def chunk_id(doc_id: str, version: str, ordinal: int) -> str:
    return stable_id(doc_id, version, str(ordinal))


class ProcessItem:
    def __init__(
        self,
        *,
        state: IngestionStateStore,
        raw: RawDocumentStore,
        parser_for: Callable[[str, str | None], DocumentParser],
        chunker: Chunker,
        embedder: EmbeddingProvider,
        index: SearchIndex,
        mapper: IndexDocumentMapper,
        engine: AccessPolicyEngine,
        facets: FacetSchema,
        tagger: TagResolver,
        classifier: Classifier,
        profile: EmbeddingProfile,
        index_semaphore: asyncio.Semaphore,
        index_batch: int = 500,
        max_file_mb: int = 100,
    ) -> None:
        self.state = state
        self.raw = raw
        self.parser_for = parser_for
        self.chunker = chunker
        self.embedder = embedder
        self.index = index
        self.mapper = mapper
        self.engine = engine
        self.facets = facets
        self.tagger = tagger
        self.classifier = classifier
        self.profile = profile
        self.fp = profile.fingerprint()
        self.index_sem = index_semaphore
        self.index_batch = index_batch
        self.max_bytes = max_file_mb * 1024 * 1024

    # ------------------------------------------------------------------ entry point

    async def handle(self, msg: IngestMessage) -> Outcome:
        t0 = time.perf_counter()
        rec = self.state.get(msg.doc_id)
        if rec is None:
            return Outcome("skipped")
        if msg.mode == MessageMode.DELETE:
            n = await self.index.delete_doc_versions(msg.doc_id, None)
            log.info("document removed from index", extra={"doc_id": msg.doc_id, "chunks": n})
            return Outcome("deleted", n, time.perf_counter() - t0)
        if rec.status == DocumentStatus.DELETED or msg.version_key != rec.version_key:
            return Outcome("skipped")  # stale message: a newer version (or deletion) superseded it
        if msg.mode == MessageMode.RETAG:
            return await self._retag(rec, t0)
        # embedding_fp is part of the test: the same bytes under a DIFFERENT embedding profile are not
        # "already done" - they belong to a different vector space, in a different index.
        if (rec.status == DocumentStatus.INDEXED and rec.indexed_version == msg.version_key
                and rec.indexed_tags_hash == tags_hash(rec.tags) and rec.embedding_fp == self.fp):
            return Outcome("skipped")
        return await self._full(rec, msg, t0)

    # ------------------------------------------------------------------ full pipeline

    def _parse_and_chunk(self, rec: DocumentRecord) -> tuple[str, list[ChunkDraft], dict[str, Any]]:
        if not rec.blob_uri:
            raise NotFound("document has no stored content (blob_uri missing)")
        filename = rec.path.split("/")[-1]
        parser = self.parser_for(filename, rec.content_type)
        with self.raw.open(rec.blob_uri) as stream:
            try:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(0)
            except (OSError, ValueError):
                size = 0
            if size > self.max_bytes:
                raise ValidationFailed(f"file is {size} bytes; limit {self.max_bytes}")
            parsed = parser.parse(stream, filename)
            drafts = list(self.chunker.chunk(parsed, self.profile.chunking))
        return (parsed.title or filename), drafts, dict(parsed.metadata)

    async def _full(self, rec: DocumentRecord, msg: IngestMessage, t0: float) -> Outcome:
        doc_id, version = rec.doc_id, msg.version_key
        self.state.transition(doc_id, DocumentStatus.PARSING, stage="parse", attempts=rec.attempts + 1,
                              correlation_id=msg.correlation_id or rec.correlation_id)
        title, drafts, meta = await asyncio.to_thread(self._parse_and_chunk, rec)
        if not drafts:
            raise EmptyDocument("no extractable text (scanned image PDF? password protected?)")
        self.state.transition(doc_id, DocumentStatus.CHUNKED, stage="chunk", title=title[:500],
                              chunk_count=len(drafts))

        texts = [f"{title}\n{d.heading}\n{d.text}" if d.heading else f"{title}\n{d.text}" for d in drafts]
        vectors, emb_usage = await self.embedder.embed_documents(texts)
        self.state.transition(doc_id, DocumentStatus.EMBEDDED, stage="embed",
                              event_message=f"{len(vectors)} vectors, {emb_usage.embedding} tokens")

        tags = rec.tags
        review = rec.review_status
        unset = self.tagger.unset_classifiable(tags) if rec.review_status != ReviewStatus.APPROVED else []
        if unset:
            sample = title + "\n" + "\n".join(d.text for d in drafts[:3])
            doc_vec = np.mean(np.asarray(vectors[: min(len(vectors), 8)], dtype=np.float32), axis=0).tolist()
            result = await self.classifier.classify(facets_to_fill=unset, text_sample=sample, vector=doc_vec,
                                                    schema=self.facets)
            if result.facets:
                tags = tags.merged_with(TagSet(facets=result.facets), f"classifier:{result.method}")
                for f in result.facets:
                    tags.sources[f"confidence:{f}"] = str(result.confidence.get(f, ""))
            if result.needs_review and review != ReviewStatus.APPROVED:
                review = ReviewStatus.PENDING
        self.state.transition(doc_id, DocumentStatus.CLASSIFIED, stage="classify", tags=tags, review_status=review)

        acl = self.engine.validate_doc_acl(tags.acl)
        docs = [
            self.mapper.to_document(IndexedChunk(
                chunk_id=chunk_id(doc_id, version, d.ordinal), doc_id=doc_id, doc_version=version,
                ordinal=d.ordinal, title=title[:500], heading=d.heading[:500], content=d.text, page=d.page,
                source_id=rec.source_id, path=rec.path, content_type=rec.content_type, embedding_fp=self.fp,
                facets=tags.facets, acl=acl, vector=v, is_current=True,
                effective_date=str(meta.get("effective_date")) if meta.get("effective_date") else None,
            ))
            for d, v in zip(drafts, vectors, strict=True)
        ]
        async with self.index_sem:  # global cap on concurrent index writes (protects query latency)
            for i in range(0, len(docs), self.index_batch):
                await self.index.upsert(docs[i:i + self.index_batch])
            removed = await self.index.delete_doc_versions(doc_id, keep_version=version)
        self.state.transition(doc_id, DocumentStatus.INDEXED, stage="index", indexed_version=version,
                              chunk_count=len(docs), embedding_fp=self.fp, indexed_tags_hash=tags_hash(tags),
                              event_message=f"{len(docs)} chunks indexed, {removed} stale removed")
        return Outcome("indexed", len(docs), time.perf_counter() - t0)

    # ------------------------------------------------------------------ retag

    async def _retag(self, rec: DocumentRecord, t0: float) -> Outcome:
        if not rec.indexed_version or rec.chunk_count == 0:
            # never indexed -> needs a full run
            return await self._full(rec, IngestMessage(doc_id=rec.doc_id, version_key=rec.version_key,
                                                       source_id=rec.source_id), t0)
        acl = self.engine.validate_doc_acl(rec.tags.acl)
        template = self.mapper.to_document(IndexedChunk(
            chunk_id="x", doc_id=rec.doc_id, doc_version=rec.indexed_version, ordinal=0, title="", heading="",
            content="", page=None, source_id=rec.source_id, path=rec.path, content_type=rec.content_type,
            embedding_fp=self.fp, facets=rec.tags.facets, acl=acl, vector=[],
        ))
        tag_fields = {k: template[k] for k in self._tag_fields() if k in template}
        docs = [{"chunk_id": chunk_id(rec.doc_id, rec.indexed_version, i), **tag_fields}
                for i in range(rec.chunk_count)]
        async with self.index_sem:
            await self.index.merge(docs)
        self.state.transition(rec.doc_id, DocumentStatus.INDEXED, stage="retag",
                              indexed_tags_hash=tags_hash(rec.tags), event_message="tags/ACL updated in index")
        return Outcome("retagged", len(docs), time.perf_counter() - t0)

    def _tag_fields(self) -> set[str]:
        fields = set(self.mapper.facet_fields.values())
        fields |= {a.field for a in self.engine.policy.attributes}
        return fields
