---
name: fastapi-dependencies
description: Uses FastAPI dependency injection well - Depends with Annotated type aliases, dependencies with yield and their scope (request or function), sub-dependency chains, per-request caching, router-level dependencies, validation dependencies that load a resource or raise 404, sync versus async dependencies and dependency_overrides in tests. Use when sharing logic across endpoints, managing a database session or client per request, checking permissions, loading a resource by id, or replacing a dependency in tests.
metadata:
  tags:
  - fastapi
  - dependencies
  - depends
  - yield
  - dependency-injection
  - dependency-overrides
---

# FastAPI dependency injection

This skill uses `Depends` to give endpoints what they need (a session, the current user, a loaded resource) without
repeating code, and with cleanup that runs at the right moment. Checked against FastAPI 0.162.

## Step 0: installed versions for dependency injection

Read the FastAPI version first, because when cleanup runs changed twice. A missing line means it is not installed.

```bash
uv pip list | grep -iE '^(fastapi|starlette|anyio) '
```

Yield cleanup runs after the response from 0.118. `scope=` on `Depends` needs 0.121, and security classes on WebSockets
need 0.144; on an older one, upgrade before relying on them.

## Choose the right kind of dependency

Most sharing problems in FastAPI have one fitting tool; a dependency is not always it.

| Need | Use | Why the others lose |
|---|---|---|
| a resource per request, with cleanup, that streams may still use | a `yield` dependency, default scope | `scope="function"` closes it before a streaming response reads from it |
| a lock or a resource released before the client gets the answer | `Depends(fn, scope="function")` | the default scope holds it until the response is sent |
| a check whose value the endpoint does not need | `dependencies=[Depends(fn)]` on the router or app | a parameter nobody reads looks like a mistake |
| a value used by several dependencies in one request | one dependency, cached per request | `use_cache=False` repeats the work |
| a fresh value every time within one request | `Depends(fn, use_cache=False)` | the cache returns the first result |
| a client or pool that lives as long as the app | the lifespan, read through a thin dependency | a `yield` dependency would reconnect on every request |
| a rule on one field | the Pydantic model | a dependency hides it from the request schema |

## When to write a dependency

A dependency is worth writing when logic is shared by several endpoints, needs request data (headers, path
parameters), needs cleanup, or can fail early with an HTTP error. Plain validation of one field belongs in the
Pydantic model, not in a dependency.

Typical dependencies: the database session, the current user, permission checks, a resource loaded by id, pagination
parameters, an external client, feature flags.

## Annotated aliases for dependencies

Declare each dependency once as an `Annotated` alias and use the alias in every endpoint. The endpoint signature then
reads like plain types.

```python
# app/deps.py: every alias declared once
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin   # the chain under "Sub-dependencies and chains"
from app.db import get_session                         # the yield dependency in the next section
from app.users.models import User

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
```

```python
# app/items/router.py: endpoints use the aliases as plain types
from fastapi import APIRouter

from app.deps import CurrentUser, SessionDep
from app.items.schemas import ItemRead

router = APIRouter(prefix="/items", tags=["items"])


@router.get("/mine")
async def list_my_items(session: SessionDep, user: CurrentUser) -> list[ItemRead]: ...
```

## When a yield dependency cleans up: scope

A dependency that `yield`s runs its code after `yield` as cleanup. Its `scope` says when: `"request"` (the default
for yield dependencies) runs cleanup after the response has been sent; `"function"` runs it right after the endpoint
returns, while the response is still unsent. Verified in FastAPI 0.162: the order with `scope="function"` is
enter → handler → exit → response.

```python
# app/db.py
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

engine = create_async_engine("postgresql+psycopg://app@localhost/app")
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
export_lock = threading.Lock()   # one export at a time across the process


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session              # cleanup (close) after the response: streaming responses can still use it


def hold_export_lock() -> Iterator[None]:
    export_lock.acquire()          # sync dependency: FastAPI runs it in the thread pool
    try:
        yield
    finally:
        export_lock.release()      # released before the client gets the response


ExportLock = Annotated[None, Depends(hold_export_lock, scope="function")]
```

