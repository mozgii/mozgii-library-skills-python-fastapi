---
name: fastapi-testing
description: Tests FastAPI backends with pytest 9, pytest-asyncio 1.4 and a real PostgreSQL - auto mode and session loop scope, the httpx AsyncClient with ASGITransport, TestClient for sync and WebSocket tests, testcontainers with the asyncpg driver, schema built by Alembic migrations, a transaction-per-test rollback with savepoints, dependency_overrides for auth and settings, lifespan in tests, and tests for tenant isolation, response contracts and audit logs. Use when writing any test for a FastAPI app, when async tests are skipped or fail with "attached to a different loop", or when setting up a test database.
metadata:
  tags:
  - fastapi
  - testing
  - pytest
  - pytest-asyncio
  - httpx
  - testcontainers
  - postgres
  - sqlalchemy
---

# FastAPI testing with pytest and real Postgres

This skill tests FastAPI apps against a real PostgreSQL in Docker, with real migrations and real async, because the
things that break in production (constraints, scope filters, migrations, transactions) do not exist in a mock.
Verified on 2026-10-28 with pytest 9.2.0, pytest-asyncio 1.4.1, httpx 0.28.1, testcontainers 4.15.0, SQLAlchemy
2.1.6, asyncpg 0.32.0 and PostgreSQL 18: the suite in [references/conftest.md](references/conftest.md) passes as
written.

## Step 0: installed versions for the test stack

Read the test stack first, because each of these packages changed its rules recently. A missing line means it is not
installed.

```bash
uv pip list | grep -iE '^(pytest|pytest-asyncio|httpx|httpx2|testcontainers|fastapi|starlette) '
docker info --format '{{.ServerVersion}}'                       # testcontainers needs a running Docker
```

pytest 8.4+ fails unmarked async tests, pytest-asyncio 1.0 removed `event_loop`, and httpx 0.28 removed
`AsyncClient(app=...)`. On older versions the failures below look different.

## Choose the test client and the database

The closer a test runs to production, the more it finds; choose the cheapest option that still sees the failure.

| Option | Wins when | Loses because |
|---|---|---|
| `httpx.AsyncClient` with `ASGITransport` | async suites that share fixtures such as a database session | it skips the lifespan unless you add `LifespanManager` |
| `TestClient` | sync suites and WebSocket tests; `with` runs the lifespan | it runs the app in another thread and loop, so async fixtures cannot be shared |
| a real server in a container | smoke tests of the image | slow, and it cannot override dependencies |
| PostgreSQL in testcontainers | every test that touches data | needs Docker and a few seconds at session start |
| SQLite | never for a PostgreSQL app | no `JSONB`, no `FOR UPDATE`, and different constraint behaviour, so it proves another database |
| a mocked repository | unit tests of a business rule | it proves the code you wrote, never a constraint or a scope filter |

## Install

The test stack is pytest with pytest-asyncio, httpx for the async client, and testcontainers for PostgreSQL.

```bash
uv add --dev pytest pytest-asyncio pytest-cov httpx "testcontainers[postgres]"
```

`fastapi[standard]` already brings `httpx` and `httpx2`. Docker must be running for testcontainers.

## Configure pytest once

Auto mode runs every `async def test_*` without a marker; session loop scope lets session-scoped async fixtures (the
engine) and tests share one event loop.

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-q --strict-markers --strict-config"
```

- In `strict` mode (the default) an async test without `@pytest.mark.asyncio` fails with "async def functions are not
  natively supported" on pytest 8.4+; older pytest skipped it with only a warning, so old suites may contain tests
  that never ran.
- With a session-scoped engine fixture and function-scoped test loops, the engine's connections belong to another
  loop and tests fail with "attached to a different loop" or "Future attached to a different loop". Setting both loop
  scopes to `session` avoids it.
- pytest-asyncio 1.0 removed the `event_loop` fixture; delete any override of it. 1.4 deprecates overriding
  `event_loop_policy` in favour of the `pytest_asyncio_loop_factories` hook (for example to test with uvloop).

## The async client: ASGITransport

`httpx.AsyncClient` with `ASGITransport` calls the app in-process: no server, no port, no socket, and the same event
loop as the test.

```python
import httpx

