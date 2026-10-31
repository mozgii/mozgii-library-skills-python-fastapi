---
name: fastapi-performance
description: Finds and fixes FastAPI performance problems - load testing with Locust or k6, profiling with pyinstrument and py-spy, detecting event-loop blocking with asyncio debug mode, async versus def endpoints and the 40-thread pool, fast JSON through return types, N+1 queries, indexes and EXPLAIN ANALYZE, shared HTTP clients, concurrency with gather, caching with HTTP headers and Redis, and middleware cost. Use when an endpoint is slow, latency rises under load, throughput is lower than expected, CPU or memory is high, or before a launch to size the service.
metadata:
  tags:
  - fastapi
  - performance
  - profiling
  - load-testing
  - async
  - caching
  - database
  - latency
---

# FastAPI performance

This skill makes a FastAPI service fast by measuring first and fixing the causes that matter most in practice: a
blocked event loop, the database, slow serialization and repeated work. FastAPI itself is rarely the bottleneck.

## Step 0: installed versions for performance work

Read the versions first, since the fast serialization path depends on them. A missing line means it is not installed.

```bash
uv run python --version
uv pip list | grep -iE '^(fastapi|pydantic|uvicorn|uvloop|httptools|sqlalchemy|asyncpg) '
```

Rust serialization from return types needs FastAPI 0.130. `uvloop` and `httptools` are missing without
`uvicorn[standard]`, which `fastapi[standard]` installs.

## Choose the first tool by the symptom

Start from what the graphs show; each symptom points to one tool first.

| You see | Likely cause | First tool | Why the others lose |
|---|---|---|---|
| latency rises with load, CPU stays low | a blocking call on the event loop | `PYTHONASYNCIODEBUG=1`, py-spy | a profiler on one quiet request sees nothing wrong |
| one endpoint is slow even alone | a query or an upstream call | pyinstrument, SQL echo | a load test only repeats the number |
| CPU is high in the workers | serialization or real computation | py-spy flame graph | more threads share the same GIL |
| waits for a connection under load | a pool too small, or held during network calls | `engine.pool.status()` | a bigger server does not change the pool |
| a query is slow | a missing index, or offset pagination | `EXPLAIN (ANALYZE, BUFFERS)`, `pg_stat_statements` | app profiles only show time inside the driver |
| more workers bring no gain | the database or an upstream is the limit | a load test against each dependency | workers multiply the load on that limit |

## Measure before changing anything

A load test gives the baseline and shows whether a fix helped. Test a production-like build (`fastapi run`, real
database, same worker count), never `fastapi dev`.

```bash
oha -z 30s -c 50 http://127.0.0.1:8000/items/             # quick: 50 clients for 30 s (brew or cargo install oha)
uv add --dev locust && uv run locust -f tests/load/locustfile.py --headless -u 200 -r 20 -t 2m --host http://127.0.0.1:8000
```

```python
# tests/load/locustfile.py
from locust import HttpUser, between, task


class ApiUser(HttpUser):
    wait_time = between(0.5, 2)

    def on_start(self) -> None:
        r = self.client.post("/auth/token", data={"username": "load@example.com", "password": "load-test-only"})
        self.client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    @task(5)
    def list_items(self) -> None:
        self.client.get("/items/?limit=20")

    @task(1)
    def create_item(self) -> None:
        self.client.post("/items/", json={"title": "load"})
```

Watch p50, p95 and p99 latency and the error rate, not only requests per second.

## Profile a slow endpoint

A profiler shows where time goes inside one request; py-spy shows it for a running production process without code
changes.

```python
# development only: add ?profile=1 to any request to get an HTML profile
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from pyinstrument import Profiler


@app.middleware("http")
async def profile_request(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if settings.environment == "development" and request.query_params.get("profile"):
        with Profiler(async_mode="enabled") as profiler:
            await call_next(request)
        return HTMLResponse(profiler.output_html())
    return await call_next(request)
```

```bash
uvx py-spy top --pid <uvicorn worker pid>                  # live view of a running worker
uvx py-spy record -o profile.svg --pid <pid> --duration 30 # flame graph
```