- Use the default scope for database sessions, so a `StreamingResponse` or SSE endpoint can keep reading while it
  streams (FastAPI 0.118+ behaviour).
- Use `scope="function"` when the resource must be released before the response goes out, or when an exception after
  `yield` must still be able to change the response.
- Code after `yield` that catches an exception must re-raise it (`raise`) or raise a new one. A swallowed exception
  leaves the request without a response: the client gets a 500 and the server logs `FastAPIError: Response not
  awaited`, which names a dependency with yield that has a bare `except`.
- Do not commit in the session dependency's cleanup with the default scope: the client already has its 200 when the
  commit fails. Commit in the service, before returning.

## Sub-dependencies and chains

A dependency can depend on others, so checks build on each other: token → user → active user → admin.

```python
# app/auth.py: spelled out with Depends() here, because app/deps.py imports from this module
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.users.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str, Depends(oauth2_scheme)],
) -> User: ...                     # decode the token, load the user, 401 when either fails


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user
```

FastAPI caches each dependency per request: `get_current_user` runs once even when three dependencies in the same
request need it. Pass `Depends(fn, use_cache=False)` when a fresh value is required every time.

## Validation dependencies that load a resource

A dependency that takes the path parameter, loads the object and raises 404 or 403 removes the same five lines from
every endpoint of that resource.

```python
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import select

from app.deps import CurrentUser, SessionDep
from app.items.models import Item
from app.items.router import router
from app.items.schemas import ItemRead


async def get_owned_item(item_id: int, session: SessionDep, user: CurrentUser) -> Item:
    item = await session.scalar(select(Item).where(Item.id == item_id, Item.owner_id == user.id))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return item


OwnedItem = Annotated[Item, Depends(get_owned_item)]


@router.get("/{item_id}")
async def read_item(item: OwnedItem) -> ItemRead:
    return ItemRead.model_validate(item)


@router.delete("/{item_id}", status_code=204)
async def delete_item(item: OwnedItem, session: SessionDep) -> None:
    await session.delete(item)
    await session.commit()
```

Return 404, not 403, for another user's resource: a 403 confirms the id exists.

## Router-level and app-level dependencies

Dependencies whose value the endpoint does not need (an auth check, a rate limit, an audit hook) go in the router's or
app's `dependencies=` list and run for every route in it.

```python
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
app = FastAPI(dependencies=[Depends(enforce_tenant_header)])
```

## Function dependencies instead of class dependencies

A function that returns an instance is clearer than `Depends()` on a class whose `__init__` FastAPI inspects; the
FastAPI maintainers recommend the function form.

```python
from dataclasses import dataclass


@dataclass
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


PaginationDep = Annotated[Pagination, Depends(pagination)]
```

A callable instance works when the dependency needs configuration: `Depends(RequireScope("items:write"))` where the
class defines `__call__`.

## Sync versus async dependencies

FastAPI runs a `def` dependency in the thread pool and an `async def` dependency on the event loop, exactly as for
endpoints.

- A dependency that only reads headers or builds an object should be `async def`: a thread hop for trivial work costs
  more than the work.
- A dependency that calls blocking code must be `def` (or wrap the call in `asyncio.to_thread`).
- Many `def` dependencies per request can exhaust the thread pool (40 threads by default in AnyIO); see
  fastapi-performance.

## dependency_overrides in tests

`app.dependency_overrides` replaces a dependency for the whole app, which is how tests swap the database session or
the current user without tokens.

```python
from app.auth import get_current_user
from app.main import app


@pytest.fixture
def as_user(client: httpx.AsyncClient):
    def login(user: User) -> httpx.AsyncClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return client

    yield login
    app.dependency_overrides.pop(get_current_user, None)
```

Override the function object that `Depends()` received, not the `Annotated` alias. Clear overrides after each test,
or one test's user leaks into the next.

