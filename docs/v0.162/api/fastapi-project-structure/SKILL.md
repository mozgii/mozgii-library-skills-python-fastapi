---
name: fastapi-project-structure
description: Starts and organizes a FastAPI backend - uv project, fastapi[standard], the fastapi dev and run commands, the entrypoint in pyproject.toml, folder layout by domain, APIRouter and include_router, the lifespan function for startup and shutdown, and typed settings with pydantic-settings. Use when creating a new FastAPI project, adding a module or router, deciding where code goes, wiring startup and shutdown, or loading configuration from environment variables.
metadata:
  tags:
  - fastapi
  - project-structure
  - uv
  - apirouter
  - lifespan
  - pydantic-settings
  - configuration
---

# FastAPI project structure

This skill sets up a FastAPI backend that stays easy to change as it grows: one uv project, one app object, one
router per domain, startup and shutdown in a lifespan function, and settings read once from the environment.

Versions checked on 2026-10-31: FastAPI 0.162.0, Starlette 1.10.0, Pydantic 2.14.2, pydantic-settings 2.16.2,
Uvicorn 0.54.0, uv 0.13.6. Stay on Pydantic 2.14.2 or later: that release patches the advisories GHSA-qpmf-fgh3-ff25
and GHSA-3r8g-cqwr-jr29. FastAPI needs Python 3.10 or later; Starlette 1.8+ and SQLAlchemy 2.1 need Python 3.11, so
start new projects on Python 3.13 or 3.14.

## Step 0: installed versions for project setup

Read what the project really runs before applying this skill. A missing line means the package is not installed.

```bash
uv run python --version
uv pip list | grep -iE '^(fastapi|fastapi-cli|starlette|pydantic|pydantic-settings|uvicorn) '
```

Below FastAPI 0.137 included routers are copied rather than kept as a tree, and below 0.146 `FastAPI(root_path=...)`
carries no deprecation, so those two notes do not apply yet. An older stack gets upgraded before new structure goes
on top of it (fastapi-version-upgrades).

## Choose a project layout

The layout decides how many folders one change touches, so pick it by the size of the app and the team.

| Layout | Wins when | Loses because |
|---|---|---|
| By domain: `app/items/`, `app/users/`, each with router, schemas, models, service | teams, long-lived services, anything past five or six resources: a feature is one folder and deleting a domain is deleting that folder | a two-route prototype ends up with packages that hold one line of real code |
| By layer: `api/routes/`, `models.py`, `crud.py` (the official full-stack template) | small and medium apps, and teaching: every route file looks the same | `models.py` and `crud.py` grow without limit, and unrelated changes collide in them |
| Single module: `app/main.py`, `models.py`, `schemas.py` | a prototype or a one-developer tool | past a third domain or a second developer it turns into one long file; moving to packages then changes imports only |

## Create the project with uv

A new FastAPI project starts with `uv init` and the `fastapi[standard]` extra, which brings the `fastapi` CLI,
Uvicorn, python-multipart, email-validator, pydantic-settings, the OpenTelemetry SDK and the TestClient's HTTP client.

```bash
uv init --app --python 3.14 items-api
cd items-api
uv add "fastapi[standard]" "sqlalchemy[asyncio]" asyncpg alembic
uv add --dev pytest pytest-asyncio pytest-cov "testcontainers[postgres]" ruff pyright
```

Use plain `fastapi` (without `[standard]`) only for a library or a slim image where you pick every dependency
yourself. `fastapi-slim` is no longer released.

## Declare the entrypoint in pyproject.toml

The FastAPI CLI finds the app through `[tool.fastapi] entrypoint`, so every tool (the CLI, editors, deploy scripts)
imports the same object without guessing.

```toml
[tool.fastapi]
entrypoint = "app.main:app"
```

```bash
uv run fastapi dev            # reload on, listens on 127.0.0.1:8000, sets FASTAPI_ENV=development
uv run fastapi run            # production mode: no reload, listens on 0.0.0.0:8000
uv run fastapi run --workers 4 --proxy-headers --forwarded-allow-ips "10.0.0.0/8"
```

`fastapi dev` sets `FASTAPI_ENV=development` before importing the app unless it is already set; `fastapi run` leaves
it alone, so set `FASTAPI_ENV=production` yourself when code branches on it. `--reload` and `--workers` cannot be
combined.

## Folder layout by domain

A domain layout keeps everything about one business area (items, users, billing) in one package, so a change touches
one folder and a domain can be deleted cleanly. Split by layer only inside a domain.

