"""Generic factory registries (Factory + Registry pattern).

Adapters register themselves with a decorator; the composition root creates them by *name* from
configuration. Adding a new adapter (e.g. a new source type) never requires editing callers.

    @SOURCES.register("azure_blob", config=AzureBlobSettings)
    class AzureBlobSource(DocumentSource): ...

    source = SOURCES.create("azure_blob", source_config, secrets=resolver)
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from rag_os.domain.errors import ConfigError


@dataclass(frozen=True)
class Registration[T]:
    name: str
    factory: Callable[..., T]
    config_model: type[BaseModel] | None
    description: str
    stub: bool


class Registry[T]:
    def __init__(self, kind: str, autoload: list[str] | None = None) -> None:
        self.kind = kind
        self._items: dict[str, Registration[T]] = {}
        self._autoload = autoload or []
        self._loaded = False

    def register(
        self,
        name: str,
        *,
        config: type[BaseModel] | None = None,
        description: str = "",
        stub: bool = False,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        def deco(factory: Callable[..., T]) -> Callable[..., T]:
            if name in self._items and self._items[name].factory is not factory:
                raise ConfigError(f"{self.kind} '{name}' registered twice")
            self._items[name] = Registration(name, factory, config, description or (factory.__doc__ or ""), stub)
            return factory

        return deco

    def _ensure_loaded(self) -> None:
        # Import adapter modules lazily so their @register decorators run.
        if self._loaded:
            return
        for mod in self._autoload:
            importlib.import_module(mod)
        self._loaded = True

    def names(self) -> list[str]:
        self._ensure_loaded()
        return sorted(self._items)

    def get(self, name: str) -> Registration[T]:
        self._ensure_loaded()
        try:
            return self._items[name]
        except KeyError:
            raise ConfigError(
                f"unknown {self.kind} '{name}'", detail={"available": self.names()}
            ) from None

    def validate_settings(self, name: str, settings: dict[str, Any]) -> BaseModel | None:
        reg = self.get(name)
        if reg.config_model is None:
            return None
        try:
            return reg.config_model.model_validate(settings)
        except ValidationError as e:
            raise ConfigError(
                f"invalid settings for {self.kind} '{name}'",
                detail={"errors": e.errors(include_url=False, include_context=False)},
            ) from e

    def create(self, name: str, *args: Any, **kwargs: Any) -> T:
        return self.get(name).factory(*args, **kwargs)

    def describe(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        return [
            {
                "name": r.name,
                "stub": r.stub,
                "description": " ".join(r.description.split())[:200],
                "settings_schema": r.config_model.model_json_schema() if r.config_model else None,
            }
            for r in self._items.values()
        ]


_I = "rag_os.infrastructure"

SOURCES: Registry[Any] = Registry(
    "source",
    [f"{_I}.sources.local_folder", f"{_I}.sources.azure_blob", f"{_I}.sources.upload", f"{_I}.sources.stubs"],
)
PARSERS: Registry[Any] = Registry("parser", [f"{_I}.parsers"])
EMBEDDERS: Registry[Any] = Registry("embedding provider", [f"{_I}.embeddings.openai_compatible", f"{_I}.embeddings.fake"])
LLMS: Registry[Any] = Registry("llm provider", [f"{_I}.llm.azure_openai", f"{_I}.llm.claude_foundry", f"{_I}.llm.fake"])
SEARCH_INDEXES: Registry[Any] = Registry("search index", [f"{_I}.search.azure_search", f"{_I}.search.in_memory"])
QUEUES: Registry[Any] = Registry(
    "queue", [f"{_I}.queue.in_memory", f"{_I}.queue.sql_queue", f"{_I}.queue.servicebus"]
)
RAW_STORES: Registry[Any] = Registry("raw store", [f"{_I}.storage.raw_store"])
CONFIG_REPOS: Registry[Any] = Registry("config repository", [f"{_I}.storage.config_repo"])
RETRIEVERS: Registry[Any] = Registry("retriever", [f"{_I}.search.retrievers"])
CLASSIFIERS: Registry[Any] = Registry("classifier", [f"{_I}.classifier.classifiers"])
# Deliberately NOT surfaced by GET /api/admin/registry: a directory is chosen by environment variable for
# the whole deployment, and that endpoint describes adapters a source can be configured with.
DIRECTORIES: Registry[Any] = Registry("identity directory", [f"{_I}.directory.graph", f"{_I}.directory.fake"])
