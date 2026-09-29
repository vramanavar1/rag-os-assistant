"""Structured logging, correlation ids, OpenTelemetry (Azure Monitor) and metrics."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)

_STD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}
_tracer: Any = None
_meter: Any = None
_instruments: dict[str, Any] = {}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "msg": record.getMessage(),
        }
        cid = correlation_id_var.get()
        if cid:
            payload["correlation_id"] = cid
        for k, v in record.__dict__.items():
            if k not in _STD and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        cid = correlation_id_var.get()
        if cid and not hasattr(record, "correlation_id"):
            record.correlation_id = cid
        return True


def setup_telemetry(service: str, level: str = "INFO", connection_string: str | None = None,
                    enabled: bool = True) -> None:
    """Configure JSON logs to stdout and (when a connection string is present) Azure Monitor OTel export."""
    global _tracer, _meter
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter(service))
    h.addFilter(_CorrelationFilter())
    root.addHandler(h)
    root.setLevel(level.upper())
    for noisy in ("azure.core.pipeline.policies.http_logging_policy", "azure.identity", "httpx", "uvicorn.access",
                  "azure.servicebus", "azure.monitor"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if enabled and connection_string:
        try:
            import os

            from azure.monitor.opentelemetry import configure_azure_monitor

            os.environ.setdefault("OTEL_SERVICE_NAME", service)  # cloud role name in App Insights
            configure_azure_monitor(connection_string=connection_string, logger_name="rag_os")
        except Exception as e:
            logging.getLogger(__name__).warning("Azure Monitor exporter not configured", extra={"error": str(e)})
    try:
        from opentelemetry import metrics, trace

        _tracer = trace.get_tracer("rag_os")
        _meter = metrics.get_meter("rag_os")
        _instruments["tokens"] = _meter.create_counter("rag.tokens", unit="{token}", description="LLM/embedding tokens")
        _instruments["stage"] = _meter.create_histogram("rag.stage.duration", unit="ms", description="Stage latency")
        _instruments["docs"] = _meter.create_counter("rag.ingest.docs", unit="{document}", description="Ingested docs")
        _instruments["chunks"] = _meter.create_counter("rag.ingest.chunks", unit="{chunk}",
                                                       description="Chunks indexed (what consumes index quota)")
    except Exception:  # noqa: S110
        pass


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """OpenTelemetry span + latency histogram. No-op when OTel isn't configured."""
    t0 = time.perf_counter()
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, str | int | float | bool) else str(v))
        cid = correlation_id_var.get()
        if cid:
            s.set_attribute("rag.correlation_id", cid)
        try:
            yield s
        finally:
            if "stage" in _instruments:
                _instruments["stage"].record((time.perf_counter() - t0) * 1000, {"stage": name})


def record_tokens(usage: Any, provider: str, model: str, purpose: str = "answer") -> None:
    c = _instruments.get("tokens")
    if c is None or usage is None:
        return
    buckets = getattr(usage, "by_purpose", None) or {purpose: {
        k: int(getattr(usage, k, 0) or 0) for k in ("input", "output", "cache_read", "cache_write", "embedding")}}
    for p, bucket in buckets.items():
        for kind, n in bucket.items():
            if n:
                c.add(int(n), {"kind": kind, "provider": provider, "model": model, "purpose": p})


def record_ingest(status: str, source_id: str, chunks: int = 0, reused: bool = False) -> None:
    """One document ingested, and what it actually cost.

    The document counter alone made duplicate work invisible: a corpus with every file uploaded twice reported
    twice the documents and no extra cost at all, while paying for two parses, two embeddings and two sets of
    vectors. `chunks` is the unit that consumes index quota, and `reused` separates documents whose content was
    already indexed from those that were embedded from scratch - the ratio between them is how you tell whether
    de-duplication is working.
    """
    c = _instruments.get("docs")
    if c is not None:
        c.add(1, {"status": status, "source_id": source_id, "reused": str(reused).lower()})
    ch = _instruments.get("chunks")
    if ch is not None and chunks:
        ch.add(chunks, {"source_id": source_id, "reused": str(reused).lower()})
