# Runbook: Observability (OpenTelemetry + Structured Logging)

## What is instrumented

- **Structured JSON logs** (`app/observability.py::JsonFormatter`) for all
  `logging` output at `LOG_LEVEL` (default `INFO`). Every request is logged by
  `RequestContextMiddleware` with `method`, `path`, `status`, `duration_ms`, and
  the request's `request_id`.
- **Request IDs**: each request gets an `X-Request-Id` (incoming value is echoed,
  otherwise a UUID is generated). It is propagated to log lines via a
  `contextvars.ContextVar`, so correlated log lines are greppable.
- **OpenTelemetry tracing** (opt-in): when `OTEL_ENABLED=1`, FastAPI routes are
  instrumented via `FastAPIInstrumentor` and spans are exported over OTLP/HTTP to
  `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4318/v1/traces`).
  Service name: `ai-customer-support`.

## Quick start (local)

```powershell
# 1. Run any OTLP/HTTP collector, e.g. Jaeger or the OTel Collector:
docker run -d --name jaeger -p 16686:16686 -p 4318:4318 jaegertracing/jaeger
# 2. Point the app at it:
$env:OTEL_ENABLED="1"
$env:OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318/v1/traces"
.\.venv\Scripts\python.exe -m uvicorn app.main:app
# 3. Browse http://localhost:16686 for the `ai-customer-support` service.
```

## Production checklist

- Collect structured logs with a shipping agent (OpenTelemetry Collector,
  Fluent Bit, etc.) into a central store (Elasticsearch/Loki/CloudWatch).
- Run a Collector close to the app; use compression + batching; do not let a
  down collector block or crash the API (batchers drop spans, logs always flow).
- Trace into async/worker paths: `enqueue_notification` (Celery) inherits the
  `traceparent` header when `OTEL_PROPAGATORS` includes tracecontext; instrument
  Celery worker tasks with `opentelemetry-instrumentation-celery`.
- Alert on error-rate/latency SLOs from the trace/log pipeline, not on the app.
- Keep `X-Request-Id` in response headers so customers/ops can correlate.