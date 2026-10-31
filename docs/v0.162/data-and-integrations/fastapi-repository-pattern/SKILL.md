---
name: fastapi-repository-pattern
description: Separates database access from FastAPI routes with a service and repository layer on SQLAlchemy 2 async - query functions that never commit, a repository or service that owns the transaction, scope guards in every WHERE clause, domain exceptions instead of HTTPException, and model docstrings that state loading rules. Use when a router contains queries, when commits are scattered, when the same query is written twice, or when structuring the data layer of a new FastAPI domain.
metadata:
  tags:
  - fastapi
  - repository-pattern
  - service-layer
  - sqlalchemy
  - architecture
  - transactions
---

# FastAPI repository and service layers

This skill keeps SQL out of route handlers and gives every transaction one owner. The layers are small on purpose:
a route translates HTTP, a service applies business rules and commits, a repository (or plain query functions) talks
to the database.

## Step 0: installed versions for the data layer

Read the versions the data layer runs on. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastapi|sqlalchemy|sqlmodel|asyncpg|psycopg) '
```

The examples here need SQLAlchemy 2.0 or later (`select()`, `Mapped`, `AsyncSession`). Code still on `session.query()`
runs, but move it to `select()` as you touch it.

## Choose how much data layer to write

More layers cost more code, so add them only when they pay for themselves.

| Style | Wins when | Loses because |
|---|---|---|
| Plain query functions in `repository.py` (default) | most services: each query has a name, scope guards are in one place, tests can call it | a second storage backend needs a seam that functions do not give you |
| Class repository (`ItemRepository(session)`) | you prefer methods, or need several implementations behind one interface | more ceremony than functions when there is one database |
| Queries directly in the service | small apps and prototypes; keep only the rule "routers do not query" | the same query soon gets written twice, with a different `WHERE` |
| Generic `BaseRepository[T]` with `get/list/create/update/delete` | almost never | it hides the queries that matter (filters, loading, scope) behind a uniform API that fits none of them |

## The three layers

Each layer has one job and may import only the layer below it.

```text
router.py      HTTP in and out: parse, call the service, map domain errors to status codes, return a schema
service.py     business rules and the transaction: validates state, calls the repository, commits or rolls back
repository.py  database access: select/insert/update/delete with scope guards; never commits
models.py      ORM classes; docstrings say how to load them
```

- Routers import from `service.py`, never from `repository.py` or `sqlalchemy`.
- Services take an `AsyncSession` and plain arguments; they import no FastAPI names, so a CLI command, a test or a
  background worker can call them.
- Repositories never call `commit()` or `rollback()`. They may call `flush()` to get generated ids.

## Repository functions

A repository function is an `async def` that runs one query and returns typed ORM objects or `None`. Every query on
owned data carries the owner or tenant condition, which makes the `WHERE` clause the authorization check.

```python
# app/items/repository.py
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.items.models import Item


async def get_owned(session: AsyncSession, item_id: int, owner_id: int) -> Item | None:
    return await session.scalar(
        select(Item).where(Item.id == item_id, Item.owner_id == owner_id)     # scope guard
    )


async def list_owned(session: AsyncSession, owner_id: int, limit: int, offset: int) -> Sequence[Item]:
    stmt = (
        select(Item)
        .where(Item.owner_id == owner_id)
        .options(selectinload(Item.tags))
        .order_by(Item.id)
        .limit(limit)                      # always bounded
        .offset(offset)
    )
    return (await session.scalars(stmt)).all()


async def count_owned(session: AsyncSession, owner_id: int) -> int:
    return await session.scalar(select(func.count()).select_from(Item).where(Item.owner_id == owner_id)) or 0


def add(session: AsyncSession, item: Item) -> Item:
    session.add(item)                      # sync: session.add does no I/O
    return item
```

`add()` is a plain `def` because `session.add()` does no I/O; call it without `await`.

A class-based repository (`ItemRepository(session)`) is equally valid when you prefer methods or need several
implementations; the rules are the same. Pick one style per codebase.

## Service functions own the transaction

A service function decides whether an operation is allowed, calls the repository, and commits once. On a failed
commit it rolls back and raises a domain exception.

```python
# app/items/service.py
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.items import repository
from app.items.exceptions import ItemNotFound, ItemTitleTaken
from app.items.models import Item
from app.items.schemas import ItemCreate, ItemUpdate


