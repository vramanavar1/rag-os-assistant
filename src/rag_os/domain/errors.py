"""Domain errors. The API layer maps these to problem+json responses."""

from __future__ import annotations


class RagOsError(Exception):
    """Base class for all expected (non-bug) failures."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class ConfigError(RagOsError):
    status_code = 500
    code = "config_error"


class ValidationFailed(RagOsError):
    status_code = 422
    code = "validation_failed"


class AuthenticationFailed(RagOsError):
    status_code = 401
    code = "authentication_failed"


class AccessDenied(RagOsError):
    status_code = 403
    code = "access_denied"


class NotFound(RagOsError):
    status_code = 404
    code = "not_found"


class Conflict(RagOsError):
    status_code = 409
    code = "conflict"


class ProfileMismatch(RagOsError):
    """Embedding model / index profile disagree. Never silently fall back."""

    status_code = 503
    code = "embedding_profile_mismatch"


class DependencyUnavailable(RagOsError):
    status_code = 503
    code = "dependency_unavailable"


class NotSupported(RagOsError):
    status_code = 501
    code = "not_supported"


class ParseError(RagOsError):
    status_code = 422
    code = "parse_error"
