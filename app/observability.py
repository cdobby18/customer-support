import contextvars
import json
import logging
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

SERVICE_NAME = "ai-customer-support"
APP_LOGGER_NAME = "app"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
OTEL_ENABLED = os.getenv("OTEL_ENABLED", "").lower() in {"1", "true", "yes"}
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()

_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def current_request_id() -> str | None:
    return _request_id_ctx.get()


def get_app_logger() -> logging.Logger:
    return logging.getLogger(APP_LOGGER_NAME)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key in ("method", "path", "status", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(LOG_LEVEL)


def setup_tracing(app) -> None:
    if not OTEL_ENABLED:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT or None)
        )
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, logger: logging.Logger | None = None):
        super().__init__(app)
        self.logger = logger or get_app_logger()

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or str(uuid4())
        token = _request_id_ctx.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            _request_id_ctx.reset(token)
        response.headers["X-Request-Id"] = request_id
        self.logger.info(
            "http_request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return response