"""Global error handling: every error is RFC 7807 problem+json carrying the correlation id. No stack traces leak."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DatabaseError
from starlette.exceptions import HTTPException as StarletteHTTPException

from rag_os.domain.errors import RagOsError
from rag_os.infrastructure.telemetry import correlation_id_var

log = logging.getLogger("rag_os.api")


def problem(status: int, title: str, detail: str | None = None, code: str = "error", **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://rag-os/errors/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "correlation_id": correlation_id_var.get(),
        **extra,
    }
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    safe = jsonable_encoder(body, custom_encoder={BaseException: str})
    return JSONResponse(safe, status_code=status, media_type="application/problem+json", headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RagOsError)
    async def _domain(request: Request, exc: RagOsError) -> JSONResponse:
        level = logging.WARNING if exc.status_code < 500 else logging.ERROR
        log.log(level, "request failed", extra={"error_code": exc.code, "status": exc.status_code,
                                               "path": request.url.path, "error": exc.message})
        extra = {k: v for k, v in exc.detail.items() if k in ("errors", "reasons", "available", "etag", "doc_id")}
        return problem(exc.status_code, exc.message, None, exc.code, **extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return problem(422, "Request validation failed", None, "validation_failed", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail), None, "http_error")

    @app.exception_handler(DatabaseError)
    async def _database(request: Request, exc: DatabaseError) -> JSONResponse:
        """A schema that does not match this build is an operational state, not a mystery.

        The column list in a query comes from the Table metadata in this build, so a database missing one
        column fails every full-row read of that table. That used to arrive as a bare "Internal server error"
        with nothing pointing at the cause - the whole Upload feature down, and no clue in the response, in
        readyz, or in `doctor`. 503 rather than 500: it is a dependency being in the wrong state, and it is
        fixed by running the migration rather than by changing code.

        Only the exception TYPE and the two revision ids reach the client; the driver's message can carry
        hostnames and identity details, and it is already in the log.
        """
        log.exception("database error", extra={"path": request.url.path})
        if _looks_like_missing_column(exc):
            return problem(503, "The database schema is out of date",
                           "This build expects columns the database does not have. Run the migrations "
                           "(rag-os bootstrap; in Azure ./infra/scripts/08-bootstrap.ps1), then retry. "
                           "GET /api/readyz reports the exact revisions.", "schema_out_of_date")
        return problem(503, "The database is unavailable", "The error has been logged.", "database_error")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return problem(500, "Internal server error", "The error has been logged.", "internal_error")


def _looks_like_missing_column(exc: BaseException) -> bool:
    """Postgres raises ProgrammingError/UndefinedColumn, SQLite OperationalError 'no such column'.

    Matched on the driver's text rather than on a code, because the two backends agree on neither the class
    nor the SQLSTATE. Getting this wrong only costs a less specific message, never a wrong status.
    """
    text = f"{exc}".lower()
    return "undefinedcolumn" in text or "does not exist" in text or "no such column" in text
