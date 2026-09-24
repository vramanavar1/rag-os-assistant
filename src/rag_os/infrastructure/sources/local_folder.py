"""Local folder / network share source. Discovery runs where the folder is mounted (admin machine,
file server, or a job with an Azure Files mount) and stages changed files to the raw store."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from pydantic import BaseModel, Field

from rag_os.application.ports import DocumentSource
from rag_os.domain.documents import SourceItem, TagSet
from rag_os.domain.errors import ConfigError
from rag_os.domain.ingestion import SourceConfig
from rag_os.infrastructure.registry import SOURCES
from rag_os.infrastructure.sources.metadata_files import MANIFEST_NAME, SIDECAR_SUFFIX, parse_manifest, parse_sidecar


class LocalFolderSettings(BaseModel):
    root: str
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=lambda: ["**/~$*", "**/.*", "**/Thumbs.db"])
    max_file_mb: int = 200


def _match(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel.lower(), p.lower()) or fnmatch.fnmatchcase(rel.lower(), p.lower().removeprefix("**/"))
               for p in patterns)


@SOURCES.register("local_folder", config=LocalFolderSettings,
                  description="Files on a local disk or mounted share; changed files are staged to the raw store.")
class LocalFolderSource(DocumentSource):
    staging_required = True

    def __init__(self, config: SourceConfig, settings: LocalFolderSettings, **_: Any) -> None:
        super().__init__(config)
        self.settings = settings
        self.root = Path(os.path.expandvars(os.path.expanduser(settings.root))).resolve()

    def healthcheck(self) -> None:
        if not self.root.is_dir():
            raise ConfigError(f"source '{self.id}': folder not found: {self.root}")

    def iter_items(self) -> Iterator[SourceItem]:
        self.healthcheck()
        max_bytes = self.settings.max_file_mb * 1024 * 1024
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                if fn.endswith(SIDECAR_SUFFIX) or fn.lower() == MANIFEST_NAME:
                    continue
                full = Path(dirpath) / fn
                rel = full.relative_to(self.root).as_posix()
                if not _match(rel, self.settings.include) or _match(rel, self.settings.exclude):
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                sidecar = None
                side = full.with_name(fn + SIDECAR_SUFFIX)
                if side.exists():
                    sidecar = parse_sidecar(side.read_bytes())
                yield SourceItem(
                    source_id=self.id,
                    item_id=rel,
                    path=rel,
                    etag=f"{st.st_size}-{st.st_mtime_ns}",
                    size=st.st_size,
                    modified=datetime.fromtimestamp(st.st_mtime, tz=UTC),
                    metadata={"too_large": st.st_size > max_bytes},
                    sidecar=sidecar,
                )

    def open(self, item: SourceItem) -> IO[bytes]:
        path = (self.root / item.item_id).resolve()
        if self.root not in path.parents:
            raise ConfigError("path escapes the source root")
        return open(path, "rb")

    def read_manifest(self) -> dict[str, TagSet]:
        p = self.root / MANIFEST_NAME
        if not p.exists():
            return {}
        with open(p, "rb") as f:
            return parse_manifest(f)
