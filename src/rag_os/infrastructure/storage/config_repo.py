"""Versioned domain configuration: sources, access policy, facets, path rules, embedding profiles.

Filesystem (``CONFIG_DIR``) for local runs; Blob container (``CONFIG_CONTAINER``) in Azure. Every write
validates against the domain model, uses optimistic concurrency (ETag / If-Match) and keeps history.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from rag_os.application.ports import ConfigRepository
from rag_os.domain.access import AccessPolicy
from rag_os.domain.classification import FacetSchema, PathRules
from rag_os.domain.embedding import EmbeddingProfile
from rag_os.domain.errors import ConfigError, Conflict, NotFound, ValidationFailed
from rag_os.domain.ingestion import SourcesFile
from rag_os.infrastructure.registry import CONFIG_REPOS


class EmbeddingProfiles(BaseModel):
    profiles: dict[str, EmbeddingProfile]


class DevPrincipal(BaseModel):
    id: str
    display_name: str
    claims: dict[str, Any] = {}  # raw token claims, in the same shape a real token would carry them
    roles: list[str] = []


class DevPrincipals(BaseModel):
    principals: list[DevPrincipal] = []


KINDS: dict[str, tuple[str, type[BaseModel]]] = {
    "sources": ("sources/sources.yaml", SourcesFile),
    "access-policy": ("access-policy/access-policy.yaml", AccessPolicy),
    "facets": ("classification/facets.yaml", FacetSchema),
    "path-rules": ("classification/path-rules.yaml", PathRules),
    "embedding-profiles": ("embedding/profiles.yaml", EmbeddingProfiles),
    "dev-principals": ("dev/principals.yaml", DevPrincipals),
}


def validate_yaml(kind: str, text: str) -> BaseModel:
    if kind not in KINDS:
        raise NotFound(f"unknown config kind '{kind}'")
    model = KINDS[kind][1]
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValidationFailed(f"{kind}: invalid YAML", detail={"error": str(e)[:500]}) from e
    if kind == "embedding-profiles" and isinstance(data, dict) and "profiles" in data:
        for name, p in (data.get("profiles") or {}).items():
            if isinstance(p, dict):
                p.setdefault("name", name)
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ValidationFailed(f"{kind}: invalid configuration", detail={"errors": e.errors(include_url=False,
                                                          include_context=False)}) from e


def _etag(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class _BaseRepo(ConfigRepository):
    def _read(self, rel: str) -> str: ...

    def _write(self, rel: str, text: str) -> None: ...

    def _load(self, kind: str) -> Any:
        rel, _ = KINDS[kind]
        try:
            text = self._read(rel)
        except FileNotFoundError as e:
            raise ConfigError(f"missing configuration file '{rel}'") from e
        return validate_yaml(kind, text)

    def load_sources(self) -> SourcesFile:
        return self._load("sources")  # type: ignore[no-any-return]

    def load_access_policy(self) -> AccessPolicy:
        return self._load("access-policy")  # type: ignore[no-any-return]

    def load_facets(self) -> FacetSchema:
        return self._load("facets")  # type: ignore[no-any-return]

    def load_path_rules(self) -> PathRules:
        try:
            return self._load("path-rules")  # type: ignore[no-any-return]
        except ConfigError:
            return PathRules()

    def load_embedding_profile(self, name: str) -> EmbeddingProfile:
        profiles: EmbeddingProfiles = self._load("embedding-profiles")
        if name not in profiles.profiles:
            raise ConfigError(f"embedding profile '{name}' not found", detail={"available": sorted(profiles.profiles)})
        return profiles.profiles[name]

    def read_raw(self, kind: str) -> tuple[str, str]:
        if kind not in KINDS:
            raise NotFound(f"unknown config kind '{kind}'")
        try:
            text = self._read(KINDS[kind][0])
        except FileNotFoundError:
            text = ""
        return text, _etag(text)

    def write_raw(self, kind: str, text: str, if_match: str | None) -> str:
        validate_yaml(kind, text)
        current, etag = self.read_raw(kind)
        if if_match is not None and if_match.strip('"') != etag:
            raise Conflict("configuration changed since it was read (ETag mismatch)", detail={"etag": etag})
        rel = KINDS[kind][0]
        if current:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            self._write(f"history/{rel}.{stamp}", current)
        self._write(rel, text)
        return _etag(text)


@CONFIG_REPOS.register("filesystem", description="YAML files under CONFIG_DIR.")
class FileConfigRepository(_BaseRepo):
    def __init__(self, config_dir: str, **_: Any) -> None:
        self.root = Path(config_dir).resolve()

    def _read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def _write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@CONFIG_REPOS.register("blob", description="YAML blobs in the CONFIG_CONTAINER (keyless).")
class BlobConfigRepository(_BaseRepo):
    def __init__(self, account_url: str | None, container: str = "config", connection_string: str | None = None,
                 **_: Any) -> None:
        from azure.storage.blob import ContainerClient

        if connection_string:
            self._cc = ContainerClient.from_connection_string(connection_string, container)
        elif account_url:
            from azure.identity import DefaultAzureCredential

            self._cc = ContainerClient(account_url=account_url, container_name=container,
                                       credential=DefaultAzureCredential())
        else:
            raise ConfigError("BLOB_ACCOUNT_URL is required for CONFIG_STORE=blob")

    def _read(self, rel: str) -> str:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return bytes(self._cc.download_blob(rel).readall()).decode("utf-8")
        except ResourceNotFoundError as e:
            raise FileNotFoundError(rel) from e

    def _write(self, rel: str, text: str) -> None:
        self._cc.upload_blob(rel, text.encode("utf-8"), overwrite=True)
