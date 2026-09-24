"""Virtual source for documents uploaded through the API (POST /api/uploads).

Uploads are staged straight into the raw store by the API and submitted one at a time on the PRIORITY lane,
so interactive uploads are never stuck behind a bulk backfill. There is nothing to list.
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


class UploadSettings(BaseModel):
    allowed_roles: list[str] = ["admin", "contributor"]


@SOURCES.register("upload", config=UploadSettings, description="Documents uploaded via the API (priority lane).")
class UploadSource(DocumentSource):
    staging_required = False

    def __init__(self, config: SourceConfig, settings: UploadSettings, **_: Any) -> None:
        super().__init__(config)
        self.settings = settings

    def iter_items(self) -> Iterator[SourceItem]:
        return iter(())

    def open(self, item: SourceItem) -> IO[bytes]:
        raise NotSupported("upload items are read from the raw store")
