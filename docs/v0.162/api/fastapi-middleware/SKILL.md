---
name: fastapi-middleware
description: "Writes and orders FastAPI middleware and mounts sub-applications - middleware versus dependency, the order middleware runs in, pure ASGI middleware, when @app.middleware(\"http\") is fine, built-in CORS, GZip, TrustedHost and session middleware, reading the body in middleware, custom APIRoute classes, and app.mount for static files, a second app or Flask and Django through a2wsgi. Use when adding middleware, when it runs in the wrong order or a header is missing on 500 errors, or when combining FastAPI with another app."
metadata:
  tags:
  - fastapi
  - middleware
  - asgi
  - cors
  - gzip
  - mount
  - sub-applications
  - wsgi
---

# FastAPI middleware and sub-applications

This skill puts cross-cutting code (request ids, timing, compression, CORS, host checks) in the right place and in
the right order, and combines a FastAPI app with other ASGI or WSGI apps. Checked on FastAPI 0.162 and
Starlette 1.10.

## Step 0: installed versions for middleware

Read the versions first. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastapi|starlette|a2wsgi|itsdangerous) '
```

`scope["route"]` needs Starlette 1.7 and `app.frontend()` needs FastAPI 0.138. `SessionMiddleware` will not import
without `itsdangerous`.

## Middleware or dependency

Middleware runs for every request the app receives, including 404s, static files and mounted apps, before routing.
A dependency runs only for the routes that declare it, after routing, with access to path parameters and the
current user. Pick by that difference.

| Need | Use |
|---|---|
| request id, timing, security headers, compression, CORS, host checks | middleware |
| authentication, permissions, rate limits per user or per route, loading a resource | a dependency on the router or route |
| logging that needs the matched route or the user | a dependency, or middleware that reads `scope["route"]` (Starlette 1.7+) after the call |
| changing the request body for one route | a custom `APIRoute` class (below) |

## The order middleware runs in

Each `app.add_middleware()` call wraps everything added before it, so the middleware added last is the outermost:
it sees the request first and the response last.

```python
app.add_middleware(CatchAllMiddleware)                     # innermost: turns a crash into a response (below)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["https://app.example.com"])   # outermost: runs first
```

FastAPI places its own layers around yours: `ServerErrorMiddleware` outside all user middleware, then your
middleware, then `ExceptionMiddleware` (which runs your `HTTPException` and custom exception handlers), then the
router.

- Add `CORSMiddleware` last so it is outermost: then preflight `OPTIONS` requests and every response that passes
  through your middleware get CORS headers, and the browser shows the real error instead of a CORS failure.
- Responses from your exception handlers for `HTTPException` and domain errors pass back through your middleware.
  An unhandled exception does not: `ServerErrorMiddleware` builds that 500 outside your middleware, and an
  `@app.exception_handler(Exception)` handler runs there too, so the response has no request id and no CORS headers,
  and a log line written in that handler shows the request id as `-`.
- Add a pure ASGI catch-all first, so it is innermost and its 500 travels back through every other middleware.
- Middleware is fixed when the app starts; `add_middleware()` after startup raises `RuntimeError`.

### Pure ASGI catch-all for unhandled exceptions

A catch-all middleware answers an unhandled exception from inside your middleware stack. It answers only if no
response has started, then raises again so Uvicorn, Sentry and FastAPI's telemetry still see the exception.

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
                body = {"title": "Internal Server Error", "status": 500}
                await JSONResponse(body, status_code=500)(scope, receive, send)
            raise
```

A crash inside `CORSMiddleware` or another outer layer still reaches `ServerErrorMiddleware`, which answers with a
plain-text `Internal Server Error`. Keep the outer layers small.

## Pure ASGI middleware

A pure ASGI middleware is a class with `__call__(scope, receive, send)`. It passes the response through untouched,
so it is cheap, works with streaming, SSE and WebSockets, and keeps context variables intact. Use it for anything on
every request.

```python
import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class TimingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":                       # pass WebSocket and lifespan through
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("Server-Timing", f"app;dur={(time.perf_counter() - start) * 1000:.1f}")
            await send(message)

        await self.app(scope, receive, send_with_timing)


app.add_middleware(TimingMiddleware)
```