async def create(session: AsyncSession, owner_id: int, data: ItemCreate) -> Item:
    item = repository.add(session, Item(**data.model_dump(), owner_id=owner_id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ItemTitleTaken(data.title) from None
    await session.refresh(item)
    return item


async def update(session: AsyncSession, item_id: int, owner_id: int, data: ItemUpdate) -> Item:
    item = await repository.get_owned(session, item_id, owner_id)
    if item is None:
        raise ItemNotFound(item_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    await session.commit()
    return item
```

When one business operation spans several services, the outer one owns the commit and the inner ones only flush;
or pass a flag such as `commit=False`. Never let two layers both commit the same unit of work.

## Domain exceptions, mapped once

Services raise domain exceptions; `main.py` maps them to HTTP. That keeps HTTP status codes out of code that also
runs in workers and scripts.

```python
# app/items/exceptions.py
class ItemNotFound(Exception):
    def __init__(self, item_id: int) -> None:
        super().__init__(f"item {item_id} not found")
        self.item_id = item_id


class ItemTitleTaken(Exception):
    def __init__(self, title: str) -> None:
        super().__init__("title already used")
        self.title = title
```

```python
# app/main.py
from fastapi import Request
from fastapi.responses import JSONResponse


@app.exception_handler(ItemNotFound)
async def item_not_found(request: Request, exc: ItemNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"title": "Item not found", "status": 404})


@app.exception_handler(ItemTitleTaken)
async def title_taken(request: Request, exc: ItemTitleTaken) -> JSONResponse:
    return JSONResponse(status_code=409, content={"title": "Title already used", "status": 409})
```

Never build an error message from the database exception (`f"Failed: {exc}"`); it exposes table and constraint
names.

## Thin routers on top

With the layers in place a route handler is a few lines of translation.

```python
# app/items/router.py
@router.patch("/{item_id}")
async def update_item(item_id: int, body: ItemUpdate, session: SessionDep, user: CurrentUser) -> ItemRead:
    item = await items_service.update(session, item_id, user.id, body)
    return ItemRead.model_validate(item)
```

## Model docstrings as loading rules

A short docstring on each model tells the next developer (and an AI assistant) how to load it and how big it gets.

```python
class Order(Base):
    """A customer's order.

    Load lines with selectinload(Order.lines); customer with joinedload(Order.customer).
    Grows without bound: always paginate, never select all.
    Writes go through orders.service; status changes only through service.transition().
    """
```

## When not to add layers

A repository layer is a cost; skip it where it adds nothing.

- A small app or a prototype: services can run queries directly; keep only the rule "routers do not query".
- Read-only reporting endpoints with one complex query: a single query function called by the router through a
  thin service is enough.
- Do not wrap SQLAlchemy in a generic `BaseRepository[T]` with `get/list/create/update/delete` for every model; it
  hides the queries that matter (filters, loading, scope) behind a uniform API that fits none of them.

## Verify the layer boundaries

Each `grep` line must print nothing; the last line runs the tests that prove the scope guards on real rows.

```bash
grep -rnE 'select\(|from sqlalchemy' app/*/router.py            # queries in a router
grep -rnE '\.commit\(|\.rollback\(' app/*/repository.py         # transactions in a repository
grep -rnE 'HTTPException|from fastapi' app/*/service.py app/*/repository.py   # HTTP below the router
uv run pytest -k "isolation or rollback or conflict"
```

A scope test needs two owners. It must get `None` for the other owner's row, and a conflict test must leave the
session usable:

```python
async def test_get_owned_is_scoped(session: AsyncSession, alice: User, bob: User) -> None:
    item = repository.add(session, Item(title="private", owner_id=alice.id))
    await session.flush()
    assert await repository.get_owned(session, item.id, bob.id) is None
    assert await repository.get_owned(session, item.id, alice.id) is item


async def test_conflict_rolls_back(session: AsyncSession, alice: User) -> None:
    owner_id = alice.id                                  # a rollback expires alice, so read her id first
    await service.create(session, owner_id, ItemCreate(title="same"))
    with pytest.raises(ItemTitleTaken):
        await service.create(session, owner_id, ItemCreate(title="same"))
    assert await repository.count_owned(session, owner_id) == 1      # no PendingRollbackError
```

## Repository and service anti-patterns

These break the one rule of this skill: one owner per transaction, and SQL in one layer.

| Do not | Do instead |
|---|---|
| `await session.commit()` in a repository | commit in the service, once per unit of work |
| `raise HTTPException` in a service or repository | raise a domain exception and map it once in `main.py` |
| load by id, then `if item.owner_id != user.id` | put the owner in the `WHERE` clause |
| an unbounded `select(Item)` | a `limit` on every read |
| `f"Failed: {exc}"` from a database error | a fixed message; the exception goes to the log |
| a `commit=False` flag on many functions | let the outer service own the commit and the inner ones flush |

## Repository and service layer checklist

Go through this repository and service layer checklist before merging a change.

```text
[ ] routers import only services and schemas; no select() in router.py
[ ] repository functions never commit or roll back; reads are bounded with limit
[ ] every query on owned data filters by owner or tenant id
[ ] each unit of work committed exactly once, by the service; rollback before re-raise
[ ] services raise domain exceptions; HTTP mapping lives in main.py
[ ] no database error text in any response
[ ] model docstrings state loading hints, size and write rules
```

## Common repository and service layer problems

These are the repository and service layer problems people hit most, each with its symptom, cause and fix.

1. **Half an operation stays in the database after a later step failed.** A repository committed and the service
   committed again; the first commit made that half permanent. Commits belong to the service only.
2. **A background job fails with an HTTP error nobody can handle.** `HTTPException` in the repository works until a
   job calls the same code. Raise domain exceptions.
3. **A new endpoint shows one user another user's row.** The scope guard was in Python: loading by id and then checking
   `item.owner_id != user.id` works, but the same query copied elsewhere forgets the check. Put the condition in the
   `WHERE`.
4. **`PendingRollbackError` on every statement after a failed flush.** The rollback is missing, so the session stays
   in a failed state. Roll back before re-raising.

## See also

- fastapi-sqlalchemy-async: sessions, loading and transactions.
- fastapi-errors-observability: the error body behind the handlers in `main.py`.
- fastapi-security: ownership, 403 and 404.
- fastapi-testing: real rows from two tenants.
- fastapi-project-structure: where these files live.
