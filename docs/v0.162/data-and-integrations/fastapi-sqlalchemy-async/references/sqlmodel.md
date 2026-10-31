---
title: SQLModel with FastAPI
author: MOZGII, Inc.
lang: en
---

# SQLModel with FastAPI

This reference covers SQLModel 0.0.48, the library that combines SQLAlchemy tables and Pydantic models in one class,
and how to use it safely with FastAPI. SQLModel is still pre-1.0: pin it with an upper bound (`sqlmodel>=0.0.48,<0.1`).

## Contents

This reference on SQLModel with FastAPI has these sections:

- When to choose SQLModel
- Table models and data models
- Timestamps with UTCDateTime
- Sync session (the official template's way)
- Async session
- Relationships
- Migrations with SQLModel

## When to choose SQLModel

SQLModel removes the duplicate between an ORM model and its schema, which the FastAPI maintainers recommend for most
apps. Choose plain SQLAlchemy when you need advanced mapping (inheritance, composite types, hybrid properties),
heavy async use with relationships, or strict control over which fields reach the API.

## Table models and data models

Only classes with `table=True` are tables; the others are plain Pydantic models used for input and output. Keep the
split: a table model used as a response leaks every column.

```python
from sqlmodel import Field, SQLModel


class ItemBase(SQLModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class Item(ItemBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True, ondelete="CASCADE")


class ItemCreate(ItemBase):
    pass


class ItemPublic(ItemBase):
    id: int
    owner_id: int


class ItemUpdate(SQLModel):
    title: str = Field(default_factory=str, min_length=1, max_length=200)   # NOT NULL column: null is a 422
    description: str | None = Field(default=None, max_length=2000)          # nullable column: null clears it
```

Apply a PATCH with `item.sqlmodel_update(body.model_dump(exclude_unset=True))`, so the empty default of `title` is
never written.

Validation constraints (`min_length`) are checked on data models, not when you construct a `table=True` model
directly. Validate input through `ItemCreate` first, then build the table row with `Item.model_validate(data)`.

## Timestamps with UTCDateTime

From SQLModel 0.0.45 a plain `datetime` field picks the `UTCDateTime` column type, built on `DateTime(timezone=True)`.
It turns aware values into UTC on the way in and gives back aware UTC values on the way out, on SQLite and MySQL too. So
write no `sa_type=DateTime(timezone=True)`: an explicit SQLAlchemy type replaces `UTCDateTime` and its UTC handling.

```python
from datetime import UTC, datetime

from pydantic import AwareDatetime
from sqlalchemy import func
from sqlmodel import Field, SQLModel


class EventBase(SQLModel):
    happened_at: AwareDatetime                  # an API input without an offset is a 422


class Event(EventBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime | None = Field(default=None, sa_column_kwargs={"server_default": func.now()})
```

- A naive `datetime` fails when it reaches the database (`StatementError` around a `ValueError`), and plain `datetime`
  validation still lets one through. Use `AwareDatetime` for values that come from clients.
- `NaiveDatetime`, or an explicit `Field(sa_type=DateTime(timezone=False))`, keeps naive storage.
- Moving an older app to 0.0.45 changes no existing column. On PostgreSQL write a migration that names the timezone of
  the stored values; SQLite and MySQL rows that are not in UTC need a data migration.

## Sync session (the official template's way)

The full-stack template uses a sync engine with psycopg and `def` endpoints, which FastAPI runs in the thread pool.
This is simple and fast enough for most apps.

```python
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlmodel import Session, create_engine, select

engine = create_engine("postgresql+psycopg://app:app@localhost/app", pool_pre_ping=True)


def get_session() -> Generator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/")
def list_items(session: SessionDep, limit: int = 20) -> list[ItemPublic]:
    items = session.exec(select(Item).limit(limit)).all()
    return [ItemPublic.model_validate(i) for i in items]
```

Endpoints using the sync session must be `def`, never `async def`, or every query blocks the event loop.

## Async session

SQLModel ships an async session that adds `exec()` on top of SQLAlchemy's `AsyncSession`.

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

engine = create_async_engine("postgresql+asyncpg://app:app@localhost/app")
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@router.get("/{item_id}")
async def read_item(item_id: int, session: AsyncSessionDep) -> ItemPublic:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    return ItemPublic.model_validate(item)
```

All rules from the SQLAlchemy async skill apply: `expire_on_commit=False`, explicit eager loading, one session per
request.

## Relationships

SQLModel relationships are SQLAlchemy relationships; pass loader settings through `sa_relationship_kwargs`.

```python
from sqlmodel import Relationship


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    items: list["Item"] = Relationship(
        back_populates="owner",
        cascade_delete=True,
        sa_relationship_kwargs={"lazy": "raise"},
    )
```

Load them with `selectinload(User.items)` exactly as in plain SQLAlchemy.

## Migrations with SQLModel

Alembic works unchanged: point `target_metadata` at `SQLModel.metadata` and import every table module in `env.py`.
Autogenerated migrations contain `sqlmodel.sql.sqltypes.AutoString`; add `import sqlmodel` to the migration
template (`script.py.mako`) so the files import cleanly.
