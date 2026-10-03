"""FastAPI application factory. OpenAPI docs: /api/docs (Swagger UI) and /api/openapi.json."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rag_os import __doc__ as pkg_doc
from rag_os.api.errors import install_error_handlers
from rag_os.api.middleware import CorrelationMiddleware
from rag_os.api.routers import (
    admin_config,
    admin_directory,
    admin_ingestion,
    admin_traces,
    chat,
    dev,
    health,
    uploads,
)
from rag_os.application.use_cases.expectations import maintenance_tick
from rag_os.composition import Container
from rag_os.infrastructure.settings import Settings, get_settings
from rag_os.infrastructure.telemetry import record_expectation, setup_telemetry

log = logging.getLogger("rag_os.api")
CONFIG_REFRESH_S = 60
MAINTENANCE_S = 300


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Logging first. Container() is the slowest thing in startup - it reads the whole domain config over
        # HTTPS - and configuring telemetry after it meant that phase produced no log line at all: a process
        # stuck loading config looked identical to one that had crashed, with "Waiting for application startup."
        # as the last thing anyone saw. The connection string is only known after Container() resolves secrets,
        # so telemetry is set up twice: locally first, then again with the exporter once it is available.
        setup_telemetry(settings.service_name, settings.log_level, None, False)
        log.info("loading configuration", extra={"config_store": settings.config_store})
        c = container or Container(settings)
        setup_telemetry(settings.service_name, settings.log_level, c.settings.applicationinsights_connection_string,
                        settings.otel_enabled)
        app.state.container = c
        log.info("api starting", extra={"index": c.index_name, "profile_fp": c.guard.fp,
                                        "llm_answer": c.settings.llm_answer, "search": c.settings.search_backend,
                                        "azure_client_id": c.settings.azure_client_id or ""})
        if c.settings.uses_azure_services and not c.settings.azure_client_id:
            # DefaultAzureCredential would fall through to a system-assigned identity, and no workload has one.
            log.warning("AZURE_CLIENT_ID is not set: Azure calls will fail with 403 rather than authenticate as "
                        "the user-assigned managed identity. 07-container-apps.ps1 normally sets it.")

        async def _refresh() -> None:
            while True:
                await asyncio.sleep(CONFIG_REFRESH_S)
                try:
                    await c.reload_config()
                except Exception:
                    log.exception("configuration refresh failed")

        async def _maintain() -> None:
            # Trace retention and scheduled expectation replays. Here rather than in the scheduler job, which
            # holds no LLM credentials; expectations are claimed in the database, so replicas never double up.
            s = c.settings
            if not s.query_trace_enabled:
                return
            while True:
                await asyncio.sleep(MAINTENANCE_S)
                try:
                    out = await maintenance_tick(c.traces, c.expectations, retention_days=s.query_trace_retention_days,
                                                 replay_hours=s.query_expectation_replay_hours)
                    for _ in range(out.get("passed", 0)):
                        record_expectation("pass")
                    for _ in range(out.get("failed", 0)):
                        record_expectation("fail")
                    if any(out.values()):
                        log.info("query trace maintenance", extra=out)
                except Exception:
                    log.exception("query trace maintenance failed")

        task = asyncio.create_task(_refresh())
        maintenance = asyncio.create_task(_maintain())
        try:
            yield
        finally:
            task.cancel()
            maintenance.cancel()
            await c.aclose()

    app = FastAPI(
        title="RAG-OS Knowledge Assistant API",
        version="0.1.0",
        description=(pkg_doc or "") + "\n\nPermission-aware answers grounded in your documents. "
        "Authenticate with `Authorization: Bearer <JWT>` (Microsoft Entra ID, or a dev token locally).",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.embed_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID", "If-Match"],
        expose_headers=["X-Correlation-ID", "ETag"],
        max_age=600,
    )
    app.add_middleware(CorrelationMiddleware)
    install_error_handlers(app)
    for r in (health.router, chat.router, uploads.router, admin_ingestion.router, admin_config.router,
              admin_directory.router, admin_traces.router):
        app.include_router(r)
    if settings.dev_auth_enabled:
        app.include_router(dev.router)
    return app
