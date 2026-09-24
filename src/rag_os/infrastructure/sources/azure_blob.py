"""Azure Blob container source. Workers read blobs directly (no staging copy).

Listing is streamed page by page (millions of blobs, constant memory). Sidecars are paired in a single pass:
because listings are lexicographic, ``<name>.meta.json`` can only appear after ``<name>``; once the listing
passes that name the item is emitted.
"""

from __future__ import annotations

import fnmatch
import tempfile
from collections import OrderedDict
from collections.abc import Iterator
from typing import IO, Any

from pydantic import BaseModel, Field

from rag_os.application.ports import DocumentSource
from rag_os.domain.documents import SourceItem, TagSet
from rag_os.domain.errors import ConfigError
from rag_os.domain.ingestion import SourceConfig
from rag_os.infrastructure.registry import SOURCES
from rag_os.infrastructure.sources.metadata_files import MANIFEST_NAME, SIDECAR_SUFFIX, parse_manifest, parse_sidecar


class AzureBlobSettings(BaseModel):
    account_url: str | None = None  # default: BLOB_ACCOUNT_URL of the app
    container: str
    prefix: str = ""
    connection_string: str | None = None  # dev only (Azurite); supports kv://
    include: list[str] = Field(default_factory=lambda: ["*"])
    exclude: list[str] = Field(default_factory=list)


@SOURCES.register("azure_blob", config=AzureBlobSettings,
                  description="Azure Blob Storage container/prefix (keyless). Upload endpoint writes here too.")
class AzureBlobSource(DocumentSource):
    staging_required = False

    def __init__(self, config: SourceConfig, settings: AzureBlobSettings, app: Any = None, **_: Any) -> None:
        super().__init__(config)
        self.settings = settings
        from azure.storage.blob import ContainerClient

        conn = settings.connection_string or (getattr(app, "blob_connection_string", None) if app else None)
        account = settings.account_url or (getattr(app, "blob_account_url", None) if app else None)
        if conn:
            self._cc = ContainerClient.from_connection_string(conn, settings.container)
        elif account:
            from azure.identity import DefaultAzureCredential

            self._cc = ContainerClient(account_url=account, container_name=settings.container,
                                       credential=DefaultAzureCredential())
        else:
            raise ConfigError(f"source '{config.id}': account_url or BLOB_ACCOUNT_URL required")
        self.prefix = settings.prefix.lstrip("/")

    def healthcheck(self) -> None:
        if not self._cc.exists():
            raise ConfigError(f"source '{self.id}': container '{self.settings.container}' not found")

    def _wanted(self, rel: str) -> bool:
        low = rel.lower()
        inc = any(fnmatch.fnmatchcase(low, p.lower()) for p in self.settings.include) or not self.settings.include
        exc = any(fnmatch.fnmatchcase(low, p.lower()) for p in self.settings.exclude)
        return inc and not exc

    def _item(self, b: Any) -> SourceItem:
        rel = b.name[len(self.prefix):].lstrip("/")
        md5 = getattr(getattr(b, "content_settings", None), "content_md5", None)
        return SourceItem(
            source_id=self.id,
            item_id=b.name,
            path=rel,
            uri=f"{self._cc.url}/{b.name}",
            etag=str(b.etag).strip('"'),
            content_hash=bytes(md5).hex() if md5 else None,
            size=int(b.size or 0),
            modified=b.last_modified,
            content_type=getattr(getattr(b, "content_settings", None), "content_type", None),
            metadata={k: v for k, v in (b.metadata or {}).items()} if getattr(b, "metadata", None) else {},
        )

    def iter_items(self) -> Iterator[SourceItem]:
        pending: OrderedDict[str, SourceItem] = OrderedDict()
        manifest_name = f"{self.prefix}{MANIFEST_NAME}"
        for b in self._cc.list_blobs(name_starts_with=self.prefix or None, include=["metadata"]):
            name: str = b.name
            if name.endswith(SIDECAR_SUFFIX):
                base = name[: -len(SIDECAR_SUFFIX)]
                if base in pending:
                    data = self._cc.download_blob(name).readall()
                    pending[base] = pending[base].model_copy(update={"sidecar": parse_sidecar(bytes(data))})
                continue
            while pending:
                first = next(iter(pending))
                if first + SIDECAR_SUFFIX < name:
                    yield pending.popitem(last=False)[1]
                else:
                    break
            if name == manifest_name or name.endswith("/"):
                continue
            rel = name[len(self.prefix):].lstrip("/")
            if self._wanted(rel):
                pending[name] = self._item(b)
        while pending:
            yield pending.popitem(last=False)[1]

    def open(self, item: SourceItem) -> IO[bytes]:
        spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)  # noqa: SIM115
        self._cc.download_blob(item.item_id, max_concurrency=4).readinto(spool)
        spool.seek(0)
        return spool  # type: ignore[return-value]

    def read_manifest(self) -> dict[str, TagSet]:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            data = self._cc.download_blob(f"{self.prefix}{MANIFEST_NAME}").readall()
        except ResourceNotFoundError:
            return {}
        import io

        return parse_manifest(io.BytesIO(bytes(data)))

    def upload(self, name: str, stream: IO[bytes], content_type: str | None = None) -> str:
        """Used by the upload endpoint: write into this source's container/prefix."""
        from azure.storage.blob import ContentSettings

        blob_name = f"{self.prefix}{name}"
        self._cc.upload_blob(blob_name, stream, overwrite=True,
                             content_settings=ContentSettings(content_type=content_type) if content_type else None)
        return blob_name
