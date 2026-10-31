---
name: fastapi-errors-observability
description: Makes FastAPI errors consistent and the service observable - HTTPException versus domain exceptions, exception handlers, a problem-details error body, custom validation error responses, a catch-all 500 handler that leaks nothing, structured JSON logging with a request id, FastAPI's native OpenTelemetry traces, metrics and logs, Sentry, and liveness and readiness health checks. Use when designing error responses, adding logging or tracing, connecting a monitoring service, debugging production errors, or adding health endpoints for Kubernetes or a load balancer.
metadata:
  tags:
  - fastapi
  - errors
  - exception-handlers
  - logging
  - opentelemetry
  - sentry
  - health-checks
  - observability
---

# FastAPI errors, logging and observability

This skill gives a FastAPI service one error format, logs that can be searched by request, and traces and metrics
through FastAPI's built-in OpenTelemetry support (0.142+). Verified on FastAPI 0.162 and Starlette 1.10.

## Step 0: installed versions for errors and telemetry

Read the versions first, because the telemetry options are recent. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastapi|starlette|sentry-sdk|structlog|opentelemetry-sdk) '
```

Native OpenTelemetry needs FastAPI 0.142, and exporting became opt-in in 0.143. Header names in validation errors need
0.152. Below 0.142 keep your existing instrumentation until you upgrade.

## Choose how each failure is answered

Each kind of failure has one place that handles it, so the client always gets the same body.

| Failure | Handled by | Why the others lose |
|---|---|---|
| invalid input | a `RequestValidationError` handler, 422 | the default body echoes `input`, which can hold a password |
| a business rule (not found, conflict) | a domain exception and one `exception_handler` | `HTTPException` in a service breaks when a worker calls it |
| an HTTP concern in a router or dependency | `HTTPException` | a domain class for one line of routing is ceremony |
| an unknown route or method | a `starlette.exceptions.HTTPException` handler | FastAPI's own `HTTPException` handler misses these |
| anything unexpected in a route or dependency | a pure ASGI catch-all, added first | an `Exception` handler runs outside your middleware, with no request id and no CORS headers |
| a crash in an outer middleware | `ServerErrorMiddleware`, plain text | nothing inside it can run; keep outer layers small |

## One error body for the whole API

Clients handle errors best when every error has the same shape. A small problem-details body (after RFC 9457) works
for validation errors, domain errors and unexpected failures.

```python
from pydantic import BaseModel


class Problem(BaseModel):
    type: str = "about:blank"
    title: str                 # short, stable, for people: "Item not found"
    status: int
    detail: str | None = None  # this occurrence, safe for the client
    code: str | None = None    # machine-readable: "item_not_found"
    errors: list[dict[str, object]] | None = None
```

Document it on routers with `responses={404: {"model": Problem}, 409: {"model": Problem}}` so generated clients know
the shape.

## HTTPException and domain exceptions

`HTTPException` is for code that is about HTTP (routers, dependencies). Services raise domain exceptions, and one
handler maps each to a status code, so the same service works in a worker or a script.

```python
class DomainError(Exception):
    status = 400
    code = "domain_error"
    title = "Request could not be processed"


class ItemNotFound(DomainError):
    status, code, title = 404, "item_not_found", "Item not found"


class ItemTitleTaken(DomainError):
    status, code, title = 409, "item_title_taken", "Title already used"


@app.exception_handler(DomainError)
async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
    body = Problem(title=exc.title, status=exc.status, code=exc.code)
    return JSONResponse(body.model_dump(exclude_none=True), status_code=exc.status, media_type="application/problem+json")
```

One handler on the base class covers every subclass; add a subclass per new error.

## Custom validation error responses

FastAPI answers invalid input with 422 and a `detail` list. Override `RequestValidationError` to fit the error body,
keeping the field location and message but dropping the echoed input, which can contain passwords.

```python
from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors: list[dict[str, object]] = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    body = Problem(title="Validation failed", status=422, code="validation_failed", errors=errors)
    return JSONResponse(body.model_dump(exclude_none=True), status_code=422, media_type="application/problem+json")
```

Since FastAPI 0.152, validation errors of header parameter models report the HTTP header name (`x-request-id`), not
the Python field name.

## A catch-all 500 middleware that leaks nothing

An unexpected exception must produce a generic 500 for the client and a full log entry for you. An
`@app.exception_handler(Exception)` handler cannot do it: Starlette runs that handler in `ServerErrorMiddleware`,
outside all user middleware, so the response has no `X-Request-Id` and no CORS headers, and its log line shows the
request id as `-`. Use a pure ASGI catch-all, added first so it is the innermost layer.

```python
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app")


class CatchAllMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def watch(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, watch)
        except Exception:
            logger.exception("unhandled error", extra={"method": scope["method"], "path": scope["path"]})
            if not started:                           # once headers are out, a second response is impossible
                body = Problem(title="Internal Server Error", status=500, code="internal_error")
                content = body.model_dump(exclude_none=True)
                await JSONResponse(content, status_code=500, media_type="application/problem+json")(scope, receive, send)
            raise                                     # Uvicorn, Sentry and FastAPI's telemetry still see it


app.add_middleware(CatchAllMiddleware)                # first: innermost, inside everything that adds headers
app.add_middleware(RequestIdMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins)   # last: outermost
```

- The `raise` at the end keeps Starlette's behaviour for unhandled errors: the server logs the exception, FastAPI's
  OpenTelemetry records it, and `TestClient` re-raises it in tests unless `raise_server_exceptions=False`
  (`httpx.ASGITransport(app=app, raise_app_exceptions=False)` for the async client). Verified on Starlette 1.10.
- Never return `str(exc)`, a traceback, SQL or a file path. The `X-Request-Id` header on the 500 gives the user an id
  to quote to support.

## Structured logging with a request id

JSON logs with a request id on every line let you follow one request across workers and services. A pure ASGI
middleware sets the id in a `contextvars.ContextVar`; a logging filter adds it to each record.

```python
import contextvars
import json
import logging
import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
SAFE_REQUEST_ID = re.compile(rb"[A-Za-z0-9._-]{1,64}")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"")      # raw bytes: a client may send anything
        rid = incoming.decode("ascii") if SAFE_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id.set(rid)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id.get(),
        }
        for key in ("method", "path", "status", "duration_ms", "user_id", "item_id"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for noisy in ("httpx", "httpx2", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
```

- Log ids, counts, durations and outcomes; never passwords, tokens, `Authorization` headers or request bodies.
- Do not enable `tracebacks_show_locals` or similar in production: local variables in tracebacks often hold secrets.
- structlog 26.1 is a good alternative to a hand-written formatter, with processors for redaction.
- Check an incoming `X-Request-Id` before echoing it: `SAFE_REQUEST_ID` tests the raw bytes for length and characters,
  so a CRLF, a space or non-UTF-8 bytes get a fresh id instead of a header injection or a `UnicodeDecodeError`.

## Native OpenTelemetry

FastAPI 0.142+ records OpenTelemetry traces, metrics and logs for HTTP requests (and traces and logs for WebSockets)
without extra instrumentation code. Since 0.143 exporting is opt-in.

```bash
uv add "fastapi[standard]"                 # includes the OpenTelemetry SDK and the OTLP HTTP exporter
export FASTAPI_OTEL_AUTO_CONFIGURE=true
export OTEL_SERVICE_NAME=items-api
export OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector.example.com    # traces to /v1/traces, metrics, logs
export OTEL_EXPORTER_OTLP_HEADERS="api-key=YOUR_API_KEY"
uv run fastapi run
```

```python
app = FastAPI(
    telemetry={
        "exclude": lambda scope: scope["path"] in {"/health", "/ready"},   # skip probes
        "operation_spans": True,      # spans for dependencies, the endpoint, serialization, background tasks
        "logs": True,                 # unhandled exceptions and validation failures as OTel logs
    }
)
```

- Spans cover dependency resolution, the endpoint, response serialization and each `BackgroundTasks` task.
- Unhandled exceptions are logged with message and stack trace, which may contain sensitive data; redact with a log
  processor or set `"logs": False`.
- If another library already configures OpenTelemetry providers, FastAPI uses them; leave
  `FASTAPI_OTEL_AUTO_CONFIGURE` unset to avoid duplicate exports.
- Database spans need SQLAlchemy instrumentation (`opentelemetry-instrumentation-sqlalchemy`) on the engine's
  `sync_engine`.

## Sentry for error tracking

Sentry groups exceptions, shows the request and the trace, and alerts on new errors. `sentry-sdk` detects FastAPI and
Starlette automatically.

```python
import sentry_sdk

sentry_sdk.init(
    dsn=settings.sentry_dsn.get_secret_value() if settings.sentry_dsn else None,
    environment=settings.environment,
    traces_sample_rate=0.1,
    send_default_pii=False,          # no cookies, headers with auth, or user IPs
)
```

Call `init` before creating the app (at the top of `main.py`). `uv add "sentry-sdk[fastapi]"` (2.73 on 2026-10-28).

## Liveness and readiness checks

A liveness check says the process is running; a readiness check says it can serve traffic now. Keep them separate:
a database outage should take pods out of the load balancer (readiness), not restart them (liveness).

```python
from sqlalchemy import text


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}                       # no dependencies: cheap and always true while the loop runs


@app.get("/ready", include_in_schema=False)
async def ready(session: SessionDep) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.warning("readiness check failed", exc_info=True)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})
```

## Verify errors and request ids

These checks prove the 500 keeps its headers and hides the exception, and that a hostile `X-Request-Id` never comes
back. The tests are in [references/error-tests.md](references/error-tests.md); then check the running service:

```bash
uv run pytest -k "error or request_id"
curl -si -H 'X-Request-Id: abc-123' http://127.0.0.1:8000/health | grep -i '^x-request-id: abc-123'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/ready   # 200, or 503 with the database stopped
```

## Error handling and logging anti-patterns

Each of these either leaks detail to the client or loses it from the logs.

| Do not | Do instead |
|---|---|
| `@app.exception_handler(Exception)` as the catch-all | pure ASGI middleware added first |
| `str(exc)` or a traceback in a response | a generic body, and the exception in the log with the request id |
| `.decode()` on a raw header | test the bytes against a pattern, then decode |
| echoing `input` in validation errors | keep `loc`, `msg` and `type` only |
| `BaseHTTPMiddleware` for request logging | pure ASGI middleware |
| `/health` that checks the database | `/health` with no dependencies, `/ready` for the database |
| `send_default_pii=True` | leave it `False` |

## Error handling and observability checklist

Go through this error handling, logging and tracing checklist before merging a change.

```text
[ ] one error body (problem details) for domain, validation and unexpected errors
[ ] services raise domain exceptions; one handler maps them to status codes
[ ] validation errors keep loc and msg, never echo the input
[ ] pure ASGI catch-all added first: generic 500 with request id and CORS headers, exception logged with the id
[ ] JSON logs with a request id; noisy loggers at WARNING; no secrets or bodies in logs
[ ] OpenTelemetry exporting enabled on purpose; health checks excluded; exception logs redacted
[ ] Sentry (or similar) initialized before the app with send_default_pii=False
[ ] /health without dependencies, /ready checking the database
```

## Common error handling, logging and tracing problems

These are the error handling, logging and tracing problems people hit most, each with its symptom, cause and fix.

1. **The same traceback appears twice: once with the request id, once from Uvicorn as
   `Exception in ASGI application`.** Expected: the catch-all raises again after it answers, and so does Starlette
   after an `Exception` handler. To keep only your line, write `except Exception as exc:` in the catch-all, add
   `exc.add_note(LOGGED)` right after `logger.exception(...)`, and filter marked errors out of `uvicorn.error`. A crash
   in an outer layer carries no note, so its traceback stays in the log.

   ```python
   LOGGED = "logged by CatchAllMiddleware"


   def drop_logged(record: logging.LogRecord) -> bool:
       exc = record.exc_info[1] if record.exc_info else None
       return LOGGED not in getattr(exc, "__notes__", ())


   logging.getLogger("uvicorn.error").addFilter(drop_logged)               # in configure_logging()
   ```

2. **`HTTPException` from Starlette is not caught by your FastAPI handler.** Register handlers for
   `starlette.exceptions.HTTPException`, which FastAPI's `HTTPException` subclasses, to also catch 404s for unknown
   routes.
3. **Telemetry does nothing after upgrading to 0.143.** Automatic exporter setup became opt-in. Set
   `FASTAPI_OTEL_AUTO_CONFIGURE=true` or `telemetry={"auto_configure": True}`.
4. **Request id missing in some log lines.** Lines logged from threads started by hand or from other processes do not
   inherit the context variable. `asyncio.to_thread` and FastAPI's thread pool copy the context; raw
   `threading.Thread` does not.
5. **A log line from `@app.middleware("http")` shows the default for a value the endpoint set in a `ContextVar`.**
   `BaseHTTPMiddleware` runs the endpoint in its own task, so the change never reaches the middleware. It also wraps
   the response stream and has had bugs with background tasks and streaming; pure ASGI middleware as shown avoids all
   of this.

## See also

- fastapi-middleware: middleware order and pure ASGI classes.
- fastapi-repository-pattern: domain exceptions raised by services.
- fastapi-openapi: documenting the error body.
- fastapi-deployment: probes behind a load balancer.
- fastapi-security: what must never appear in a log.