```text
items-api/
├── pyproject.toml          dependencies, [tool.fastapi], [tool.pytest.ini_options], [tool.ruff], [tool.alembic]
├── uv.lock
├── migrations/             Alembic env.py and versions/
├── app/
│   ├── main.py             create the FastAPI app, lifespan, include routers, exception handlers
│   ├── config.py           Settings (pydantic-settings) and get_settings()
│   ├── db.py               engine, session factory, SessionDep
│   ├── deps.py             cross-domain dependencies: CurrentUser, pagination
│   ├── items/
│   │   ├── router.py       path operations only: parse, call the service, return a schema
│   │   ├── schemas.py      Pydantic request and response models
│   │   ├── models.py       SQLAlchemy ORM models
│   │   ├── service.py      business rules; no FastAPI imports
│   │   ├── repository.py   database access (see fastapi-repository-pattern)
│   │   └── exceptions.py   domain errors mapped to HTTP in main.py
│   └── users/ ...
└── tests/
    ├── conftest.py
    └── items/test_router.py
```

Rules that keep it clean:

- `router.py` imports from `service.py`, never the other way round. Services take plain arguments and an
  `AsyncSession`, so a CLI job or a worker can call them without HTTP.
- Domains import each other only through their `service.py` or `schemas.py`, never through another domain's models.
- Small apps can start with `app/main.py`, `app/models.py`, `app/schemas.py` and `app/routers/`; move to domain
  packages when a second developer or a third domain arrives.

For the layered alternative and how the official full-stack template arranges `api/`, `core/` and `crud.py`, read
[references/layouts.md](references/layouts.md).

## APIRouter per domain

An `APIRouter` groups the path operations of one domain and carries their shared prefix, tags, dependencies and
error responses, so `main.py` only includes it.

```python
# app/items/router.py
from fastapi import APIRouter, Depends

from app.deps import get_current_user

router = APIRouter(
    prefix="/items",
    tags=["items"],
    dependencies=[Depends(get_current_user)],
    responses={404: {"description": "Item not found"}},
)


@router.get("/")
async def list_items() -> list[str]:
    return []
```

```python
# app/main.py
from fastapi import FastAPI

from app.items.router import router as items_router

app = FastAPI(title="Items API")
app.include_router(items_router)
```

- Put `prefix`, `tags` and shared `dependencies` on the router itself rather than on `include_router()`, so the router
  reads the same wherever it is mounted.
- Since FastAPI 0.137 an included router is kept as a tree, not copied: routes added after `include_router()` still
  work. Code that walked `app.routes` as a flat list of `APIRoute` breaks; use `fastapi.routing.iter_route_contexts()`
  or the OpenAPI schema instead.
- Version an API with a parent router: `v1 = APIRouter(prefix="/v1")`, include domain routers into it, then include
  `v1` into the app.

## Lifespan for startup and shutdown

The lifespan function runs code once before the app takes requests and once after it stops: open pools, warm caches,
start a task group, and close them in reverse order. Starlette 1.0 removed `on_startup`, `on_shutdown` and
`@app.on_event`. FastAPI still accepts them, and only `@app.on_event` warns at runtime, so write new code with
`lifespan` only.

```python
# app/main.py
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TypedDict

import httpx
from fastapi import FastAPI, Request

from app.db import engine


class State(TypedDict):
    http: httpx.AsyncClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[State]:
    async with httpx.AsyncClient(timeout=10) as http:
        yield {"http": http}          # becomes request.state.http in every request
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.get("/rates")
async def rates(request: Request) -> dict[str, float]:
    http: httpx.AsyncClient = request.state.http
    response = await http.get("https://rates.example.com/latest")
    return response.json()
```

- Annotate the lifespan as `AsyncGenerator[...]`. Type checkers mark `@asynccontextmanager` on a function returning
  `AsyncIterator[...]` as deprecated.
- Prefer lifespan state (the yielded dict) over module globals and `app.state`: it is created per app instance, so
  tests that build a fresh app get fresh clients.
- A failure before `yield` stops the server from starting. That is what you want for a missing database; do not catch
  it.
- `httpx.AsyncClient` used in tests does not run the lifespan; see fastapi-testing.

## Settings with pydantic-settings

Typed settings read environment variables once, validate them at startup and fail fast on a missing secret. Put them
in `app/config.py` and read them through a cached function so tests can override them.

```python
# app/config.py
from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    environment: Literal["development", "staging", "production"] = "development"
    database_url: PostgresDsn
    secret_key: SecretStr                      # required: no default for a secret
    access_token_minutes: int = 30
    cors_origins: list[str] = []               # APP_CORS_ORIGINS='["https://app.example.com"]'


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from the environment
```

- `SecretStr` keeps the value out of `repr()`, logs and tracebacks; read it with `.get_secret_value()` only where it is
  used.
