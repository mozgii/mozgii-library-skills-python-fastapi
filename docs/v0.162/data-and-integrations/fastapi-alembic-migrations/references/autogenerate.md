---
title: Alembic autogenerate traps
author: MOZGII, Inc.
lang: en
---

# Alembic autogenerate traps

This reference lists what Alembic's autogenerate gets wrong or misses with SQLAlchemy 2.1 and PostgreSQL, and how to
fix each case in the revision file.

## Contents

This reference on Alembic autogenerate traps has these sections:

- What autogenerate detects
- Column renames
- Server defaults
- PostgreSQL enums
- Check constraints
- SQLModel AutoString
- Several metadata objects or schemas
- Excluding objects Alembic should not touch
- Revision file names with dates

## What autogenerate detects

Autogenerate reliably detects added and removed tables and columns, nullability changes, added and removed indexes,
unique constraints and foreign keys, and column type changes (`compare_type` defaults to `True`). Everything else
needs a hand-written operation.

## Column renames

A renamed attribute appears as `drop_column` plus `add_column`, which destroys the data. Replace both lines.

```python
def upgrade() -> None:
    op.alter_column("items", "name", new_column_name="title")


def downgrade() -> None:
    op.alter_column("items", "title", new_column_name="name")
```

## Server defaults

Changes to `server_default` are compared only when `compare_server_default=True` is passed to `context.configure`,
and the comparison can report false differences for expressions such as `now()`. Turn it on, review its output, and
write `op.alter_column("items", "status", server_default="draft")` yourself when needed.

## PostgreSQL enums

A native PostgreSQL enum is created with its table, but adding or removing a value is not detected. Adding a value:

```python
def upgrade() -> None:
    op.execute("ALTER TYPE item_status ADD VALUE IF NOT EXISTS 'archived'")
```

Removing a value needs a new type, a column conversion and a drop of the old type. Many teams store such fields as
`String(20)` with a `CHECK (status IN (...))` constraint instead; changing the allowed values is then one constraint
swap.

## Check constraints

Autogenerate does not compare the body of a `CHECK` constraint. Drop and create it explicitly, using the
naming-convention name so it can be found later.

```python
def upgrade() -> None:
    op.drop_constraint("ck_items_status", "items", type_="check")
    op.create_check_constraint("status", "items", "status IN ('draft', 'published', 'archived')")
```

With the naming convention `"ck": "ck_%(table_name)s_%(constraint_name)s"`, the second call creates
`ck_items_status`.

## SQLModel AutoString

Revisions generated from SQLModel models reference `sqlmodel.sql.sqltypes.AutoString`. Add `import sqlmodel` to
`migrations/script.py.mako` so every new revision imports it, or replace the type with `sa.String()`.

## Several metadata objects or schemas

When models live in two metadata objects, pass a list: `target_metadata = [core.metadata, billing.metadata]`. For
PostgreSQL schemas other than `public`, set `include_schemas=True` and give each table `__table_args__ = {"schema": "billing"}`.

## Excluding objects Alembic should not touch

Tables created by extensions or other tools (PostGIS `spatial_ref_sys`, a queue library's tables) show up as
"removed". Filter them out in `env.py`.

```python
def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    return not (type_ == "table" and reflected and compare_to is None and name in {"spatial_ref_sys"})


context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
```

## Revision file names with dates

Sorting revision files by creation date makes the folder readable. Set the file template in the Alembic
configuration:

```toml
[tool.alembic]
script_location = "%(here)s/migrations"
file_template = "%%(year)d_%%(month).2d_%%(day).2d_%%(hour).2d%%(minute).2d-%%(rev)s_%%(slug)s"
```

The revision id inside the file stays the source of truth for order; the file name is only for people.
