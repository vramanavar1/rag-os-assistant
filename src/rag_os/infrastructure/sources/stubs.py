"""Registered-but-not-yet-implemented source types.

Their settings are validated (so configuration can be prepared now) and they fail with a clear
NotSupported error at sync time. Implementing one means filling in iter_items/open - nothing else changes.
Recommended implementation for SharePoint: Azure AI Search indexed SharePoint knowledge source (ACL sync).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import IO, Any

from pydantic import BaseModel

from rag_os.application.ports import DocumentSource
from rag_os.domain.documents import SourceItem
from rag_os.domain.errors import NotSupported
from rag_os.domain.ingestion import SourceConfig
from rag_os.infrastructure.registry import SOURCES


class SharePointSettings(BaseModel):
    site_url: str
    library: str = "Documents"
    folder: str = ""
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # kv://...  (or use managed identity + Sites.Selected)


class GoogleDriveSettings(BaseModel):
    folder_id: str
    service_account_json: str  # kv://...
    include_shared_drives: bool = True


class FtpSettings(BaseModel):
    host: str
    port: int = 22
    protocol: str = "sftp"  # sftp | ftps
    username: str
    password: str | None = None  # kv://...
    private_key: str | None = None  # kv://...
    root: str = "/"


class _StubSource(DocumentSource):
    kind = "stub"

    def __init__(self, config: SourceConfig, settings: BaseModel, **_: Any) -> None:
        super().__init__(config)
        self.settings = settings

    def iter_items(self) -> Iterator[SourceItem]:
        raise NotSupported(f"source type '{self.config.type}' is registered but not implemented yet (see roadmap)")

    def open(self, item: SourceItem) -> IO[bytes]:
        raise NotSupported(f"source type '{self.config.type}' is not implemented yet")

    def healthcheck(self) -> None:
        raise NotSupported(f"source type '{self.config.type}' is not implemented yet")


@SOURCES.register("sharepoint", config=SharePointSettings, stub=True, description="SharePoint Online library (planned).")
class SharePointSource(_StubSource):
    pass


@SOURCES.register("google_drive", config=GoogleDriveSettings, stub=True, description="Google Drive folder (planned).")
class GoogleDriveSource(_StubSource):
    pass


@SOURCES.register("ftp", config=FtpSettings, stub=True, description="SFTP/FTPS server (planned).")
class FtpSource(_StubSource):
    pass
