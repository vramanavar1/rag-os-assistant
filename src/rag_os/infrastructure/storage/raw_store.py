"""Raw document store (claim-check pattern): queue messages carry URIs, never content.

URIs:  ``local://<relative/path>`` (resolved against RAW_DIR; shared docker volume in compose)
       ``https://<account>.blob.core.windows.net/<container>/<blob>`` (keyless read via managed identity)
Staging target is filesystem (dev) or blob (Azure). Reads accept both schemes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote, unquote, urlparse

from rag_os.application.ports import RawDocumentStore, Staged
from rag_os.domain.errors import ConfigError, NotFound, ValidationFailed
from rag_os.infrastructure.registry import RAW_STORES

log = logging.getLogger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9._/ -]+")
_SPOOL_BYTES = 8 * 1024 * 1024


def _safe_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    return (_SAFE.sub("_", name).strip() or "file")[:180]


def _copy_and_hash(src: IO[bytes], dst: IO[bytes]) -> str:
    """Copy while hashing, so the bytes are read once rather than twice."""
    h = hashlib.sha256()
    while chunk := src.read(1024 * 1024):
        h.update(chunk)
        dst.write(chunk)
    return h.hexdigest()


class _BlobAccess:
    def __init__(self, account_url: str | None, connection_string: str | None) -> None:
        self.account_url = account_url
        self.connection_string = connection_string
        self._svc: Any = None
        self._cred: Any = None

    def service(self) -> Any:
        if self._svc is None:
            from azure.storage.blob import BlobServiceClient

            if self.connection_string:
                self._svc = BlobServiceClient.from_connection_string(self.connection_string)
            elif self.account_url:
                from azure.identity import DefaultAzureCredential

                self._cred = DefaultAzureCredential()
                self._svc = BlobServiceClient(account_url=self.account_url, credential=self._cred)
            else:
                raise ConfigError("BLOB_ACCOUNT_URL is not configured")
        return self._svc

    def container_of(self, url: str) -> tuple[str, str]:
        """(host, container) of a blob url, for both Azure (/<container>/..) and azurite (/<account>/<container>/..)."""
        p = urlparse(url)
        parts = unquote(p.path).lstrip("/").split("/")
        container = (parts[1] if len(parts) > 1 else "") if p.port else (parts[0] if parts else "")
        return (p.netloc.lower(), container)

    def blob_from_url(self, url: str) -> Any:
        from azure.storage.blob import BlobClient

        if self.connection_string:
            p = urlparse(url)
            parts = unquote(p.path).lstrip("/").split("/", 2)
            # azurite urls: /<account>/<container>/<blob>
            container, blob = (parts[1], parts[2]) if p.port else (parts[0], "/".join(parts[1:]))
            return self.service().get_blob_client(container, blob)
        if self._cred is None:
            from azure.identity import DefaultAzureCredential

            self._cred = DefaultAzureCredential()
        return BlobClient.from_blob_url(url, credential=self._cred)


class RawStore(RawDocumentStore):
    def __init__(self, *, target: str, raw_dir: str = "./.data/raw", account_url: str | None = None,
                 connection_string: str | None = None, container: str = "raw-docs",
                 exports_container: str = "exports", **_: Any) -> None:
        self.target = target
        self.root = Path(raw_dir).resolve()
        self.container = container
        self.exports_container = exports_container
        self.blob = _BlobAccess(account_url, connection_string)
        if target == "filesystem":
            self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ staging

    def stage(self, source_id: str, doc_id: str, filename: str, stream: IO[bytes],
              content_hash: str | None = None) -> Staged:
        """Copy bytes in, keyed by CONTENT. Identical bytes are stored once, whatever the source or uploader.

        Returns the uri and the sha256, because staging is the one place that necessarily reads every byte -
        so a source that cannot cheaply supply a hash (a local folder, where change detection is otherwise
        size+mtime) gets one for free here.

        `source_id`, `doc_id` and `filename` no longer appear in the key. They identify the DOCUMENT, and two
        documents can hold the same content; the filename lives on the document row, which is also where the
        worker reads it from when choosing a parser (`ProcessItem._parse_and_chunk`).
        """
        digest, spool = content_hash, None
        try:
            if digest is None:
                spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115
                digest = _copy_and_hash(stream, spool)
                spool.seek(0)
                stream = spool  # type: ignore[assignment]
            rel = f"staged/{digest[:2]}/{digest}"
            if self.target == "filesystem":
                dest = (self.root / rel).resolve()
                if self.root not in dest.parents:
                    raise ValidationFailed("invalid staging path")
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Same content already here: nothing to write. This is the whole storage saving, and it makes
                # staging idempotent - a retry after a crash costs nothing.
                if not dest.exists():
                    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
                    with open(tmp, "wb") as f:
                        shutil.copyfileobj(stream, f, 1024 * 1024)
                    os.replace(tmp, dest)  # atomic: a reader never sees a half-written blob
                return Staged(f"local://{rel}", digest)
            bc = self.blob.service().get_blob_client(self.container, rel)
            if not bc.exists():
                bc.upload_blob(stream, overwrite=True, max_concurrency=4)
            return Staged(str(bc.url), digest)
        finally:
            if spool is not None:
                spool.close()

    def owns(self, uri: str | None) -> bool:
        """Whether this store staged `uri` - and so may delete it.

        A document from an azure_blob source is read in place: its blob_uri is the CUSTOMER's blob, in the
        source's own container. Purge used to treat any https url as a staged copy and delete it, which removed
        the original file from the source. Only our raw container (on our account) and our local root are ours.
        """
        if not uri:
            return False
        if uri.startswith("local://"):
            path = (self.root / uri.removeprefix("local://")).resolve()
            return self.root in path.parents
        if uri.startswith(("https://", "http://")):
            try:
                ours_host = urlparse(str(self.blob.service().url)).netloc.lower()
            except ConfigError:
                return False
            host, container = self.blob.container_of(uri)
            return host == ours_host and container == self.container
        return False

    def delete(self, uri: str) -> bool:
        """Delete a staged copy. Anything this store did not stage is refused (returns False) - see `owns`."""
        if not self.owns(uri):
            log.warning("refusing to delete a blob this store did not stage", extra={"uri": uri[:120]})
            return False
        if uri.startswith("local://"):
            path = (self.root / uri.removeprefix("local://")).resolve()
            if self.root not in path.parents:
                raise ValidationFailed("invalid local uri")
            if not path.exists():
                return False
            path.unlink()
            return True
        if uri.startswith(("https://", "http://")):
            from azure.core.exceptions import ResourceNotFoundError

            try:
                self.blob.blob_from_url(uri).delete_blob()
            except ResourceNotFoundError:
                return False
            return True
        # file:// is a source reading its own bytes in place; those are not ours to remove.
        raise ValidationFailed(f"refusing to delete a uri this store did not stage: {uri[:20]}")

    def open(self, uri: str) -> IO[bytes]:
        if uri.startswith("local://"):
            path = (self.root / uri.removeprefix("local://")).resolve()
            if self.root not in path.parents:
                raise ValidationFailed("invalid local uri")
            if not path.exists():
                raise NotFound(f"raw document missing: {uri}")
            return open(path, "rb")
        if uri.startswith(("https://", "http://")):
            spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115
            try:
                self.blob.blob_from_url(uri).download_blob(max_concurrency=4).readinto(spool)
            except Exception as e:
                spool.close()
                from azure.core.exceptions import ResourceNotFoundError

                if isinstance(e, ResourceNotFoundError):
                    raise NotFound(f"raw document missing: {uri}") from e
                raise
            spool.seek(0)
            return spool  # type: ignore[return-value]
        if uri.startswith("file://"):
            return open(unquote(urlparse(uri).path.lstrip("/") if os.name == "nt" else urlparse(uri).path), "rb")
        raise ValidationFailed(f"unsupported uri scheme: {uri[:20]}")

    def clear_staged(self) -> dict[str, int]:
        """Delete every staged copy and every export - and nothing else. Used by the full data reset."""
        out = {"staged": 0, "exports": 0}
        if self.target == "filesystem":
            for sub, key in (("staged", "staged"), ("exports", "exports")):
                d = (self.root / sub).resolve()
                if self.root in d.parents and d.exists():
                    out[key] = sum(1 for f in d.rglob("*") if f.is_file())
                    shutil.rmtree(d)
            return out
        svc = self.blob.service()
        for container, key in ((self.container, "staged"), (self.exports_container, "exports")):
            cc = svc.get_container_client(container)
            try:
                names = [b.name for b in cc.list_blobs()]
            except Exception as e:  # a container that was never created holds nothing to clear
                from azure.core.exceptions import ResourceNotFoundError

                if isinstance(e, ResourceNotFoundError):
                    continue
                raise
            for i in range(0, len(names), 256):
                cc.delete_blobs(*names[i:i + 256])
            out[key] = len(names)
        return out

    # ------------------------------------------------------------------ exports

    def put_export(self, name: str, data: bytes, content_type: str) -> str:
        safe = _safe_name(name)
        if self.target == "filesystem":
            dest = self.root / "exports" / safe
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return safe
        from azure.storage.blob import ContentSettings

        bc = self.blob.service().get_blob_client(self.exports_container, safe)
        bc.upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type=content_type))
        return safe

    def open_export(self, name: str) -> IO[bytes]:
        safe = _safe_name(name)
        if self.target == "filesystem":
            p = self.root / "exports" / safe
            if not p.exists():
                raise NotFound("export not found")
            return open(p, "rb")
        return self.open(str(self.blob.service().get_blob_client(self.exports_container, quote(safe)).url))


@RAW_STORES.register("filesystem", description="Local directory (shared docker volume in compose).")
def _fs(**kw: Any) -> RawStore:
    return RawStore(target="filesystem", **kw)


@RAW_STORES.register("blob", description="Azure Blob Storage (keyless).")
def _blob(**kw: Any) -> RawStore:
    return RawStore(target="blob", **kw)
