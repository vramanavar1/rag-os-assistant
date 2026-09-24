"""Correlation-id + security-header middleware (pure ASGI, streaming-safe)."""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rag_os.infrastructure.telemetry import correlation_id_var

log = logging.getLogger("rag_os.access")
_VALID_CID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cache-control", b"no-store"),
]


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        cid = headers.get("x-correlation-id", "")
        if not _VALID_CID.match(cid):
            cid = uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        t0 = time.perf_counter()
        status: dict[str, Any] = {"code": 500}
        path = scope.get("path", "")
        docs = path.startswith(("/api/docs", "/api/openapi", "/api/redoc"))

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                hdrs = list(message.get("headers", []))
                hdrs.append((b"x-correlation-id", cid.encode()))
                existing = {k.lower() for k, _ in hdrs}
                for k, v in SECURITY_HEADERS:
                    if docs and k in (b"content-security-policy", b"x-frame-options"):
                        continue  # Swagger UI needs scripts/styles
                    if k not in existing:
                        hdrs.append((k, v))
                message["headers"] = hdrs
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            if not path.endswith(("/healthz", "/readyz")):
                log.info("request", extra={"method": scope.get("method"), "path": path, "status": status["code"],
                                           "duration_ms": round((time.perf_counter() - t0) * 1000, 1)})
            correlation_id_var.reset(token)
