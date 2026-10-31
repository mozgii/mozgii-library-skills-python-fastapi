---
name: fastapi-pydantic-models
description: Designs Pydantic v2 request and response models for FastAPI - separate Create, Update and Read schemas, from_attributes for ORM objects, Field constraints, field and model validators, PATCH with exclude_unset, camelCase aliases, discriminated unions, computed fields and a shared base model. Use when writing schemas.py, validating input, hiding fields such as passwords from responses, implementing partial updates, or converting ORM objects to API responses.
metadata:
  tags:
  - fastapi
  - pydantic
  - schemas
  - validation
  - serialization
  - patch
  - aliases
---

# FastAPI Pydantic models and schemas

This skill writes the schemas that define a FastAPI API's contract with Pydantic 2.14: what a client may send, what it
gets back, and the rules both must follow. FastAPI 0.128+ supports Pydantic v2 only.

## Step 0: installed versions for schemas

Read the Pydantic version before writing schemas. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastapi|pydantic|pydantic-core|pydantic-settings|email-validator) '
```

Move to Pydantic 2.14.2 or later first: it patches GHSA-qpmf-fgh3-ff25 and GHSA-3r8g-cqwr-jr29. Since that release
JSON validation treats a key that only matches an unused alias as extra data, so an `extra="forbid"` model with
`alias_generator=to_camel` refuses `{"page_size": 50}` where it used to drop the key. `validate_by_name` and
`serialize_by_alias` need 2.11 or later.

## Choose where a rule belongs

Pydantic offers several places for the same rule; the wrong place shows up as a wrong schema or a hidden query.

| Need | Use | Why the others lose |
|---|---|---|
| a limit on one value | `Field()` or an `Annotated` alias | a validator would hide the limit from OpenAPI |
| a field PATCH may omit but never clear | a non-nullable type with a `default_factory` | `str \| None` documents and accepts `null`, which then fails at the NOT NULL column |
| a field PATCH may clear | `str \| None = None` | a validator that rejects `None` removes the feature |
| a rule on one field that `Field()` cannot express | `@field_validator` | `@model_validator` runs later, with more code |
| a rule across fields | `@model_validator(mode="after")` | a field validator cannot see the other fields reliably |
| a rule that needs the database | a service or a dependency | a validator has no session and no async |
| one of several shapes | a discriminated union | a plain union gives vague errors and tries every model |
| a value derived from other fields | `@computed_field` | a stored copy goes stale |

## One schema per direction

A resource gets separate schemas for create, update and read, so input fields (a password) never appear in output
and output fields (an id, timestamps) are never accepted as input.

```python
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ItemBase(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):                # every field may be omitted: PATCH
    title: str = Field(default_factory=str, min_length=1, max_length=200)   # NOT NULL column: null is a 422
    description: str | None = Field(default=None, max_length=2000)           # nullable column: null clears it


class ItemRead(ItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    created_at: datetime


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class UserRead(BaseModel):                  # no password, no hash
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
```

- `from_attributes=True` lets `ItemRead.model_validate(orm_item)` read attributes from an SQLAlchemy object.
- Never return the ORM model or the `Create` schema from an endpoint; return the `Read` schema.
- Required fields have no default. Do not write `= ...` or `Field(...)`.

## Field constraints

`Field()` and `Annotated` types declare limits that Pydantic checks before your code runs, and that appear in
OpenAPI.

```python
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, Field, HttpUrl, StringConstraints

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]+$", min_length=3, max_length=64)]
Price = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=30)]


class ProductCreate(BaseModel):
    slug: Slug
    price: Price
    tags: list[Tag] = Field(default=[], max_length=10)
    homepage: HttpUrl | None = None
    category_id: UUID
```

- Put a `max_length` on every string and list that comes from a client; unbounded input is a memory and storage
  problem.
- Use `Decimal` for money, never `float`.
- Reusable `Annotated` aliases (`Slug`, `Price`) keep the same rule in every model.

## Validators

A field validator checks or normalizes one field; a model validator checks rules across fields. Raise `ValueError`
with a message the client can act on; FastAPI turns it into a 422.

```python
from datetime import date
from typing import Self

from pydantic import field_validator, model_validator


class BookingCreate(BaseModel):
    starts_on: date
    ends_on: date
    guests: int = Field(ge=1, le=12)

    @field_validator("starts_on")
    @classmethod
    def not_in_past(cls, value: date) -> date:
        if value < date.today():
            raise ValueError("must not be in the past")
        return value

    @model_validator(mode="after")
    def ends_after_start(self) -> Self:
        if self.ends_on <= self.starts_on:
            raise ValueError("ends_on must be after starts_on")
        return self
```

Validators must not query the database or call other services: they run inside Pydantic, without a session and
without async. Uniqueness and existence checks belong in the service or a dependency.

## PATCH with exclude_unset

A partial update applies only the fields the client sent. `model_dump(exclude_unset=True)` returns exactly those,
which tells "not sent" apart from "sent as null".

```python
@router.patch("/{item_id}")
async def update_item(item_id: int, body: ItemUpdate, session: SessionDep, user: CurrentUser) -> ItemRead:
    item = await items_service.get_owned(session, item_id, user.id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    await session.commit()
    return ItemRead.model_validate(item)
```

- A client sending `{"description": null}` clears the description; omitting it leaves it alone.
- A field that must never be cleared, such as `title` on a NOT NULL column, keeps a non-nullable type with a
  `default_factory`, so `{"title": null}` is a 422 before your code runs, and `{}` is still valid. The factory's empty
  string is never written: `exclude_unset=True` leaves the field out. Writing `str | None` here would let `null` through
  to the database, where it fails as a 500.
- A validator that rejects `None` does the same job, but the schema then tells clients that `null` is allowed. Since
  FastAPI 0.151, `null` for a non-nullable field is a 422 even when it has a default.

## camelCase aliases for JavaScript clients

An alias generator gives the JSON API camelCase keys while Python code keeps snake_case. Set it once on a base model.

```python
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,       # accept owner_id as well as ownerId
        validate_by_alias=True,
        serialize_by_alias=True,     # model_dump() and responses use ownerId
        from_attributes=True,
    )


