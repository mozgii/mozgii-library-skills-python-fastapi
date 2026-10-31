---
name: fastapi-sqlalchemy-async
description: Connects FastAPI to PostgreSQL with SQLAlchemy 2 async - create_async_engine with asyncpg or psycopg, async_sessionmaker with expire_on_commit=False, a session dependency, typed models with Mapped and mapped_column, naming conventions, eager loading with selectinload and lazy="raise", transactions, pagination, upserts, JSONB and pool sizing, plus SQLModel as the alternative. Use when adding a database to a FastAPI app, writing models or queries, fixing MissingGreenlet or N+1 problems, or choosing between SQLAlchemy and SQLModel.
metadata:
  tags:
  - fastapi
  - sqlalchemy
  - async
  - asyncpg
  - postgres
  - orm
  - sqlmodel
  - database
---

# FastAPI with SQLAlchemy 2 async

This skill wires a FastAPI app to PostgreSQL through SQLAlchemy 2.1's asyncio extension: one engine per process, one
session per request, typed models, and relationships that are loaded on purpose. Versions checked 2026-10-28:
SQLAlchemy 2.1.6, asyncpg 0.32.0, psycopg 3.3.6, SQLModel 0.0.48, PostgreSQL 18.

## Step 0: installed versions for the database layer

Read the versions first, because SQLAlchemy 2.1 changed packaging. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(sqlalchemy|greenlet|asyncpg|psycopg|psycopg-binary|sqlmodel|alembic) '
uv run python --version
```

If `greenlet` is missing, the async extension cannot run: install `sqlalchemy[asyncio]`. On SQLAlchemy 2.0 the
`filter_by()` and autoflush notes below do not apply, and a plain `postgresql://` URL means psycopg2.

## Choose the driver and the ORM layer

Pick these once, at the start: changing them later touches every URL and every model.

| Option | Wins when | Loses because |
|---|---|---|
| SQLAlchemy with asyncpg (`postgresql+asyncpg://`) | async-only services; the default here | PgBouncer in transaction mode needs the settings under Pool sizing |
| SQLAlchemy with psycopg 3 (`postgresql+psycopg://`) | one driver for sync scripts and async code | one more package choice to make: `psycopg[binary]` or a system libpq |
| SQLModel | small and medium apps that want one class for table and schema | less control over mapping and complex relationships, and the same class carries two jobs |

## Install with the asyncio extra

SQLAlchemy 2.1 no longer installs `greenlet` by default; the async extension needs it, so install the `asyncio`
extra and an async driver.

```bash
uv add "sqlalchemy[asyncio]" asyncpg          # or: uv add "sqlalchemy[asyncio]" "psycopg[binary]"
```

SQLAlchemy 2.1 also needs Python 3.11+, and a plain `postgresql://` URL now means psycopg 3, not psycopg2. Always
name the async driver in the URL: `postgresql+asyncpg://...` or `postgresql+psycopg://...`.

## Engine and session factory

Create the engine and the session factory once per process (module level or lifespan), and dispose of the engine on
shutdown.

```python
# app/db.py
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

engine = create_async_engine(
    str(get_settings().database_url),
    pool_size=10,           # connections kept open per process
    max_overflow=5,         # extra connections under burst
    pool_pre_ping=True,     # drop dead connections after a database restart
    pool_recycle=1800,      # reconnect before proxies or firewalls cut idle connections
)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
```

- `expire_on_commit=False` is required in async code: with the default `True`, reading an attribute after `commit()`
  triggers a lazy refresh, which raises `MissingGreenlet`.
- One `AsyncSession` per request, never shared between concurrent tasks. For parallel queries inside one request,
  open a session per task.
- Call `await engine.dispose()` in the lifespan after `yield`.

## Typed declarative models

Models use `DeclarativeBase`, `Mapped[...]` annotations and `mapped_column()`; nullability follows the annotation,
so `Mapped[str | None]` is a nullable column.

```python
# app/models.py
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, MetaData, String, UniqueConstraint, func
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    hashed_password: Mapped[str]
    is_active: Mapped[bool] = mapped_column(default=True)
    items: Mapped[list["Item"]] = relationship(back_populates="owner", lazy="raise")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("owner_id", "title"),)         # one owner cannot repeat a title

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None]
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[User] = relationship(back_populates="items", lazy="raise")
```

- The naming convention gives every constraint and index a stable name, which Alembic needs to drop or alter it
  later. Set it before the first migration.
- Index every foreign key column (`index=True`); PostgreSQL does not do it for you.
- Use `DateTime(timezone=True)` for every timestamp and `server_default=func.now()` so the database fills it.
- `AsyncAttrs` adds `await item.awaitable_attrs.owner` for the rare case where a lazy load in async code is wanted.

## Queries in the 2.0 style

Queries are built with `select()` and run with `session.scalars()`, `session.scalar()` or `session.execute()`.

