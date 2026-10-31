---
title: FastAPI conftest.py for async tests on real Postgres
author: MOZGII, Inc.
lang: en
---

# FastAPI conftest.py for async tests on real Postgres

This reference holds a complete `tests/conftest.py` and a test module that pass as written (verified 2026-10-28 with
FastAPI 0.162, pytest 9.2, pytest-asyncio 1.4.1, testcontainers 4.15, SQLAlchemy 2.1.6, asyncpg 0.32 and the
`postgres:18-alpine` image). Copy it, then change the imports to your app.

## Contents

This reference on FastAPI conftest.py for async tests on real Postgres has these sections:

- What the fixtures do
- pyproject.toml settings
- tests/conftest.py
- tests/test_items.py
- Adapting it

## What the fixtures do

The fixtures start one PostgreSQL container per session, run the Alembic migrations once, give each test a session
inside a transaction that is rolled back, and point the app's session dependency at that session.

| Fixture | Scope | Gives |
|---|---|---|
| `database_url` | session | an asyncpg URL to a fresh PostgreSQL container |
| `engine` | session | an `AsyncEngine` on a schema built by `alembic upgrade head` |
| `session` | test | an `AsyncSession` whose commits become savepoints; everything rolled back after the test |
| `client` | test | an `httpx.AsyncClient` calling the app in-process with the session override |

## pyproject.toml settings

The pytest settings below make async tests run without markers in one shared event loop.

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-q --strict-markers --strict-config"
```

## tests/conftest.py

The conftest assumes `app.main:app`, `app.db:get_session`, and an Alembic `env.py` that reads
`config.attributes["database_url"]` (see fastapi-alembic-migrations).

```python
import asyncio
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from app.db import get_session
from app.main import app


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:18-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    cfg = Config(toml_file="pyproject.toml")
    cfg.attributes["database_url"] = database_url
    # env.py calls asyncio.run(); a worker thread gives it a loop of its own.
    await asyncio.to_thread(command.upgrade, cfg, "head")
    eng = create_async_engine(database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        try:
            yield s
        finally:
            await s.close()
            await outer.rollback()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
```

## tests/test_items.py

These tests register two users through the API, prove that one cannot see the other's items, check that each test
starts with an empty table, and confirm FastAPI's 401 and strict Content-Type behaviour.

```python
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Item


async def auth_headers(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    r = await client.post("/auth/register", json={"email": email, "password": "correct horse battery"})
    assert r.status_code == 201, r.text
    r = await client.post("/auth/token", data={"username": email, "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_create_and_list_items(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client, "a@example.com")
    r = await client.post("/items/", json={"title": "First"}, headers=h)
    assert r.status_code == 201
    r = await client.get("/items/", headers=h)
    assert [i["title"] for i in r.json()] == ["First"]


async def test_user_cannot_see_other_users_items(client: httpx.AsyncClient) -> None:
    alice = await auth_headers(client, "alice@example.com")
    bob = await auth_headers(client, "bob@example.com")
    await client.post("/items/", json={"title": "Private"}, headers=alice)
    r = await client.get("/items/", headers=bob)
    assert r.status_code == 200
    assert r.json() == []


async def test_rollback_isolates_tests(session: AsyncSession) -> None:
    assert await session.scalar(select(func.count()).select_from(Item)) == 0


async def test_missing_token_is_401(client: httpx.AsyncClient) -> None:
    r = await client.get("/items/")
    assert r.status_code == 401


async def test_patch_keeps_unset_fields(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client, "p@example.com")
    item = (await client.post("/items/", json={"title": "T", "description": "D"}, headers=h)).json()
    r = await client.patch(f"/items/{item['id']}", json={"title": "T2"}, headers=h)
    assert r.json()["description"] == "D"


async def test_json_without_content_type_is_rejected(client: httpx.AsyncClient) -> None:
    h = await auth_headers(client, "c@example.com")
    r = await client.post("/items/", content=b'{"title": "x"}', headers=h)
    assert r.status_code == 422
```

## Adapting it

Three changes cover most apps.

- **Classic `alembic.ini`**: replace `Config(toml_file="pyproject.toml")` with `Config("alembic.ini")`.
- **psycopg instead of asyncpg**: `PostgresContainer(..., driver="psycopg")` and `uv add "psycopg[binary]"`.
- **Sync SQLModel apps** (the full-stack template style): use `TestClient`, a sync engine
  (`driver="psycopg"`), `Session(bind=conn, join_transaction_mode="create_savepoint")` and plain `def` tests; the
  structure stays the same.
