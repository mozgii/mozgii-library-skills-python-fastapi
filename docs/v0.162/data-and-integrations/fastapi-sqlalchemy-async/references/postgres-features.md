---
title: Upserts and JSONB with SQLAlchemy async
author: MOZGII, Inc.
lang: en
---

# Upserts and JSONB with SQLAlchemy async

This reference holds two PostgreSQL features async FastAPI apps use often: `INSERT ... ON CONFLICT` and JSONB columns.

## Contents

This reference on upserts and JSONB with SQLAlchemy async has these sections:

- Upserts and bulk writes on PostgreSQL
- JSONB columns

## Upserts and bulk writes on PostgreSQL

PostgreSQL's `INSERT ... ON CONFLICT` inserts or updates in one statement and is safe under concurrency, unlike
"select, then insert".

```python
from sqlalchemy.dialects.postgresql import insert

stmt = insert(Tag).values([{"name": n} for n in names])
stmt = stmt.on_conflict_do_nothing(index_elements=[Tag.name])
await session.execute(stmt)
await session.commit()
```

Use `on_conflict_do_update(index_elements=[...], set_={"title": stmt.excluded.title})` to update instead.

## JSONB columns

JSONB stores semi-structured data with indexing; SQLAlchemy does not notice in-place changes unless told.

```python
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm.attributes import flag_modified

settings: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

user.settings["theme"] = "dark"
flag_modified(user, "settings")         # without this the UPDATE is skipped
await session.commit()
```

Or declare the column as `MutableDict.as_mutable(JSONB)` (from `sqlalchemy.ext.mutable`) to track top-level key
changes automatically. Nested changes still need `flag_modified`.