## Verify dependencies and their cleanup

Run the commands, then keep the test below: it proves when cleanup runs, in your FastAPI version.

```bash
uv run ruff check --select FAST002 app/                         # dependencies written without Annotated
uv run pytest -k "dependency or override"
grep -rn 'dependency_overrides\[' tests/ | grep -v conftest     # each one needs a matching clear or pop
```

```python
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def run(scope: str) -> list[str]:
    events: list[str] = []

    def resource() -> Iterator[None]:
        yield
        events.append("cleanup")

    app = FastAPI()

    @app.get("/")
    def read(_: Annotated[None, Depends(resource, scope=scope)]) -> dict[str, str]:
        return {"status": "ok"}

    class Spy:
        def __init__(self, inner: ASGIApp) -> None:
            self.inner = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            async def spy(message: Message) -> None:
                if message["type"] == "http.response.start":
                    events.append("response")
                await send(message)

            await self.inner(scope, receive, spy)

    app.add_middleware(Spy)
    TestClient(app).get("/")
    return events


def test_cleanup_order() -> None:
    assert run("function") == ["cleanup", "response"]
    assert run("request") == ["response", "cleanup"]
```

## Dependency injection anti-patterns

These turn a helpful feature into a source of surprises in production and in tests.

| Do not | Do instead |
|---|---|
| `except Exception: pass` after `yield` | re-raise, or the client gets a 500 with `Response not awaited` in the log |
| commit in the session dependency's cleanup | commit in the service, before the response |
| `Depends()` on a class whose `__init__` FastAPI inspects | a function that returns the instance |
| a blocking call in an `async def` dependency | make it `def`, or use `asyncio.to_thread` |
| 403 for another user's resource | 404, so the id stays unconfirmed |
| overriding the `Annotated` alias in a test | override the function that `Depends()` holds, and clear it afterwards |

## Dependency injection checklist

Go through this dependency injection checklist before merging a change.

```text
[ ] every dependency declared once as an Annotated alias
[ ] yield dependencies release resources in finally; exceptions re-raised
[ ] database session uses the default scope; nothing commits in cleanup
[ ] scope="function" only where cleanup must happen before the response
[ ] repeated "load by id and check owner" logic moved into a dependency returning 404
[ ] side-effect-only checks in router dependencies=[...]
[ ] trivial dependencies are async def; blocking ones are def
[ ] tests override the original function and clear overrides afterwards
```

## Common dependency injection problems

These are the dependency injection problems people hit most, each with its symptom, cause and fix.

1. **Streaming response fails with a closed session.** Before FastAPI 0.118, yield cleanup ran before the response was
   sent. Upgrade, and keep the session dependency on the default scope.
2. **`FastAPIError: Response not awaited`.** An `except` block after `yield` swallowed the endpoint's exception. Add
   `raise`.
3. **`DependencyScopeError` at startup, naming a request-scoped dependency and a function-scoped one below it.** A
   `scope="request"` dependency may only depend on request-scoped ones; a function-scoped dependency may depend on
   both. Widen the inner one to `scope="request"`, or narrow the outer one to `scope="function"`.
4. **The dependency runs once even though you expected twice.** Per-request caching; use `use_cache=False`.
5. **An override has no effect.** The test overrode a different function object (an import alias or the
   `Annotated` alias). Override exactly what `Depends()` holds.
6. **`TypeError: OAuth2PasswordBearer.__call__() missing 1 required positional argument: 'request'` on a WebSocket
   endpoint.** Since FastAPI 0.144, `OAuth2PasswordBearer`, `APIKeyHeader` and other security classes also work in
   WebSocket endpoints; older versions needed manual header parsing. Upgrade, or parse the header yourself.

## See also

- fastapi-security: the current-user chain.
- fastapi-sqlalchemy-async: the session dependency.
- fastapi-testing: `dependency_overrides` in fixtures.
- fastapi-performance: the thread pool that `def` dependencies use.
- fastapi-websockets-streaming: sessions during a stream.
