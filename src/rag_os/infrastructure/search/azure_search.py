"""Azure AI Search adapter (keyless via managed identity).

* Index created from the generated IndexSchema (hybrid + semantic ranker + int8 scalar quantization).
* New fields (e.g. a new access-policy attribute) are added in place; type/dimension changes are refused.
* The embedding profile is stored in the index ``description`` (``rag-os-profile:{json}``).
* Upserts are batched and retried with exponential backoff on throttling (503/429/207 partial failures),
  which protects query latency when ingestion competes for the same replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence
from typing import Any

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    BinaryQuantizationCompression,
    HnswAlgorithmConfiguration,
    HnswParameters,
    ScalarQuantizationCompression,
    SearchField,
    SearchFieldDataType,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    VectorSearch,
    VectorSearchProfile,
)
from azure.search.documents.indexes.models import SearchIndex as AzIndex
from azure.search.documents.models import VectorizedQuery
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from rag_os.application.ports import IndexField, IndexSchema, SearchIndex, SearchRequest
from rag_os.application.services.index_schema import BASE_SELECT
from rag_os.domain.answers import SearchHit
from rag_os.domain.errors import DependencyUnavailable, ProfileMismatch
from rag_os.infrastructure.registry import SEARCH_INDEXES

log = logging.getLogger(__name__)
_PROFILE_PREFIX = "rag-os-profile:"
_SEMANTIC = "default"
_MAX_BATCH = 1000


def _az_type(f: IndexField) -> str:
    return {
        "string": SearchFieldDataType.String,
        "strings": SearchFieldDataType.Collection(SearchFieldDataType.String),
        "int": SearchFieldDataType.Int32,
        "bool": SearchFieldDataType.Boolean,
        "date": SearchFieldDataType.DateTimeOffset,
        "vector": SearchFieldDataType.Collection(SearchFieldDataType.Single),
    }[f.type]


def _to_field(f: IndexField) -> SearchField:
    if f.type == "vector":
        return SearchField(
            name=f.name,
            type=_az_type(f),
            searchable=True,
            retrievable=False,
            stored=False,  # vectors never returned -> smaller index
            vector_search_dimensions=f.dimensions,
            vector_search_profile_name="vp",
        )
    return SearchField(
        name=f.name,
        type=_az_type(f),
        key=f.key,
        searchable=f.searchable,
        filterable=f.filterable,
        facetable=f.facetable,
        sortable=f.sortable,
        retrievable=f.retrievable,
    )


def _retryable(e: BaseException) -> bool:
    if isinstance(e, HttpResponseError):
        return e.status_code in (429, 500, 502, 503, 504)
    return isinstance(e, _PartialFailure | asyncio.TimeoutError | ConnectionError)


class _PartialFailure(Exception):
    def __init__(self, failed: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(failed)} documents failed")
        self.failed = failed


@SEARCH_INDEXES.register("azure", description="Azure AI Search (hybrid, semantic ranker, int8 compression).")
class AzureSearchIndex(SearchIndex):
    def __init__(self, index_name: str, endpoint: str, semantic: bool = True, **_: Any) -> None:
        if not endpoint:
            raise DependencyUnavailable("SEARCH_ENDPOINT is not configured")
        self.index_name = index_name
        self.semantic = semantic
        self._cred = DefaultAzureCredential()
        self._indexes = SearchIndexClient(endpoint=endpoint, credential=self._cred)
        self._client = SearchClient(endpoint=endpoint, index_name=index_name, credential=self._cred)

    async def aclose(self) -> None:
        await self._client.close()
        await self._indexes.close()
        await self._cred.close()

    # ------------------------------------------------------------------ schema

    def _build(self, schema: IndexSchema, description: str | None) -> AzIndex:
        # Compression is not part of the embedding fingerprint, so changing it does NOT rename the index and
        # ensure_index does not drift-check it - a change only takes effect on a rebuilt index.
        compressions: list[Any] = []
        profile_kwargs: dict[str, Any] = {}
        if schema.compression == "scalar":
            compressions = [ScalarQuantizationCompression(compression_name="sq")]
            profile_kwargs["compression_name"] = "sq"
        elif schema.compression == "binary":
            compressions = [BinaryQuantizationCompression(compression_name="bq")]
            profile_kwargs["compression_name"] = "bq"
        return AzIndex(
            name=schema.name,
            description=description,
            fields=[_to_field(f) for f in schema.fields],
            vector_search=VectorSearch(
                algorithms=[
                    HnswAlgorithmConfiguration(
                        name="hnsw", parameters=HnswParameters(m=8, ef_construction=400, ef_search=500, metric="cosine")
                    )
                ],
                profiles=[VectorSearchProfile(name="vp", algorithm_configuration_name="hnsw", **profile_kwargs)],
                compressions=compressions or None,
            ),
            semantic_search=SemanticSearch(
                default_configuration_name=_SEMANTIC,
                configurations=[
                    SemanticConfiguration(
                        name=_SEMANTIC,
                        prioritized_fields=SemanticPrioritizedFields(
                            title_field=SemanticField(field_name=schema.semantic_title),
                            content_fields=[SemanticField(field_name=n) for n in schema.semantic_content],
                            keywords_fields=[SemanticField(field_name=n) for n in schema.semantic_keywords],
                        ),
                    )
                ],
            ),
        )

    async def clear(self, schema: IndexSchema, profile: dict[str, Any] | None) -> int:
        """Delete and recreate the index - seconds at any size, where deleting chunk by chunk is hours at
        millions. The same schema and embedding-profile stamp are written back, so the profile guard passes and
        nothing needs a bootstrap. Queries in the few seconds between delete and create see index_not_ready."""
        try:
            n = int(await self._client.get_document_count())
        except ResourceNotFoundError:
            n = 0
        with contextlib.suppress(ResourceNotFoundError):
            await self._indexes.delete_index(schema.name)
        await self._indexes.create_index(self._build(schema, None))
        if profile is not None:
            await self.write_profile(profile)
        log.warning("search index cleared (deleted and recreated)", extra={"index": schema.name, "chunks": n})
        return n

    async def ensure_index(self, schema: IndexSchema) -> None:
        try:
            existing = await self._indexes.get_index(schema.name)
        except ResourceNotFoundError:
            await self._indexes.create_index(self._build(schema, None))
            log.info("search index created", extra={"index": schema.name})
            return
        have = {f.name: f for f in existing.fields}
        added = []
        for f in schema.fields:
            cur = have.get(f.name)
            if cur is None:
                existing.fields.append(_to_field(f))
                added.append(f.name)
                continue
            if str(cur.type) != str(_az_type(f)):
                raise ProfileMismatch(
                    f"index field '{f.name}' has type {cur.type}, configuration requires {_az_type(f)}; "
                    "create a new index version"
                )
            if f.type == "vector" and cur.vector_search_dimensions != f.dimensions:
                raise ProfileMismatch(
                    f"index vector dimensions {cur.vector_search_dimensions} != profile {f.dimensions}"
                )
        if added:
            await self._indexes.create_or_update_index(existing)
            log.info("search index fields added", extra={"index": schema.name, "fields": added})

    async def index_exists(self) -> bool:
        try:
            await self._indexes.get_index(self.index_name)
        except ResourceNotFoundError:
            return False
        return True

    async def read_profile(self) -> dict[str, Any] | None:
        try:
            idx = await self._indexes.get_index(self.index_name)
        except ResourceNotFoundError:
            return None
        desc = idx.description or ""
        if desc.startswith(_PROFILE_PREFIX):
            return dict(json.loads(desc[len(_PROFILE_PREFIX):]))
        return None

    async def write_profile(self, profile: dict[str, Any]) -> None:
        idx = await self._indexes.get_index(self.index_name)
        idx.description = _PROFILE_PREFIX + json.dumps(profile, sort_keys=True, separators=(",", ":"))
        await self._indexes.create_or_update_index(idx)

    # ------------------------------------------------------------------ writes

    async def _upload(self, batch: list[dict[str, Any]]) -> None:
        pending = batch
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_retryable),
            wait=wait_exponential_jitter(initial=1, max=30),
            stop=stop_after_attempt(8),
            reraise=True,
        ):
            with attempt:
                results = await self._client.merge_or_upload_documents(documents=pending)
                failed_keys = {r.key for r in results if not r.succeeded}
                if failed_keys:
                    pending = [d for d in pending if d["chunk_id"] in failed_keys]
                    raise _PartialFailure(pending)

    async def upsert(self, documents: Sequence[dict[str, Any]]) -> None:
        docs = [{k: v for k, v in d.items() if v is not None} for d in documents]
        for i in range(0, len(docs), _MAX_BATCH):
            await self._upload(docs[i:i + _MAX_BATCH])

    async def merge(self, documents: Sequence[dict[str, Any]]) -> None:
        for i in range(0, len(documents), _MAX_BATCH):
            batch = list(documents[i:i + _MAX_BATCH])
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_retryable), wait=wait_exponential_jitter(initial=1, max=30),
                stop=stop_after_attempt(8), reraise=True,
            ):
                with attempt:
                    results = await self._client.merge_documents(documents=batch)
                    failed = {r.key for r in results if not r.succeeded and r.status_code != 404}
                    if failed:
                        batch = [d for d in batch if d["chunk_id"] in failed]
                        raise _PartialFailure(batch)

    async def delete_doc_versions(self, doc_id: str, keep_version: str | None) -> int:
        safe = doc_id.replace("'", "''")
        flt = f"doc_id eq '{safe}'"
        if keep_version is not None:
            flt += f" and doc_version ne '{keep_version.replace(chr(39), chr(39) * 2)}'"
        deleted = 0
        while True:
            results = await self._client.search(search_text="*", filter=flt, select=["chunk_id"], top=1000)
            keys = [{"chunk_id": r["chunk_id"]} async for r in results]
            if not keys:
                return deleted
            await self._client.delete_documents(documents=keys)
            deleted += len(keys)
            if len(keys) < 1000:
                return deleted

    # ------------------------------------------------------------------ reads

    async def search(self, request: SearchRequest) -> list[SearchHit]:
        kwargs: dict[str, Any] = {
            "search_text": request.text or "*",
            "filter": request.odata_filter,
            "top": request.top,
            "select": request.select,
        }
        if request.vector is not None:
            kwargs["vector_queries"] = [
                VectorizedQuery(vector=request.vector, k_nearest_neighbors=request.candidates, fields="vector")
            ]
        if request.semantic and self.semantic and request.text:
            kwargs.update(query_type="semantic", semantic_configuration_name=_SEMANTIC)
        extra = [f for f in (request.select or []) if f not in BASE_SELECT]
        try:
            results = await self._client.search(**kwargs)
            hits: list[SearchHit] = []
            async for r in results:
                hits.append(
                    SearchHit(
                        chunk_id=r["chunk_id"],
                        doc_id=r["doc_id"],
                        title=r.get("title") or "",
                        heading=r.get("heading") or "",
                        content=r.get("content") or "",
                        path=r.get("path") or "",
                        page=r.get("page"),
                        source_id=r.get("source_id") or "",
                        effective_date=r.get("effective_date"),
                        score=float(r.get("@search.score") or 0.0),
                        reranker_score=r.get("@search.reranker_score"),
                        fields={f: r.get(f) for f in extra},
                    )
                )
            return hits
        except HttpResponseError as e:
            raise DependencyUnavailable("search query failed", detail={"status": e.status_code}) from e

    async def facets(self, odata_filter: str | None, fields: Sequence[str]) -> dict[str, dict[str, int]]:
        results = await self._client.search(
            search_text="*", filter=odata_filter, facets=[f"{f},count:100" for f in fields], top=0
        )
        raw = await results.get_facets() or {}
        return {f: {str(b["value"]): int(b["count"]) for b in raw.get(f, [])} for f in fields}

    async def count(self) -> int:
        return int(await self._client.get_document_count())
