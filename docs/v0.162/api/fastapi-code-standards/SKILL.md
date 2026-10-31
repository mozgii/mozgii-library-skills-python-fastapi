---
name: fastapi-code-standards
description: Code rules for production FastAPI backends - strict typing checked by pyright, Annotated parameters, thin route handlers, no blocking calls in async code, ORM queries instead of string SQL, eager loading against N+1, explicit transactions, immutable migrations and errors that never leak internals. Use when writing or reviewing any backend Python file in a FastAPI service, before a commit, or when setting up lint and type-check gates.
metadata:
  tags:
  - fastapi
  - code-review
  - typing
  - pyright
  - ruff
  - sqlalchemy
  - standards
---

# FastAPI code standards

These are the rules every change to a FastAPI backend follows: types a strict checker accepts, handlers that only
translate HTTP, database access that cannot inject SQL or load lazily, and errors that tell the client what happened
without showing how the server works. Each rule has a check that a tool can run.

## Step 0: installed versions for the code standards

Read the versions before judging code against these rules. A missing line means the tool is not installed.

```bash
uv pip list | grep -iE '^(fastapi|pydantic|sqlalchemy|ruff|pyright) '
```

Models imported from `pydantic.v1` stop working on FastAPI 0.128 or later, so migrate those first
(fastapi-version-upgrades). If `ruff` or `pyright` is missing, add them with `uv add --dev ruff pyright`.

## Choose async def or def for an endpoint

The body of the handler decides, because `async def` runs on the event loop and `def` runs in a thread pool.

| The body calls | Write | Why the others lose |
|---|---|---|
| only async libraries (asyncpg, `httpx.AsyncClient`) | `async def` | `def` spends a pool thread on every request for nothing |
| a blocking library with no async version | `def` | `async def` stops every other request in that worker for as long as the call blocks |
| mostly async code with one blocking call | `async def` with `await asyncio.to_thread(fn, ...)` | a `def` endpoint cannot `await` the rest of its work |
| the same, but that library takes only a few calls at once | `async def` with Asyncer: `await asyncify(fn, limiter=limiter)(...)`, with one shared `limiter = anyio.CapacityLimiter(5)` | `asyncio.to_thread` has no limit of its own: calls fill the event loop's default executor |
| CPU-heavy work | a queue worker or a process pool | a thread still holds the GIL on standard CPython builds |

## Strict typing rules

Strict typing means pyright in strict mode reports zero errors, so a wrong shape is caught at review time instead of
in production.

- Every function declares its parameter and return types, including route handlers (`-> ItemRead`, `-> None`).
- Write unions as `int | None`, never `Optional[int]`; use built-in generics (`list[str]`, `dict[str, int]`).
- No `Any` in signatures. When a value really is untyped (a JSON blob), type it `dict[str, object]` or a
  `TypedDict` and narrow it.
- No blanket `# type: ignore`. When a third-party stub is wrong, use a scoped
  `# pyright: ignore[ruleName]` with a comment saying why, on that one line.
- Pydantic v2 only: `Model.model_validate(obj)`, `model.model_dump()`, `model_config = ConfigDict(...)`. The
  v1-style `parse_obj()`, `.dict()` and `class Config:` still run in Pydantic 2 but are deprecated (each warns);
  models imported from `pydantic.v1` no longer work at all, since FastAPI 0.128 dropped support for them.
- Install typed extras where they exist: `pyjwt[crypto]` removes the "partially unknown" errors that plain `pyjwt`
  gives under strict mode.

Run after every edit, and before every commit:

```bash
uv run pyright app/items/service.py      # one file, seconds
uv run pyright                           # the whole project, typeCheckingMode = "strict"
```

Astral's `ty` (`uv add --dev ty`, `uv run ty check`) is a faster checker that FastAPI's own agent guidance and the
full-stack template use; it is still 0.0.x, so keep pyright strict as the gate and run ty beside it if you like.

If `uv run` warns that `VIRTUAL_ENV` does not match the project, another environment is active in the shell: run
`unset VIRTUAL_ENV` first, or pyright may check against the wrong packages.

## Annotated parameters and dependency aliases

Every parameter and dependency uses `Annotated`, so the function signature stays a normal Python signature and the
same alias is reused across endpoints.

