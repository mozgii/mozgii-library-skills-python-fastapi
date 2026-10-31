---
title: FastAPI security hardening
author: MOZGII, Inc.
lang: en
---

# FastAPI security hardening

This reference gives the code and settings for each item on the FastAPI hardening list: CORS, trusted hosts,
security headers, rate limiting, request limits, documentation in production, injection, SSRF, mass assignment and
dependency audits.

## Contents

This reference on FastAPI security hardening has these sections:

- CORS for browser clients
- Trusted hosts
- Security headers middleware
- Rate limiting
- Request size and time limits
- API documentation in production
- SQL injection
- SSRF when fetching URLs
- Mass assignment
- Logging without secrets
- Dependency audits

## CORS for browser clients

CORS tells browsers which other origins may call the API. List exact origins from settings; a wildcard with
credentials is refused by browsers and dangerous in intent.

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,            # ["https://app.example.com"]
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)
```

An API used only by servers and mobile apps needs no CORS middleware at all.

## Trusted hosts

`TrustedHostMiddleware` rejects requests whose `Host` header is not yours, which blocks host-header injection into
password-reset links and cache poisoning.

```python
from fastapi.middleware.trustedhost import TrustedHostMiddleware

app.add_middleware(TrustedHostMiddleware, allowed_hosts=["api.example.com", "*.example.com"])
```

Health checks from a load balancer often use the pod IP as host; add it or serve health checks on a separate port.

## Security headers middleware

A pure ASGI middleware adds security headers to every response without the overhead of `BaseHTTPMiddleware`.

```python
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADERS = [
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *HEADERS]
            await send(message)                # the closure's send: one per request, never stored on self

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)
```

The strict CSP above suits a JSON API; it blocks Swagger UI, so relax it on `/docs` or serve docs only in development.

## Rate limiting

Rate limits slow down password guessing and protect expensive endpoints. Apply them at the edge (Nginx
`limit_req`, a cloud WAF, an API gateway) when you can; in the app, `slowapi` (MIT) with a Redis backend shares
counters across workers.

```python
from fastapi import Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, storage_uri="redis://redis:6379/0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@router.post("/token")
@limiter.limit("5/minute")
async def login(request: Request, ...) -> Token: ...
```

Behind a proxy, `get_remote_address` sees the proxy's IP unless Uvicorn trusts forwarded headers
(`--forwarded-allow-ips`). Key login limits by account as well as by IP to stop distributed guessing.

## Request size and time limits

FastAPI sets no body size or request time limit, so set them where the bytes arrive.

- Nginx: `client_max_body_size 10m;`, `client_body_timeout 30s;`, `proxy_read_timeout 60s;`.
- Uvicorn: `--limit-concurrency 1000` returns 503 above that many open connections; `--timeout-keep-alive 5`.
- In code, cap list sizes and string lengths in Pydantic models; an unbounded list of 10 million items is a valid
  JSON body.

## API documentation in production

The interactive docs reveal every endpoint and schema. Disable them for public production APIs, or protect them.

```python
docs_enabled = settings.environment != "production"
app = FastAPI(
    docs_url="/docs" if docs_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if docs_enabled else None,
)
```

Export the schema at build time instead (fastapi-openapi) so client generation still works.

## SQL injection

SQLAlchemy's `select()` and `text()` with bound parameters are safe; string formatting is not.

```python
# unsafe: the value becomes SQL
await session.execute(text(f"SELECT * FROM items WHERE title = '{title}'"))

# safe
await session.execute(text("SELECT * FROM items WHERE title = :title"), {"title": title})
```

Column names and sort directions cannot be bound; map them through an allow-list such as
`{"created_at": Item.created_at, "title": Item.title}[sort]` or a `Literal` type on the query parameter.

## SSRF when fetching URLs

An endpoint that fetches a user-supplied URL (webhooks, link previews, imports) can be pointed at internal services
or the cloud metadata address `169.254.169.254`.

- Allow only `https` and an allow-list of hosts where possible.
- Resolve the host and reject private, loopback and link-local addresses (`ipaddress.ip_address(addr).is_private`),
  then connect to the resolved address so DNS cannot change between check and use.
- Disable redirects (`follow_redirects=False`) or re-check every redirect target; set short timeouts and a response
  size limit.

## Mass assignment

Mass assignment happens when request data is copied onto a model wholesale and sets fields the client should not
control (`is_admin`, `owner_id`, `price`).

- Input schemas list only writable fields; `owner_id` comes from the current user, never from the body.
- Use `model_dump(exclude_unset=True)` from the input schema, never the raw request JSON.
- Add `extra="forbid"` to input models so unexpected fields fail instead of being ignored.

## Logging without secrets

Logs are read by more people than the database. Log ids and outcomes; never passwords, tokens, API keys, full request
bodies or `Authorization` headers. Redact at the logger (a filter or a structlog processor) so one careless line does
not leak.

## Dependency audits

Known vulnerabilities in dependencies are the cheapest attack. Check the lock file in CI.

```bash
uv audit --preview-features audit            # uv 0.11+, experimental
uv run --with pip-audit pip-audit            # alternative, audits the project's environment
```

Pin with `uv.lock`, update regularly with `uv lock --upgrade`, and read the FastAPI and Starlette release notes for
security fixes.