class ItemRead(ApiModel):
    id: int
    owner_id: int                    # JSON: "ownerId"
```

FastAPI serializes responses by alias already (`response_model_by_alias=True`); `serialize_by_alias=True` makes
`model_dump()` in your own code agree.

## Discriminated unions

A discriminated union picks the right model from a literal tag field, gives precise errors and a clean OpenAPI
`oneOf`.

```python
from typing import Annotated, Literal

from pydantic import Field


class CardPayment(BaseModel):
    kind: Literal["card"]
    card_token: str


class BankPayment(BaseModel):
    kind: Literal["bank"]
    iban: str = Field(pattern=r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")


Payment = Annotated[CardPayment | BankPayment, Field(discriminator="kind")]


@router.post("/payments")
async def pay(payment: Payment) -> PaymentRead: ...
```

## Computed fields and serialization

`@computed_field` adds a derived value to the output and to the OpenAPI schema; `@field_serializer` controls how one
field is written.

```python
from pydantic import computed_field, field_serializer


class OrderRead(ApiModel):
    quantity: int
    unit_price: Decimal
    placed_at: datetime

    @computed_field
    @property
    def total(self) -> Decimal:
        return self.quantity * self.unit_price

    @field_serializer("unit_price", "total")
    def money(self, value: Decimal) -> str:
        return f"{value:.2f}"
```

More recipes (strict mode, a shared base model, `TypeAdapter`, generic page models, enums, datetimes in UTC):
[references/recipes.md](references/recipes.md).

## Verify the schemas

Check the schema FastAPI really publishes, then the edge cases. The first command must print `ok`.

```bash
uv run python - <<'PY'
import json
from app.main import app
published = json.dumps(app.openapi())
for secret in ("hashed_password", "password_hash"):
    assert secret not in published, secret
print("ok")
PY
uv run pyright app/
uv run pytest -k "schema or patch"
```

```python
async def test_patch_refuses_null_for_a_required_column(client: httpx.AsyncClient, item_url: str) -> None:
    assert (await client.patch(item_url, json={"title": None})).status_code == 422


async def test_patch_leaves_omitted_fields_alone(client: httpx.AsyncClient, item_url: str) -> None:
    before = (await client.get(item_url)).json()
    after = (await client.patch(item_url, json={"description": None})).json()
    assert after["title"] == before["title"] and after["description"] is None


def test_every_field_keeps_its_type() -> None:
    properties = app.openapi()["components"]["schemas"]["ItemRead"]["properties"]
    assert all("type" in schema or "anyOf" in schema for schema in properties.values())
```

## Pydantic schema anti-patterns

These look tidy and quietly widen the API.

| Do not | Do instead |
|---|---|
| one model for input and output | separate Create, Update and Read schemas |
| `title: str \| None` on a NOT NULL column in a PATCH model | a non-nullable type with a `default_factory` |
| `@field_serializer("*")` returning `object` | a typed `Annotated` serializer on the fields that need it |
| `float` for money | `Decimal` with `max_digits` and `decimal_places` |
| a client string or list with no `max_length` | a limit on every one |
| a database query in a validator | a check in the service or a dependency |
| `Field(...)` or `= ...` on a required field | no default at all |

## Pydantic schema checklist

Go through this Pydantic schema checklist before merging a change.

```text
[ ] separate Create, Update, Read schemas; Read has from_attributes=True
[ ] no password, hash, token or internal flag in any Read schema
[ ] every client string and list has max_length; money is Decimal
[ ] required fields have no default; no Ellipsis, no RootModel
[ ] validators raise ValueError with a client-facing message and never touch the database
[ ] PATCH uses model_dump(exclude_unset=True)
[ ] aliases configured once on a base model when the API is camelCase
[ ] polymorphic input uses a discriminated union
```

## Common Pydantic schema problems

These are the Pydantic schema problems people hit most, each with its symptom, cause and fix.

1. **The password shows up in the response.** The endpoint returned the ORM object or the `Create` model. Declare
   `-> UserRead`.
2. **`model_dump()` loses the alias.** Without `serialize_by_alias=True` your own dumps are snake_case while
   responses are camelCase. Set it on the base model.
3. **`exclude_unset` returns everything.** The model was built with all fields set (for example from
   `ItemUpdate(**data)` with defaults filled in). Validate the raw request body, not a dict you completed.
4. **A validator never runs when the client omits the field, and a bad default gets through.** Pydantic and, since
   0.148, FastAPI skip validation of defaults; use `Field(validate_default=True)` when a default must pass the
   validator.
5. **Handlers read `body.root`, and generated clients get an extra named type for a plain list.** The body is a
   `RootModel`. The official FastAPI guidance is to declare `list[Item]` with `Annotated` constraints instead; FastAPI
   builds a `TypeAdapter` for it.

## See also

- fastapi-routing: how schemas become request and response bodies.
- fastapi-sqlalchemy-async: the ORM objects behind `from_attributes`.
- fastapi-openapi: how these models appear in `openapi.json`.
- fastapi-version-upgrades: moving from Pydantic v1.