```python
from sqlalchemy import func, select

item = await session.get(Item, item_id)                                    # by primary key
item = await session.scalar(select(Item).where(Item.id == item_id, Item.owner_id == user_id))
rows = (await session.scalars(select(Item).order_by(Item.id).limit(20))).all()
total = await session.scalar(select(func.count()).select_from(Item).where(Item.owner_id == user_id))
```

- `scalar()` returns the first column of the first row or `None`; `scalars()` returns an iterable of the first
  column; `execute()` returns rows of several columns.
- Add an owner or tenant condition to every query on user data; the `WHERE` clause is the authorization check.
- `filter_by()` in 2.1 searches every entity in the `FROM` and raises `AmbiguousColumnError` on a name present in
  two joined tables; prefer explicit `where(Model.column == value)`.

## Relationships and the N+1 problem

In async code a lazy load cannot run implicitly, so load relationships in the query that needs them. Setting
`lazy="raise"` turns a forgotten load into an immediate error instead of a `MissingGreenlet` deep in serialization.

```python
from sqlalchemy.orm import joinedload, selectinload

users = await session.scalars(select(User).options(selectinload(User.items)))       # one-to-many: 2 queries
items = await session.scalars(select(Item).options(joinedload(Item.owner)))         # many-to-one: 1 join
```

`selectinload` for collections, `joinedload` for single related objects, `raiseload("*")` to forbid everything else
in a query. Loader strategies, nested loading and counting related rows: [references/loading.md](references/loading.md).

## Transactions and commits

The service (or repository) that changes data owns the commit; on failure it rolls back before re-raising.

```python
from sqlalchemy.exc import IntegrityError


async def create_item(session: AsyncSession, owner_id: int, data: ItemCreate) -> Item:
    item = Item(**data.model_dump(), owner_id=owner_id)
    session.add(item)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise DuplicateItem(data.title) from None
    await session.refresh(item)             # load server defaults such as created_at
    return item
```

- For several writes that must succeed together, make them all on the request's session, then `commit()` once; if
  anything fails first, nothing is saved. `async with session.begin():` fails here (problem 8 below).
- SQLAlchemy 2.1 autoflushes before every execution, including `text()` statements; pending objects are written
  before your raw query runs.
- Never commit inside a loop over rows; collect the changes and commit once.

## Concurrent updates and lost writes

Two requests that read the same row, change it and save it will silently overwrite each other: the second save wins
and the first change is lost. Pick one of two guards, by how often conflicts happen.

```python
# pessimistic: lock the row for this transaction; others wait (short critical sections, counters, balances)
account = await session.scalar(select(Account).where(Account.id == account_id).with_for_update())
account.balance -= amount
await session.commit()


# optimistic: a version column; the UPDATE matches only if nobody changed the row since it was read
class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str]
    version: Mapped[int] = mapped_column(default=1)
    __mapper_args__ = {"version_id_col": version}
```

- With `version_id_col`, a stale update raises `sqlalchemy.orm.exc.StaleDataError`; map it to 409 Conflict (or 412
  when the client sent `If-Match`).
- For APIs edited in browsers, send the version as an `ETag` header on GET and require `If-Match` on PUT and PATCH;
  a mismatch answers 412 Precondition Failed before any write.
- A single atomic statement needs neither: `update(Item).where(Item.id == i).values(stock=Item.stock - 1)`.
- `with_for_update(skip_locked=True)` lets several workers take different rows from a queue table without waiting.

## Pagination

Offset pagination is simple and fine for small tables; keyset pagination stays fast on large ones because it uses an
index instead of skipping rows.

```python
# offset: page 1000 still reads and discards 20,000 rows
stmt = select(Item).order_by(Item.id).limit(limit).offset(offset)

# keyset: the client sends the last id it saw
stmt = select(Item).where(Item.id > after_id).order_by(Item.id).limit(limit)
```

Always order by a unique column; without it pages overlap or skip rows.

## Upserts and JSONB on PostgreSQL

`INSERT ... ON CONFLICT` and JSONB columns have their own traps: an upsert is safe under concurrency where "select,
then insert" is not, and SQLAlchemy does not notice in-place JSONB changes unless told. Code for both is in
[references/postgres-features.md](references/postgres-features.md).

## Pool sizing

Each worker process has its own pool, so the database sees `workers × (pool_size + max_overflow)` connections at
peak. Keep that under PostgreSQL's `max_connections` (100 by default) minus what migrations, admins and other services
need.

- 4 workers × (10 + 5) = 60 connections.
- With PgBouncer in transaction mode, use `poolclass=NullPool` in the app, and for asyncpg switch off both prepared
  statement caches and give each statement a unique name, because prepared statements do not survive across pooled
  connections and asyncpg's numbered names can clash there. SQLAlchemy's asyncpg docs show the last two arguments and
  also ask you to configure PgBouncer to run `DISCARD` when a connection returns.

```python
from uuid import uuid4

from sqlalchemy.pool import NullPool

engine = create_async_engine(
    "postgresql+asyncpg://app@pgbouncer:6432/app",
    poolclass=NullPool,
    connect_args={
        "statement_cache_size": 0,                 # asyncpg's own cache
        "prepared_statement_cache_size": 0,        # the cache in SQLAlchemy's asyncpg dialect
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    },
)
```