transport = httpx.ASGITransport(app=app)
async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
    response = await client.get("/health")
```

`AsyncClient(app=app)` was removed in httpx 0.28 and raises `TypeError: unexpected keyword argument 'app'`. The same
API exists in `httpx2` (`httpx2.ASGITransport`), the Pydantic-maintained continuation of httpx.

## TestClient for sync tests and WebSockets

`fastapi.testclient.TestClient` runs the app in a background thread with its own loop and gives a sync API. Use it
for sync test suites and for WebSocket tests; use `AsyncClient` when tests share async fixtures such as a database
session.

```python
from fastapi.testclient import TestClient


def test_health() -> None:
    with TestClient(app) as client:          # "with" runs the lifespan
        assert client.get("/health").json() == {"status": "ok"}
```

Starlette's TestClient now uses `httpx2`; using it with plain `httpx` is deprecated. `fastapi[standard]` installs
both.

## Lifespan in tests

`ASGITransport` does not run the app's lifespan. Either build what the lifespan builds in fixtures (usual for the
database), or wrap the app in `LifespanManager` from `asgi-lifespan`.

```python
from asgi_lifespan import LifespanManager


@pytest.fixture
async def client_with_lifespan() -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
```

## Real PostgreSQL with testcontainers

One PostgreSQL container per test session gives every run a clean, real database. Ask testcontainers for an asyncpg
URL directly.

```python
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:18-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()          # postgresql+asyncpg://test:test@localhost:<port>/test
```

- Import from `testcontainers.community.postgres`; `testcontainers.postgres` still works but warns that it is
  deprecated.
- Without `driver="asyncpg"`, `get_connection_url()` returns a `postgresql+psycopg2://` URL, and
  `create_async_engine` fails on the sync driver with an error that points at SQLAlchemy, not at the URL.
- Pin the image to the major version production runs.

## Build the schema with migrations

Run the real Alembic migrations against the container, never `Base.metadata.create_all()`. `create_all` builds what
the models describe; migrations build what production has, and the two diverge at the first hand-written
migration.

```python
@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    cfg = Config(toml_file="pyproject.toml")           # Config("alembic.ini") for the classic layout
    cfg.attributes["database_url"] = database_url      # read by env.py
    await asyncio.to_thread(command.upgrade, cfg, "head")   # env.py calls asyncio.run(): give it its own thread
    eng = create_async_engine(database_url)
    yield eng
    await eng.dispose()
```

## Transaction per test with savepoints

Each test runs inside an outer transaction that is rolled back at the end, so tests start clean without rebuilding
the schema. `join_transaction_mode="create_savepoint"` turns the app's own `commit()` calls into savepoints inside
that transaction.

```python
@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        try:
            yield s
        finally:
            await s.close()
            await outer.rollback()          # nothing the test wrote survives


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
```

Rolling back is faster than truncating and cannot miss a table the way a hand-kept truncate list does.
Code that opens its own sessions from the global factory (background tasks, `SessionFactory()` in a service) does
not see this transaction; inject the session or override the factory for those tests.

## Overriding auth and settings

Override the current-user dependency instead of minting real tokens, so tests do not depend on token formats or an
identity provider. Keep one end-to-end test that does the real login.

```python
@pytest.fixture
def as_user(client: httpx.AsyncClient) -> Iterator[Callable[[User], httpx.AsyncClient]]:
    def login(user: User) -> httpx.AsyncClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return client

    yield login
    app.dependency_overrides.pop(get_current_user, None)
```

## Tests every API needs

Four kinds of tests catch the failures that hurt most; examples are in [references/recipes.md](references/recipes.md).

1. **Isolation**: a user or tenant cannot read or change another's data, proven against the real database for every
   read endpoint. Expect an empty list or 404, never someone else's row.
