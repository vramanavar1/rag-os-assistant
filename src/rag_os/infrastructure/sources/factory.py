"""SourceFactory: SourceConfig (from sources.yaml) -> DocumentSource via the SOURCES registry."""

from __future__ import annotations

from typing import Any

from rag_os.application.ports import DocumentSource, SecretResolver
from rag_os.domain.ingestion import SourceConfig
from rag_os.infrastructure.registry import SOURCES


def _resolve(value: Any, secrets: SecretResolver | None) -> Any:
    if isinstance(value, str) and value.startswith("kv://") and secrets is not None:
        return secrets.resolve(value)
    if isinstance(value, dict):
        return {k: _resolve(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, secrets) for v in value]
    return value


class SourceFactory:
    def __init__(self, secrets: SecretResolver | None, app_settings: Any = None) -> None:
        self.secrets = secrets
        self.app = app_settings

    def validate(self, cfg: SourceConfig) -> None:
        """Validate type + settings WITHOUT resolving secrets or connecting (used by config admin)."""
        SOURCES.validate_settings(cfg.type, cfg.settings)

    def create(self, cfg: SourceConfig) -> DocumentSource:
        settings = SOURCES.validate_settings(cfg.type, _resolve(cfg.settings, self.secrets))
        return SOURCES.create(cfg.type, cfg, settings, app=self.app)  # type: ignore[no-any-return]

    @staticmethod
    def available() -> list[dict[str, Any]]:
        return SOURCES.describe()