## SQLModel as the alternative

SQLModel (by FastAPI's author) builds on SQLAlchemy and Pydantic, so one class can be both a table and a schema; the
official full-stack template uses it with a sync session. Use it for small and medium apps that want fewer classes;
use plain SQLAlchemy when you need full control over mapping, async-first code or complex relationships. Patterns and
limits: [references/sqlmodel.md](references/sqlmodel.md).

## Verify queries and sessions

Count statements per request to catch N+1 before it ships, and prove models match the migrations.

```python
from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
def statements(engine: AsyncEngine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_list_query_count(session: AsyncSession, statements: list[str], many_users: None) -> None:
    users = (await session.scalars(select(User).options(selectinload(User.items)))).all()
    assert len(users) > 1 and all(user.items is not None for user in users)
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 2
```

```bash
uv run pytest -k "query_count or isolation"
uv run alembic check                                            # models and migrations agree
uv run python -c "import greenlet; print(greenlet.__version__)"  # the asyncio extra is installed
psql postgresql://app@localhost/app -c 'SHOW max_connections'   # above workers × (pool_size + max_overflow)
```

## Async SQLAlchemy anti-patterns

Each of these has caused a production incident somewhere.

| Do not | Do instead |
|---|---|
| `expire_on_commit=True` in async code | `async_sessionmaker(engine, expire_on_commit=False)` |
| share one `AsyncSession` across `asyncio.gather` tasks | one session per task |
| rely on lazy loading | `lazy="raise"`, then `selectinload` or `joinedload` in the query |
| commit inside a loop over rows | collect the changes and commit once |
| `create_engine` per request | one engine per process, disposed in the lifespan |
| `DateTime` without a timezone | `DateTime(timezone=True)` |
| read, change, save a counter with no guard | a row lock, a version column, or one atomic `UPDATE` |

## Async SQLAlchemy checklist

Go through this async SQLAlchemy checklist before merging a change.

```text
[ ] sqlalchemy[asyncio] installed; URL names the async driver
[ ] one engine per process, disposed in lifespan; async_sessionmaker(expire_on_commit=False)
[ ] one session per request via a yield dependency
[ ] models use Mapped/mapped_column; naming convention on the metadata; FKs indexed; timestamps timezone-aware
[ ] relationships lazy="raise"; every needed relation loaded with selectinload/joinedload
[ ] every query on user data filters by owner or tenant
[ ] commits owned by the service; rollback on failure; no commit in loops
[ ] pagination ordered by a unique column; keyset for large tables
[ ] read-modify-write paths guarded by a row lock or a version column
[ ] workers × (pool_size + max_overflow) below max_connections
```

## Common async SQLAlchemy problems

These are the async SQLAlchemy problems people hit most, each with its symptom, cause and fix.

1. **`MissingGreenlet: greenlet_spawn has not been called`.** A lazy load or an expired attribute was accessed in
   async code. Set `expire_on_commit=False`, load the relationship with `selectinload`, or use
   `await obj.awaitable_attrs.name`.
2. **`No module named 'greenlet'` after upgrading to 2.1.** Install `sqlalchemy[asyncio]`.
3. **`InvalidRequestError: 'Item.owner' is not available due to lazy='raise'`.** Working as intended: add the loader
   option to the query.
4. **`InvalidRequestError` ending in `The loaded 'psycopg2' is not async.`** `postgresql+psycopg2://` names a sync
   driver, so the async engine rejects it. A plain `postgresql://` is not rejected: on SQLAlchemy 2.1 it selects
   psycopg's async dialect, and with only asyncpg installed it stops with `No module named 'psycopg'` instead. Name
   the driver: `postgresql+asyncpg://`.
5. **`asyncpg.exceptions.InvalidCachedStatementError` or `cache lookup failed for type <oid>` after a migration.** DDL
   from another process left a cached statement or type stale in a pooled connection. SQLAlchemy clears the caches
   when the error is raised, but it cannot retry, because the transaction is already invalid. Run migrations before
   the new code takes traffic, and retry the failed request.
6. **`prepared statement "__asyncpg_stmt_1__" already exists` behind PgBouncer.** Numbered statement names clash across
   server connections. Set `statement_cache_size=0` and `prepared_statement_cache_size=0`, pass
   `prepared_statement_name_func`, and use `NullPool`, as shown under Pool sizing.
7. **Tests pass, production deadlocks.** A session shared across `asyncio.gather` tasks. One session per task.
8. **`InvalidRequestError: A transaction is already begun on this Session.`** `session.begin()` ran after a read. Any
   read, including the current-user lookup, starts the session's transaction. Write everything, then `commit()` once.

## See also

- fastapi-repository-pattern: where queries and commits live.
- fastapi-alembic-migrations: schema changes.
- fastapi-testing: real PostgreSQL in tests.
- fastapi-performance: finding slow queries.
- fastapi-deployment: connections across workers and replicas.
