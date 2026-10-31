---
title: SQLAlchemy relationship loading in async FastAPI
author: MOZGII, Inc.
lang: en
---

# SQLAlchemy relationship loading in async FastAPI

This reference explains how to load related rows with SQLAlchemy 2.1 in async code without N+1 queries or
`MissingGreenlet` errors, and how deletes cascade.

## Contents

This reference on SQLAlchemy relationship loading in async FastAPI has these sections:

- Why lazy loading fails in async code
- selectinload for collections
- joinedload for single objects
- Nested and multiple loaders
- raiseload to forbid unplanned loads
- Counting related rows without loading them
- Cascading deletes with passive_deletes
- Loading after commit

## Why lazy loading fails in async code

Lazy loading runs a query the moment an attribute is read. In async code that read happens in synchronous code
(often inside Pydantic serialization), where SQLAlchemy cannot await, so it raises `MissingGreenlet`. Declaring
`lazy="raise"` on every relationship makes the mistake fail early with a clear message:
`InvalidRequestError: 'Item.owner' is not available due to lazy='raise'`.

## selectinload for collections

`selectinload` fills a collection (one-to-many, many-to-many) with one extra `SELECT ... WHERE parent_id IN (...)`
for all parents at once: two queries in total, however many parents.

```python
from sqlalchemy import select
from sqlalchemy.orm import selectinload

stmt = select(User).where(User.is_active).options(selectinload(User.items))
users = (await session.scalars(stmt)).all()
for user in users:
    print(len(user.items))        # no query here
```

## joinedload for single objects

`joinedload` adds a `LEFT OUTER JOIN` and fills a single related object (many-to-one, one-to-one) in the same query.

```python
from sqlalchemy.orm import joinedload

stmt = select(Item).options(joinedload(Item.owner)).order_by(Item.id).limit(50)
items = (await session.scalars(stmt)).all()
```

Avoid `joinedload` on collections: the join multiplies rows, and with `LIMIT` it limits joined rows, not parents. If
you must, call `.unique()` on the result.

## Nested and multiple loaders

Loader options chain for nested relations and repeat for siblings.

```python
stmt = (
    select(Order)
    .options(
        selectinload(Order.lines).joinedload(OrderLine.product),
        joinedload(Order.customer),
    )
    .where(Order.id == order_id)
)
order = await session.scalar(stmt)
```

## raiseload to forbid unplanned loads

`raiseload("*")` in a query forbids every relationship not loaded explicitly, even on models without `lazy="raise"`.

```python
from sqlalchemy.orm import raiseload

stmt = select(Order).options(selectinload(Order.lines), raiseload("*"))
```

## Counting related rows without loading them

A correlated subquery or a grouped join returns counts without pulling every child row into memory.

```python
from sqlalchemy import func, select

item_count = (
    select(func.count(Item.id)).where(Item.owner_id == User.id).correlate(User).scalar_subquery()
)
rows = (await session.execute(select(User.id, User.email, item_count.label("items")))).all()
```

## Cascading deletes with passive_deletes

A foreign key with `ondelete="CASCADE"` lets PostgreSQL delete children, but if the children are loaded in the
session, the ORM first tries to set their foreign key to `NULL` and fails with a `NotNullViolationError`. Tell the
relationship to leave deletes to the database.

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    items: Mapped[list["Item"]] = relationship(
        back_populates="owner",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,          # trust ON DELETE CASCADE; do not load or null the children
    )
```

## Loading after commit

With `expire_on_commit=False`, objects keep their loaded values after `commit()`. Values the database set (server
defaults, triggers) are not there until you refresh.

```python
await session.commit()
await session.refresh(item)                         # reloads columns
await session.refresh(item, attribute_names=["owner"])   # loads one relationship explicitly
```