```python
from typing import Annotated

from fastapi import Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{item_id}")
async def show_item(
    item_id: Annotated[int, Path(ge=1)],
    session: SessionDep,
    include_owner: Annotated[bool, Query()] = False,
) -> ItemRead: ...
```

- Required parameters and fields have no default; never `= ...` (Ellipsis) and never `= Query(...)`.
- Ruff rule `FAST002` flags dependencies written without `Annotated`.

## Thin route handlers

A route handler parses input, calls one service function and returns a response schema. Business rules live in the
service, where tests can call them without HTTP.

- Keep a handler under about 15 lines; move anything longer to `service.py`.
- Keep a router module under about 500 lines; split by sub-resource when it grows.
- One HTTP method per function. Never branch on `request.method`.
- Before writing a new helper, search the domain's `service.py` and the shared `app/core/` for an existing one.

```python
@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_item(body: ItemCreate, session: SessionDep, user: CurrentUser) -> ItemRead:
    item = await items_service.create(session, owner=user, data=body)
    return ItemRead.model_validate(item)
```

## No blocking calls in async code

An `async def` endpoint runs on the event loop, so one blocking call stalls every request in that worker. A plain
`def` endpoint runs in a thread pool and may block.

- Banned inside `async def`: `time.sleep`, `requests`, sync `httpx.get`, `subprocess.run`, sync database drivers,
  large `open().read()`, CPU-heavy loops.
