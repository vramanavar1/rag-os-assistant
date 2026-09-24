"""Secret resolution: ``kv://<name>`` -> Azure Key Vault secret value (keyless, managed identity)."""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from rag_os.domain.errors import ConfigError

log = logging.getLogger(__name__)


class KeyVaultSecretResolver:
    def __init__(self, vault_url: str | None) -> None:
        self._vault_url = vault_url
        self._client = None

    def _client_or_raise(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            if not self._vault_url:
                raise ConfigError("kv:// secret reference used but KEY_VAULT_URL is not set")
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            self._client = SecretClient(vault_url=self._vault_url, credential=DefaultAzureCredential())
        return self._client

    @lru_cache(maxsize=64)  # noqa: B019 - resolver lives for the process lifetime
    def resolve(self, reference: str) -> str:
        if not reference.startswith("kv://"):
            return reference
        name = reference.removeprefix("kv://").strip("/")
        # Local override for offline development: RAGOS_SECRET_<NAME>
        env_override = os.environ.get("RAGOS_SECRET_" + name.upper().replace("-", "_"))
        if env_override:
            return env_override
        try:
            secret = self._client_or_raise().get_secret(name)
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError(f"cannot read Key Vault secret '{name}'", detail={"error": type(e).__name__}) from e
        log.info("resolved Key Vault secret", extra={"secret_name": name})
        return str(secret.value)