2. **Contract**: a response keeps every field that released clients read. Removing or renaming a field breaks old
   mobile builds that are still installed.
3. **Audit**: every admin write leaves an audit record with who, what, before and after.
4. **Schema fuzzing**: Schemathesis sends generated requests to every endpoint from the OpenAPI schema and fails on
   any 500 or any response that breaks the documented schema.

## Commands

These pytest commands cover the daily loop, coverage gates and parallel runs.

```bash
uv run pytest                                   # all tests
uv run pytest -x -q                             # stop at the first failure
uv run pytest -k isolation                      # one concern
uv run pytest --lf                              # only the last failures
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=85
uv run pytest -n 4                              # parallel, needs pytest-xdist: one container per worker
```

In CI, Docker must be available before the test step (GitHub's `ubuntu-latest` runners have it); otherwise every
database test errors at setup with a Docker daemon message.

## Verify the test suite itself

A green run proves little if tests never ran, so check the suite as well as the code.

```bash
uv run pytest --collect-only -o addopts="" -q | tail -1         # the test count; compare with the last run
uv run pytest -x -q --cov=app --cov-report=term-missing --cov-fail-under=85
uv run pytest -q -W error::pytest.PytestUnraisableExceptionWarning
grep -rn 'metadata.create_all' tests/                           # expected: nothing
grep -rn 'event_loop\b' tests/                                  # expected: nothing
```

Break something on purpose once: remove a scope filter from a query and confirm the isolation test fails.

## Testing anti-patterns

Each of these keeps the suite green while production breaks.

| Do not | Do instead |
|---|---|
| `Base.metadata.create_all()` in fixtures | run the Alembic migrations |
| SQLite in tests for a PostgreSQL app | testcontainers |
| mock the repository in an isolation test | real rows from two tenants |
| `AsyncClient(app=app)` | `AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")` |
| leave `dependency_overrides` set | clear them after each test |
| truncate tables by a hand-kept list | a rolled-back outer transaction |

## Testing checklist

Go through this FastAPI testing checklist before merging a change.

```text
[ ] asyncio_mode = "auto"; fixture and test loop scopes both "session"
[ ] httpx.AsyncClient(transport=ASGITransport(app=app)); no app= argument
[ ] PostgresContainer(..., driver="asyncpg") from testcontainers.community.postgres
[ ] schema built by alembic upgrade head in a thread; never metadata.create_all
[ ] each test inside a rolled-back transaction with join_transaction_mode="create_savepoint"
[ ] dependency_overrides for session and user, cleared after each test
[ ] isolation, contract, audit and schema tests present for the endpoints that need them
[ ] coverage gate in CI; Docker available in CI
```

## Common FastAPI testing problems

These are the FastAPI testing problems people hit most, each with its symptom, cause and fix.

1. **"attached to a different loop".** A session-scoped async fixture used from a test running in another loop. Set
   both loop scopes to `session`, or mark the test `@pytest.mark.asyncio(loop_scope="session")`.
2. **Data leaks between tests.** A service called `SessionFactory()` itself and committed for real. Inject the session.
3. **`asyncio.run() cannot be called from a running event loop`.** Alembic was called directly from an async fixture.
   Use `asyncio.to_thread(command.upgrade, cfg, "head")`.
4. **Lifespan resources missing in tests.** `ASGITransport` skips the lifespan. Use `LifespanManager` or
   `with TestClient(app)`.
5. **A test expects 403 and gets 401.** Since FastAPI 0.122 missing credentials give 401. Update the assertion.
6. **The isolation test is green, and a user still sees another tenant's rows.** The test mocked the repository, which
   proves the code you wrote, not the isolation. Use real rows from two tenants.

## See also

- fastapi-dependencies: overriding dependencies.
- fastapi-alembic-migrations: running migrations from fixtures.
- fastapi-sqlalchemy-async: sessions and savepoints.
- fastapi-security: attack cases for login and tokens.
- fastapi-version-upgrades: test stack changes by symptom.