- Use `await asyncio.sleep()`, `httpx.AsyncClient`, async drivers (asyncpg, psycopg async), and
  `await asyncio.to_thread(fn, ...)` (or Asyncer's `asyncify` when calls need a cap) for a blocking library you cannot
  replace.
- When in doubt and the code is blocking, declare the endpoint with `def`; FastAPI runs it in the thread pool.

Scan for violations (exit code 1 when any are found):

```bash
python3 scripts/find_blocking_calls.py app/
uv run ruff check --select ASYNC app/
```

When a script from `scripts/` is not on disk (a pack install), read that script from the library that served this page and save it first.

## Database access rules

Database code uses SQLAlchemy 2.0-style constructs, loads relationships on purpose and owns its transactions in one
layer. Detail: fastapi-sqlalchemy-async and fastapi-repository-pattern.

- Query with `select()`, `insert()`, `update()`, `delete()`. When SQL text is unavoidable (a recursive CTE, a
  vendor function), use `text()` with bound parameters: `text("... WHERE id = :id").bindparams(id=item_id)`.
  Never build SQL with f-strings or `%`.
- Models use `Mapped[...]` and `mapped_column()`; no legacy `Column()` on declarative classes.
- No lazy loading in async code: set `lazy="raise"` on relationships and load them with
  `selectinload()` or `joinedload()` in the query that needs them.
- Routers do not build queries; they call the service or repository.
- Timestamps are `DateTime(timezone=True)`; JSON columns on Postgres are `JSONB`.
- After mutating a JSONB value in place (`row.data["k"] = v`), call `flag_modified(row, "data")` before commit, or
  use `MutableDict.as_mutable(JSONB)` on the column.
- One layer owns `commit()`. When a commit can fail, roll back before re-raising:

```python
try:
    await session.commit()
except IntegrityError:
    await session.rollback()
    raise ItemAlreadyExists(title=data.title) from None
```

## Migration rules

Migrations are history: once applied anywhere shared, they never change. Detail: fastapi-alembic-migrations.

- Never edit or delete a migration file that has been merged or applied to a shared database; write a new one.
- Every schema change is a new revision: change the models, run
  `uv run alembic revision --autogenerate -m "add items.archived_at"`, read the generated `upgrade()` and
  `downgrade()`, then `uv run alembic upgrade head`.
- `uv run alembic check` in CI fails when models and migrations disagree.

## Error handling rules

Errors are part of the API contract: a stable shape, a correct status code and no internals.

- Raise `HTTPException` (or a domain exception mapped by a handler) with a message written for the client.
- Never put `str(exc)`, a traceback, SQL or a file path in a response. Log it with the request id instead.
- A pure ASGI catch-all, added first so it is the innermost middleware, returns a generic 500 body and logs the full
  exception. An `@app.exception_handler(Exception)` handler runs outside every user middleware, so its 500 has no
  request id and no CORS headers.
- Service and repository code raises domain exceptions (`ItemNotFound`), not `HTTPException`, so it can run outside
  HTTP. Map them to status codes once, in `main.py`. Detail: fastapi-errors-observability.

```python
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
            logger.exception("unhandled error", extra={"path": scope["path"]})
            if not started:
                body = {"title": "Internal Server Error", "status": 500}
                await JSONResponse(body, status_code=500)(scope, receive, send)
            raise                                     # the server and error trackers still see it


app.add_middleware(CatchAllMiddleware)                # add it before every other middleware
```

## Configuration and secrets rules

Configuration comes from the environment through pydantic-settings; secrets have no defaults and never reach a log.

- Read settings through `get_settings()`; no `os.environ[...]` scattered through the code.
- Secrets are `SecretStr`; no secret literals in code, tests or fixtures (use placeholders such as `"x" * 32`).
- Log ids and counts, not request bodies, tokens or passwords.

## Pre-commit gate

The gate is one command list that must pass before a commit or a merge.

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
python3 scripts/find_blocking_calls.py app/
uv run alembic check
uv run pytest -x
```

## Verify the code standards

Run the gate above, then these checks for the rules a linter cannot see. Each `grep` line must print nothing.

```bash
python3 scripts/find_blocking_calls.py app/; echo "exit $?"     # 0 when clean, 1 with findings
uv run ruff check --select ASYNC,FAST,S608 app/                 # blocking calls, FastAPI rules, string-built SQL
grep -rnE 'detail=str\(|detail=f".*\bexc\b' app/                 # exception text in a response
grep -rn '^ *class Config:' app/                                # Pydantic v1 configuration
grep -rn 'type: ignore' app/                                    # blanket ignores
```

## Code standards anti-patterns

Most review comments on a FastAPI change are one of these.

| Do not | Do instead |
|---|---|
| `detail=str(exc)` | a message written for the client, and the exception in the log |
| `Optional[int]`, `Any` in a signature | `int \| None`, a `TypedDict` or `dict[str, object]` |
| `= Query(...)` or `= ...` for a required parameter | `Annotated[int, Query()]` with no default |
| `requests` or `time.sleep` inside `async def` | `httpx.AsyncClient`, `await asyncio.sleep()` |
| f-strings or `%` in SQL | `select()`, or `text()` with bound parameters |
| business rules in a route handler | a service function that tests can call without HTTP |
| `@app.exception_handler(Exception)` as the catch-all | pure ASGI middleware added first |

## Code standards checklist

Go through this code standards checklist before merging a change.

```text
[ ] every function typed; no Any, no Optional, no bare type: ignore; pyright strict clean
[ ] parameters and dependencies use Annotated; no Ellipsis defaults
[ ] handlers under ~15 lines; logic in services; one HTTP method per function
[ ] no blocking call inside async def (script and ruff ASYNC clean)
[ ] no string-built SQL; text() only with bound parameters
[ ] relationships lazy="raise" and loaded explicitly; no N+1
[ ] commit owned by one layer, rollback on failure
[ ] no edited applied migration; alembic check clean
[ ] errors carry no internals; unexpected errors logged and answered with a generic 500
[ ] secrets via SecretStr from the environment, never logged
```

## Common code standards problems

These are the code standards problems people hit most, each with its symptom, cause and fix.

1. **Every request in a worker stalls during one slow call.** An `async def` handler called a sync SDK, which freezes
   the worker; a `def` handler costs a thread instead. Pick by what the body calls, not by habit.
2. **Pyright passes, but a `None` turns up at runtime.** `Mapped[str]` on a column that is nullable in the database
   passes the type check; the migration is the truth. Keep `nullable` and `Mapped[str | None]` in step.
3. **An error response shows table or constraint names.** `detail=str(exc)` leaks: database errors include table names,
   constraint names and sometimes values. Map known errors to messages; log the rest.
4. **A review blocks a 20-line handler but passes a 10-line one that runs three queries.** Rules about line counts are
   guides. A 20-line handler that only validates and calls one service is fine; the 10-line one is not.

## See also

- fastapi-pydantic-models: schema rules behind the typing section.
- fastapi-sqlalchemy-async: the database rules in detail.
- fastapi-repository-pattern: where queries and commits live.
- fastapi-errors-observability: the error body and the catch-all.
- fastapi-alembic-migrations: migration rules and `check_migrations.sh`.