- Commit a `.env.example` with placeholder values, never `.env`.
- More patterns (nested settings, per-domain settings, secrets files, overriding in tests): see
  [references/settings.md](references/settings.md).

## Tooling in pyproject.toml

One `pyproject.toml` configures the linter, the type checker and the tests, so every developer and CI run the same
checks.

```toml
[tool.ruff]
target-version = "py314"
line-length = 100

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "C4", "UP", "ASYNC", "S", "FAST", "RUF"]
ignore = ["B008"]          # Depends() in defaults is the FastAPI idiom

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]      # assert is fine in tests

[tool.pyright]
typeCheckingMode = "strict"
include = ["app", "tests"]
venvPath = "."
venv = ".venv"
```

Ruff's `FAST` rules catch FastAPI mistakes (redundant `response_model`, non-`Annotated` dependencies, unused path
parameters) and `ASYNC` catches blocking calls inside `async def`. Run them with
`uv run ruff check . && uv run ruff format --check . && uv run pyright`.

## Verify the project setup

These commands prove the app imports without side effects, refuses to start without its secrets, and serves its
schema from the entrypoint.

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright
# expected: ValidationError naming secret_key and database_url (run where no .env exists)
env -u APP_SECRET_KEY -u APP_DATABASE_URL uv run python -c "from app.config import get_settings; get_settings()"
# expected: a path count with no database running; a connection error means import-time side effects
export APP_SECRET_KEY=placeholder APP_DATABASE_URL=postgresql+asyncpg://app@127.0.0.1:1/app
uv run python -c "from app.main import app; print(len(app.openapi()['paths']), 'paths')"
git check-ignore .env                     # expected: .env
```

Then start the real entrypoint once: `uv run fastapi run --port 8001` must print its startup lines, and
`curl -fsS http://127.0.0.1:8001/openapi.json` must answer.

## Project setup anti-patterns

Each of these works on day one and costs more every week after.

| Do not | Do instead |
|---|---|
| `@app.on_event("startup")` or `FastAPI(on_startup=[...])` | a `lifespan` function; both old forms are gone from Starlette 1.0 |
| connect to a database or read files at module level | open them in the lifespan, so imports stay free of side effects |
| `os.environ[...]` scattered through the code | one `Settings` class read through `get_settings()` |
| a default value on a secret | `SecretStr` with no default, so a missing secret stops the start |
| `prefix` and `tags` on every `include_router()` call | put them on the router, which then reads the same wherever it is mounted |
| one domain importing another domain's models or router | import its `service.py` or `schemas.py` |
| `FastAPI(root_path="/api")` | `fastapi run --root-path /api` |

## Project setup checklist

Go through this project setup checklist before merging a change.

```text
[ ] uv project with fastapi[standard]; uv.lock committed
[ ] [tool.fastapi] entrypoint set; fastapi dev starts without arguments
[ ] one package per domain: router, schemas, models, service, repository
[ ] routers carry prefix, tags and shared dependencies; main.py only includes them
[ ] startup and shutdown live in lifespan; no on_event, no on_startup
[ ] Settings via pydantic-settings; secrets are SecretStr without defaults; .env not committed
[ ] ruff (with FAST and ASYNC) and pyright strict configured in pyproject.toml
```

## Common project setup problems

These are the project setup problems people hit most, each with its symptom, cause and fix.

1. **`fastapi dev` serves the wrong app, or says it cannot find one.** Without `[tool.fastapi] entrypoint` the CLI
   searches for `main.py` or `app.py` and an object named `app`, and a second `FastAPI()` in a test helper can win. Set
   the entrypoint.
2. **Tests or every worker try to reach the database on import.** Code at module level in `main.py` (connecting to the
   database, reading files) runs on import, in every worker and in every test. Move it into the lifespan.
3. **`ImportError: ... most likely due to a circular import`.** A service imported another domain's router. `items.models`
   importing `users.models` for a relationship is fine; the rest keeps the arrow pointing router → service → repository
   → models.
4. **`FastAPIDeprecationWarning` for `root_path` at startup.** `FastAPI(root_path=...)` is deprecated since 0.146. Pass
   `--root-path` to `fastapi run` or configure the proxy (see fastapi-deployment).
5. **A secret turns up in a built image.** A stale `.env` was baked in: pydantic-settings reads `.env` if present, and
   though it overrides nothing (environment variables win), it leaks secrets into the image. Add `.env` to
   `.dockerignore`.

## See also

- fastapi-repository-pattern: what goes in `service.py` and `repository.py`.
- fastapi-sqlalchemy-async: `app/db.py`, engine and session.
- fastapi-testing: the lifespan in tests.
- fastapi-deployment: `fastapi run` behind a proxy, `--root-path` and the Docker image.
- fastapi-version-upgrades: moving an older project to these versions.
