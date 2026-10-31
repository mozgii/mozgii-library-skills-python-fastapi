---
title: Pydantic recipes for FastAPI
author: MOZGII, Inc.
lang: en
---

# Pydantic recipes for FastAPI

This reference collects Pydantic 2.14 patterns that FastAPI APIs need often: a shared base model, strict input,
generic page responses, enums, UTC datetimes and validation outside a request.

## Contents

This reference on Pydantic recipes for FastAPI has these sections:

- A shared base model
- Strict input for money and ids
- Generic page response
- Enums in the API
- Datetimes always in UTC
- Validating data outside a request with TypeAdapter
- Turning off extra fields on input

## A shared base model

A project base model holds the settings every schema needs in one place, such as ORM reading and whitespace
stripping. How datetimes are written belongs on a typed field, not on the base model: a `"*"` serializer that returns
`object` replaces the type of every field with an empty schema in OpenAPI, and generated clients lose all of them.

```python
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer


def in_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, PlainSerializer(in_utc, return_type=datetime, when_used="json")]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)


class EventRead(ApiModel):
    id: int
    title: str
    starts_at: UtcDatetime                 # 2026-10-28T12:00:00+03:00 is written as 2026-10-28T09:00:00Z
```

Prove the schema kept its types with `app.openapi()`: each field must still have its own `type`.

```python
schema = app.openapi()["components"]["schemas"]["EventRead"]["properties"]
assert schema["id"]["type"] == "integer"
assert schema["starts_at"] == {"type": "string", "format": "date-time", "title": "Starts At"}
```

## Strict input for money and ids

Strict mode stops Pydantic from converting `"10"` to `10` or `1.0` to `True`. Turn it on per field where a silent
conversion would be a bug.

```python
from pydantic import Field, StrictBool, StrictInt


class Transfer(BaseModel):
    amount_cents: StrictInt = Field(gt=0)
    confirm: StrictBool
```

FastAPI 0.154+ also accepts Pydantic validation options on request parameters, such as `Query(strict=True)`.

## Generic page response

A generic model gives every list endpoint the same page shape and a precise OpenAPI schema per item type.

```python
from pydantic import BaseModel


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


@router.get("/")
async def list_items(session: SessionDep, filters: Annotated[ItemFilter, Query()]) -> Page[ItemRead]:
    items, total = await items_service.page(session, filters)
    return Page[ItemRead](items=items, total=total, limit=filters.limit, offset=filters.offset)
```

The PEP 695 syntax `class Page[T](BaseModel)` needs Python 3.12+; on 3.10–3.11 use `Generic[T]` with a `TypeVar`.

## Enums in the API

A `StrEnum` documents allowed values in OpenAPI and gives generated clients a union of string literals.

```python
from enum import StrEnum


class ItemStatus(StrEnum):
    draft = "draft"
    published = "published"
    archived = "archived"


class ItemRead(ApiModel):
    status: ItemStatus
```

Adding a value to an enum that clients read is a breaking change for strictly typed clients (a Dart or Swift
`switch` without a default case). Version the field or document that clients must accept unknown values.

## Datetimes always in UTC

Store timezone-aware datetimes, accept only aware input and answer in UTC, so clients never guess the zone.

```python
from pydantic import AwareDatetime


class EventCreate(ApiModel):
    starts_at: AwareDatetime      # "2026-10-28T09:00:00" without an offset → 422
```

Pair this with `DateTime(timezone=True)` columns (fastapi-sqlalchemy-async).

## Validating data outside a request with TypeAdapter

`TypeAdapter` validates and serializes any type without a model class, for example in a background job or when
reading JSON from a queue.

```python
from pydantic import TypeAdapter

items_adapter = TypeAdapter(list[ItemCreate])

items = items_adapter.validate_json(raw_bytes)          # raises ValidationError on bad input
payload = items_adapter.dump_json(items)
```

Build adapters once at module level; building one per call is slow.

## Turning off extra fields on input

By default Pydantic ignores unknown keys. For input models where a typo should fail, forbid extras.

```python
class ItemCreate(ApiModel):
    model_config = ConfigDict(extra="forbid")
```

Do not forbid extras on models you read from other services' responses; a new field there would break you.