## Find blocking calls on the event loop

One blocking call inside `async def` stops every request in that worker; latency then rises with load while CPU stays
low. Asyncio debug mode logs every callback that holds the loop longer than 100 ms.

```bash
PYTHONASYNCIODEBUG=1 uv run fastapi run 2>&1 | grep "Executing <"
```

Fix by using an async library, moving the call to `asyncio.to_thread()`, or declaring the endpoint `def`. The
blocking-call scanner in fastapi-code-standards and Ruff's `ASYNC` rules catch the common cases before they ship.

## async def, def and the thread pool

FastAPI runs `async def` endpoints on the event loop and `def` endpoints (and `def` dependencies) in AnyIO's thread
pool, which has 40 threads by default. Under load, sync endpoints queue for a thread.

- Use `async def` with async libraries (asyncpg, httpx.AsyncClient, redis.asyncio): no thread per request.
- Use `def` for code that blocks (sync SDKs, sync SQLModel sessions). Raise the thread limit if those endpoints
  queue:

```python
import anyio.to_thread


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    anyio.to_thread.current_default_thread_limiter().total_tokens = 100
    yield
```

- Trivial dependencies (reading a header, building a small object) should be `async def`; a thread hop costs more
  than the work.

## Fast JSON responses

Give every endpoint a return type or a `response_model`: FastAPI 0.130+ then serializes with Pydantic's Rust core,
two or more times faster than the generic encoder. `ORJSONResponse` and `UJSONResponse` are deprecated since 0.131
and no longer needed.

- Return Pydantic objects (or ORM objects with `response_model` and `from_attributes`), not dicts built by hand.
- Do not return huge lists: paginate (fastapi-sqlalchemy-async) or stream JSON Lines (fastapi-websockets-streaming).
- Select only the columns a list endpoint needs (`select(Item.id, Item.title)`) when rows are wide.

## Database: the usual bottleneck

Most slow endpoints are slow queries or too many queries. Turn on SQL logging locally and count statements per
request.

```python
engine = create_async_engine(url, echo=settings.environment == "development")
```

- **N+1**: a list endpoint that runs one query per row. Load relationships with `selectinload`/`joinedload` and keep
  `lazy="raise"` on relationships.
- **Missing indexes**: run `EXPLAIN (ANALYZE, BUFFERS)` on the slow query in `psql`; a `Seq Scan` on a large table
  with a selective filter needs an index. Index foreign keys and columns used in `WHERE` and `ORDER BY`.
- **Offset pagination on big tables**: switch to keyset pagination.
- **Pool exhaustion**: requests wait for a connection when concurrency exceeds `pool_size + max_overflow`; check
  `engine.pool.status()` and raise the pool or reduce time per connection (no network calls while a transaction is
  open).
- `pg_stat_statements` in PostgreSQL ranks queries by total time across all requests.

## Reuse HTTP clients and run independent calls together

Creating an `httpx.AsyncClient` per request throws away connection pooling and TLS sessions. Create one in the
lifespan and share it; run independent calls concurrently.

```python
@router.get("/dashboard")
async def dashboard(request: Request, user: CurrentUser) -> Dashboard:
    http: httpx.AsyncClient = request.state.http
    profile, orders, recs = await asyncio.gather(
        http.get(f"https://profiles.internal/profiles/{user.id}"),
        http.get(f"https://orders.internal/orders?customer={user.id}"),
        http.get(f"https://recs.internal/recommendations?customer={user.id}"),
    )
    return Dashboard.from_responses(profile, orders, recs)
```

Timeouts, retries and error mapping for outbound calls are covered in fastapi-external-apis.

Do not `gather` queries on one `AsyncSession`; a session runs one statement at a time. Use a session per task, or
one query that returns everything.

## Caching

The fastest request is one that never reaches the database. Cache at the layer closest to the client that is still
correct.

- **HTTP caching** for public or per-user data that changes rarely: `Cache-Control: public, max-age=60`, or `ETag`
  with `304 Not Modified`; a CDN then serves repeats.