- Always pass through scope types you do not handle (`websocket`, `lifespan`).
- Change response headers in the `http.response.start` message; the body follows in `http.response.body` messages.
- Keyword arguments to `add_middleware(Cls, key=value)` reach `__init__` after `app`.

## @app.middleware("http") for quick work

The decorator form (Starlette's `BaseHTTPMiddleware`) gives a `Request` and a `call_next` that returns a
`Response`. It is short to write, and fine for low-traffic apps and development tools; it costs more per request and
wraps the response body in its own stream.

```python
@app.middleware("http")
async def add_version_header(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    response.headers["X-API-Version"] = "1.4.0"
    return response
```

Prefer pure ASGI middleware when the app streams (SSE, large downloads), when the middleware is on a hot path, or
when it sets a `ContextVar` that later code must read.

## Built-in middleware

Starlette ships middleware that covers the common needs; import them from `fastapi.middleware.*` or
`starlette.middleware.*`.

| Middleware | Use |
|---|---|
| `CORSMiddleware` | browser clients on another origin; list exact origins (see also fastapi-security) |
| `GZipMiddleware(minimum_size=1000)` | compress large JSON when no proxy or CDN does it; Starlette already skips `text/event-stream` |
| `TrustedHostMiddleware(allowed_hosts=["api.example.com"])` | reject requests with a forged `Host` header |
| `HTTPSRedirectMiddleware` | redirect `http` to `https` when no proxy does it |
| `SessionMiddleware(secret_key=...)` | signed cookie sessions for server-rendered pages; needs `itsdangerous` |

Uvicorn's `--proxy-headers` already applies `X-Forwarded-Proto` and `X-Forwarded-For`; do not add a second proxy
headers middleware in the app.

## Reading the request body in middleware

Reading the body in middleware consumes the `receive` stream, so the endpoint then waits for a body that never comes.
Read it, then hand the endpoint a `receive` that replays it. Do this only when you must (signature checks on many
routes); a dependency that reads `await request.body()` is simpler for one route.

```python
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY = 1_048_576                                              # 1 MiB; set it to your largest signed payload


class SignedBodyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return                                            # the client left mid-upload
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY:
                await PlainTextResponse("Request body too large", status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)
        check_signature(scope, body)                              # your check; raise or answer 401 here
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()                                # then wait for http.disconnect

        await self.app(scope, replay, send)
```

The loop counts bytes as they arrive, because `content-length` can lie or be missing on a chunked upload. Past
`MAX_BODY` it answers 413, so a large upload never sits in memory and the endpoint never runs.

## Custom APIRoute classes

A custom `APIRoute` subclass wraps the handler of every route in one router, with the matched route known. Use it
for per-route timing, a decompressed request body, or logging the validation error body for one API.

```python
from fastapi.routing import APIRoute


class TimedRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()
        path = self.path

        async def timed(request: Request) -> Response:
            start = time.perf_counter()
            try:
                return await handler(request)
            finally:
                logger.info("route timing", extra={"route": path, "duration_ms": (time.perf_counter() - start) * 1000})

        return timed


router = APIRouter(prefix="/reports", route_class=TimedRoute)
```

## Mounting sub-applications

`app.mount(path, other_app)` hands every request under that path to another ASGI app: static files, a second FastAPI
app with its own docs, or an app written with another framework. A mounted app sits behind the parent's
middleware, but the parent's dependencies do not apply to it, and a mounted FastAPI app has its own exception
handlers and its own `/docs`.

```python
from a2wsgi import WSGIMiddleware
from fastapi.staticfiles import StaticFiles

from legacy.flask_app import flask_app

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/v1", v1_app)                                   # its docs at /v1/docs
app.mount("/legacy", WSGIMiddleware(flask_app))            # Flask or Django, run in a thread pool
```

- The parent does not run a mounted app's lifespan. When a mounted app needs startup (an MCP server, a second app
  with its own pools), combine its lifespan into the parent's.