- **Redis** for shared, computed data with a TTL; delete the key when the source changes.
- **In-process** (`functools.lru_cache`, a dict in lifespan state) for configuration and reference data; it is per
  worker and cleared on restart. `lru_cache` on an `async def` caches the coroutine, not its result: do not.

```python
@router.get("/categories")
async def categories(response: Response, session: SessionDep) -> list[CategoryRead]:
    response.headers["Cache-Control"] = "public, max-age=300"
    return await categories_service.all(session)
```

## Middleware cost and compression

Every middleware runs on every request. `BaseHTTPMiddleware` (`@app.middleware("http")`) adds overhead and wraps
response streams; write pure ASGI middleware for anything on the hot path (examples in fastapi-errors-observability).

Compress at the proxy or CDN when possible. In the app, `GZipMiddleware(minimum_size=1000)` skips small bodies, and
Starlette leaves `text/event-stream` responses uncompressed by default.

## Verify a performance fix

A fix counts only when the same load test shows it. Keep the numbers from before the change, then run:

```bash
oha -z 30s -c 50 http://127.0.0.1:8000/items/                   # compare p50, p95, p99 and the error rate
uv run ruff check --select ASYNC app/                           # blocking calls in async code
PYTHONASYNCIODEBUG=1 uv run fastapi run 2>&1 | grep "Executing <"   # expected under load: nothing new
uv run pytest -k "query_count"                                  # one query count per list endpoint
```

The same run must use the same data volume, worker count and machine, or the numbers do not compare.

## Performance anti-patterns

Each of these spends effort where the time is not going.

| Do not | Do instead |
|---|---|
| tune before measuring | a baseline load test first |
| load test `fastapi dev` | a production-like `fastapi run` with the real database |
| add `ORJSONResponse` | a return type or `response_model` |
| `asyncio.gather` on one `AsyncSession` | one session per task, or one query |
| `lru_cache` on an `async def` | Redis or an explicit in-process dict |
| a new `httpx.AsyncClient` per request | one client from the lifespan |
| raise workers past the core count | find the real limit first |

## Performance checklist

Go through this performance checklist before merging a change.

```text
[ ] baseline load test (p50/p95/p99, errors) on a production-like setup
[ ] slow endpoints profiled (pyinstrument locally, py-spy in production)
[ ] no blocking calls in async code (asyncio debug mode, ASYNC lint, scanner)
[ ] sync endpoints sized against the thread pool; limit raised if they queue
[ ] every endpoint has a return type; no ORJSONResponse
[ ] no N+1; slow queries explained and indexed; keyset pagination for big tables
[ ] one shared httpx.AsyncClient; independent calls gathered
[ ] cache headers or Redis for repeatable reads; no lru_cache on async functions
[ ] hot-path middleware is pure ASGI; compression at the edge
```

## Common performance problems

These are the performance problems people hit most, each with its symptom, cause and fix.

1. **Throughput drops when adding `async`.** The endpoint became `async def` but still calls a sync library. Revert to
   `def` or switch the library.
2. **Latency spikes every few minutes.** Connections dropped by a proxy or firewall reconnect under load; set
   `pool_pre_ping=True` and `pool_recycle` below the idle timeout.
3. **More workers, same throughput.** The database or an external API is the limit. Profile before scaling.
4. **Fast locally, slow in production.** Different data volume: an index that does not matter on 1,000 rows matters on
   10 million. Test with production-sized data.
5. **`RuntimeWarning: The global interpreter lock (GIL) has been enabled to load module ...` on Python 3.14t.** FastAPI
   0.136+ supports Python 3.14t, but C extensions and drivers may re-enable the GIL or lack wheels, as `psycopg-binary`
   3.3.6 does; measure before switching.

## See also

- fastapi-code-standards: the blocking-call scanner.
- fastapi-sqlalchemy-async: loading, pagination and pool sizing.
- fastapi-external-apis: timeouts and retries.
- fastapi-middleware: pure ASGI middleware.
- fastapi-deployment: workers and processes.