- `fastapi.middleware.wsgi.WSGIMiddleware` is deprecated; use `a2wsgi` (`uv add a2wsgi`).
- For a built single-page frontend, `app.frontend("/", directory="dist")` (FastAPI 0.138+) is better than mounting
  `StaticFiles`: API routes match first and client-side routes fall back to `index.html`.

## Verify the middleware order

These tests prove the order on the response a client really gets. Fill in your own allowed origin, and add a route that
raises when your app has none.

```python
import httpx

from app.main import app

ORIGIN = {"Origin": "https://app.example.com"}


async def test_unhandled_error_passes_through_every_layer() -> None:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom", headers=ORIGIN)
    assert response.status_code == 500
    assert response.headers["x-request-id"]
    assert response.headers["access-control-allow-origin"] == ORIGIN["Origin"]


def test_order_of_the_stack() -> None:
    names = [m.cls.__name__ for m in app.user_middleware]              # outermost first
    assert names[0] == "CORSMiddleware" and names[-1] == "CatchAllMiddleware"
```

```bash
uv run pytest -k "middleware or unhandled"
curl -si -X OPTIONS http://127.0.0.1:8000/items/ -H 'Origin: https://app.example.com' \
  -H 'Access-Control-Request-Method: GET' | grep -i '^access-control-allow-origin'
```

## Middleware anti-patterns

Each of these passes a happy-path test and fails on an error or a stream.

| Do not | Do instead |
|---|---|
| `@app.exception_handler(Exception)` to add headers to a 500 | a pure ASGI catch-all added first |
| add `CORSMiddleware` first | add it last, so it is outermost |
| authentication in middleware | a dependency on the router |
| read the whole body in middleware with no size limit | count the bytes as they arrive, cap them and replay the body |
| store `send` on `self` | close over it per request |
| `fastapi.middleware.wsgi.WSGIMiddleware` | `a2wsgi` |
| a second proxy-headers middleware | Uvicorn's `--proxy-headers` |

## Middleware checklist

Go through this middleware checklist before merging a change.

```text
[ ] each concern in the right place: per-request in middleware, per-route or per-user in a dependency
[ ] order chosen on purpose: CORS outermost, catch-all innermost, compression just outside it
[ ] hot-path and streaming middleware written as pure ASGI; other scope types passed through
[ ] unhandled exceptions answered by a pure ASGI catch-all added first, never an Exception handler
[ ] body-reading middleware replays the body and caps its size
[ ] mounted apps' lifespans combined into the parent's; WSGI apps through a2wsgi
```

## Common middleware problems

These are the middleware problems people hit most, each with its symptom, cause and fix.

1. **The browser reports a CORS error for a request that actually failed with 500.** The 500 was built outside
   `CORSMiddleware`, either because it is not the outermost layer or because an unhandled exception reached
   `ServerErrorMiddleware` (an `Exception` handler runs there too). Add `CORSMiddleware` last and a pure ASGI
   catch-all first.
2. **The request id header is missing on 500 responses, and the log line shows `-`.** The 500 was built by
   `ServerErrorMiddleware`, outside the middleware that adds the id. Add the pure ASGI catch-all first, so it runs
   inside that middleware.
3. **The endpoint hangs after middleware read the body.** The body was consumed. Replay it as shown.
4. **SSE events arrive in one burst.** A proxy buffered them, or middleware of your own collected the body chunks
   before sending. `GZipMiddleware` skips `text/event-stream`, and `BaseHTTPMiddleware` forwards each chunk as it
   comes. Turn buffering off at the proxy and pass chunks straight through in your middleware.
5. **A mounted app fails with "not initialized" errors.** Its lifespan never ran. Combine it into the parent's.
6. **`RuntimeError: Cannot add middleware after an application has started`.** `add_middleware()` was called in a
   lifespan or a request. Add middleware at import time, right after creating the app.

## See also

- fastapi-errors-observability: the error body and request ids.
- fastapi-security: CORS and trusted hosts.
- fastapi-performance: the cost of each layer.
- fastapi-websockets-streaming: streams through middleware.
- fastapi-mcp-server: combining a mounted app's lifespan.
