---
name: fastapi-routing
description: Writes FastAPI path operations - path, query, header and cookie parameters with Annotated, parameter models for filters, request bodies, forms and file uploads, return types and response_model, status codes, custom response classes and strict JSON Content-Type checking. Use when adding or changing an endpoint, declaring request parameters, returning data with a response schema, uploading files, or when FastAPI returns an unexpected 422.
metadata:
  tags:
  - fastapi
  - routing
  - path-operations
  - query-parameters
  - request-body
  - forms
  - file-upload
  - response-model
---

# FastAPI routing and request parameters

This skill writes endpoints the way FastAPI 0.162 expects: every input declared with `Annotated` and validated by
Pydantic, every output declared by the return type, and one function per HTTP method.

## Step 0: installed versions for routing

Routing behaviour moved in many 0.1xx releases, so read the version first. A missing line means it is not installed.

```bash
uv pip list | grep -iE '^(fastapi|starlette|pydantic|python-multipart) '
```

Five rules below depend on it: strict `Content-Type` (0.132), default values no longer validated (0.148), `null`
different from omitted (0.151), unknown keywords refused (0.155), and `extra="forbid"` covering sibling parameters
(0.160). On an older FastAPI the old behaviour stands.

## Choose where each piece of input goes

A value's place in the request decides its visibility, its limits and how clients send it.

| The value is | Put it in | Why the others lose |
|---|---|---|
| what identifies the resource | the path | a query parameter makes optional what the URL cannot work without |
| an optional filter, a sort or a page | the query, in a `Query()` parameter model past two or three values | a body on `GET` is dropped by some clients and proxies |
| structured data to create or change | a JSON body model | query strings have no nesting, have length limits and land in access logs |
| browser form fields or a file | `Form()` and `File()` | JSON cannot carry a multipart upload |
| request metadata (a trace id, an idempotency key) | a header | it is not part of the resource, so it stays out of the URL and the body schema |
| a secret | a header or a body | URLs end up in proxy logs and browser history |

## Path operations, one function per method

A path operation is a function decorated with `@router.get`, `.post`, `.put`, `.patch` or `.delete`. Give each method
its own function and its own status code.

```python
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session                      # your yield dependency: one AsyncSession per request
from app.items.models import Item                   # SQLAlchemy model
from app.items.schemas import ItemCreate, ItemRead  # Pydantic models; ItemRead has from_attributes=True

SessionDep = Annotated[AsyncSession, Depends(get_session)]
router = APIRouter(prefix="/items", tags=["items"])


@router.get("/{item_id}")
async def get_item(item_id: Annotated[int, Path(ge=1)], session: SessionDep) -> ItemRead:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return ItemRead.model_validate(item)


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_item(body: ItemCreate, session: SessionDep) -> ItemRead:
    item = Item(**body.model_dump())
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return ItemRead.model_validate(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(item_id: Annotated[int, Path(ge=1)], session: SessionDep) -> None:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    await session.delete(item)
    await session.commit()
```

- Route order matters for overlapping paths: declare `/items/me` before `/items/{item_id}`, or `me` is parsed as an
  id and fails with 422.
- Use nouns in plural for collections (`/items`, `/items/{item_id}/tags`) and HTTP methods for verbs. A real action
  that is not CRUD gets a sub-path: `POST /items/{item_id}/archive`.
- A `-> None` endpoint with status 204 sends no body.

## Path, query, header and cookie parameters

FastAPI decides where a parameter comes from by its declaration: names in the path template are path parameters,
Pydantic models are the body, everything else is a query parameter unless `Header()` or `Cookie()` says otherwise.

```python
from typing import Annotated, Literal

from fastapi import Cookie, Header, Query


@router.get("/")
async def list_items(
    q: Annotated[str | None, Query(min_length=2, max_length=50)] = None,
    tags: Annotated[list[str], Query()] = [],
    sort: Literal["created_at", "title"] = "created_at",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    x_request_id: Annotated[str | None, Header()] = None,     # reads X-Request-Id
    session_id: Annotated[str | None, Cookie()] = None,
) -> list[ItemRead]: ...
```

- A list query parameter needs an explicit `Query()`: `?tags=a&tags=b` gives `["a", "b"]`. A mutable default `[]`
  is safe here because FastAPI copies it per request.
- `Header()` converts underscores to hyphens; pass `convert_underscores=False` for a header that really has
  underscores.
- Constraints (`ge`, `le`, `min_length`, `pattern`) produce a 422 with the field location before the handler runs.
- `Query(validate_default=True)` validates the default too. Since 0.148 defaults of omitted parameters are no longer
  validated unless you ask for it.
- Since 0.155 unknown keyword arguments to `Query()`, `Path()`, `Body()` and friends raise `TypeError`; extra
  OpenAPI keys go in `json_schema_extra={...}`.

## Parameter models for filters and pagination

A Pydantic model declared with `Query()` (or `Header()`, `Cookie()`) groups related parameters, so a filter is one
reusable, documented object. Verified on FastAPI 0.162.

```python
from typing import Annotated, Literal

from fastapi import Query
from pydantic import BaseModel, Field


class ItemFilter(BaseModel):
    model_config = {"extra": "forbid"}         # unknown query keys → 422 extra_forbidden

    q: str | None = Field(default=None, min_length=2)
    tags: list[str] = []
    order_by: Literal["id", "created_at"] = "id"
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


@router.get("/")
async def list_items(filters: Annotated[ItemFilter, Query()]) -> list[ItemRead]: ...
```

- With `extra="forbid"`, a typo such as `?limt=5` fails loudly instead of being ignored.
- Since 0.160 the model's `extra` setting applies to every field in the same location, including fields declared by
  other parameters or dependencies; with `forbid`, a sibling `Query()` parameter on the same endpoint is rejected.
  Put all query fields of a forbidding endpoint in the model.
- Several models can share a location (0.157+), and a model can be optional: `Annotated[ItemFilter | None, Query()] = None`
  (0.159+).

## Request bodies

A parameter typed as a Pydantic model (or a list or union of models) is read from the JSON body.

```python
@router.put("/{item_id}")
async def replace_item(item_id: int, body: ItemCreate) -> ItemRead: ...


@router.post("/bulk")
async def create_many(items: Annotated[list[ItemCreate], Field(min_length=1, max_length=500)]) -> list[ItemRead]: ...
```

- Two body models in one endpoint make FastAPI expect `{"item": {...}, "user": {...}}`; `Body(embed=True)` does the
  same for one model.
- Since 0.151 an explicit JSON `null` is no longer the same as an omitted value: a non-nullable field with a default
  rejects `null`. Clients that send `null` to mean "use the default" must omit the key instead.
- For PATCH, use a model where every field is optional and apply `model_dump(exclude_unset=True)`; see
  fastapi-pydantic-models.
- A list body needs no `RootModel`: declare `list[Item]` with `Annotated` constraints as above.

## Strict JSON Content-Type checking

Since FastAPI 0.132 a JSON body is parsed only when the request has a JSON `Content-Type` such as
`application/json`; a request without it gets a 422. This blocks a CSRF trick where a browser sends a `Blob` body
without a preflight to an unauthenticated local API.

```python
app = FastAPI(strict_content_type=False)   # only for old clients that send no Content-Type
```

Keep the default. When a client reports 422 with a body that looks right, check its `Content-Type` header first.

## Forms and file uploads

Form fields use `Form()` and need `python-multipart` (included in `fastapi[standard]`); files use `UploadFile`, which
spools large uploads to disk instead of memory.

```python
from typing import Annotated

from fastapi import File, Form, UploadFile


@router.post("/{item_id}/image")
async def upload_image(
    item_id: int,
    image: Annotated[UploadFile, File(description="PNG or JPEG, at most 5 MB")],
    caption: Annotated[str, Form(max_length=200)] = "",
) -> ImageRead:
    if image.content_type not in {"image/png", "image/jpeg"}:
        raise HTTPException(415, "Only PNG or JPEG")
    data = await image.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "Image larger than 5 MB")
    ...
```

Browser form behaviour changed in 0.147–0.162 (empty strings, unchecked checkboxes, unselected file inputs). The
rules, form models and upload limits are in [references/forms-and-files.md](references/forms-and-files.md).

## Return types and response_model

The return annotation is the response schema: FastAPI validates the returned value against it, drops fields the
schema does not declare, documents it in OpenAPI and serializes it with Pydantic's Rust core (2x or more faster
than `jsonable_encoder` since 0.130).

```python
@router.get("/me")
async def read_me(user: CurrentUser) -> UserRead:    # User ORM object → UserRead; hashed_password never leaves
    return UserRead.model_validate(user)
```

Use `response_model=` when the function returns something other than the public schema, for example an ORM object
or a dict, and you do not want to convert it yourself:

```python
@router.get("/{item_id}", response_model=ItemRead)
async def read_item(item_id: int, session: SessionDep) -> Item:   # ORM model; ItemRead has from_attributes=True
    item = await session.get(Item, item_id)          # SessionDep, Item, ItemRead: as in the first example
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return item
```

- `response_model=None` turns validation off, for endpoints that return a `Response` directly.
- `response_model_exclude_none=True` drops `None` fields from the output.
- Response classes, `ORJSONResponse` deprecation, redirects, files and headers: see
  [references/responses.md](references/responses.md).

## Status codes and error responses

Each endpoint states its success status in the decorator and documents its error statuses, so clients and generated
SDKs know what to handle.

```python
from pydantic import BaseModel


class ErrorBody(BaseModel):
    detail: str


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorBody, "description": "Title already used"}},
)
async def create_item(body: ItemCreate, session: SessionDep) -> ItemRead: ...   # SessionDep etc.: the first example
```

Use `fastapi.status` constants rather than bare numbers. 200 for reads and updates that return a body, 201 for
creates, 204 for deletes without a body, 202 for accepted background work, 409 for conflicts, 422 is FastAPI's
validation error.

## Verify the routes

The first command lists every operation with no declared response body, which means a return type or `response_model`
is missing. It must print nothing, except a route that answers 204 or redirects.

```bash
uv run python - <<'PY'
from app.main import app
for path, operations in app.openapi()["paths"].items():
    for method, operation in operations.items():
        for status, response in operation["responses"].items():
            schema = response.get("content", {}).get("application/json", {}).get("schema")
            if status.startswith("2") and not schema:
                print(method.upper(), path, status)
PY
uv run ruff check --select FAST app/
uv run pytest -k "routes or contract"
```

These tests pin the behaviour clients rely on; change the paths to yours.

```python
async def test_static_path_wins_over_the_id(client: httpx.AsyncClient) -> None:
    assert (await client.get("/items/me")).status_code != 422


async def test_unknown_query_key_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.get("/items/", params={"limt": 5})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


async def test_json_needs_a_content_type(client: httpx.AsyncClient) -> None:
    assert (await client.post("/items/", content=b'{"title": "x"}')).status_code == 422


async def test_oversized_upload_is_refused(client: httpx.AsyncClient) -> None:
    files = {"image": ("big.png", b"x" * (5 * 1024 * 1024 + 1), "image/png")}
    assert (await client.post("/items/1/image", files=files)).status_code == 413
```

## Routing anti-patterns

Each of these compiles, passes a happy-path test and still hurts a client later.

| Do not | Do instead |
|---|---|
| branch on `request.method` in one function | one function per HTTP method |
| `item_id: int = Path(...)` | `item_id: Annotated[int, Path(ge=1)]` |
| return a dict or an ORM object with no return type | a return type or `response_model`, so unknown fields are dropped |
| `/items/{item_id}` declared before `/items/me` | static paths first |
| an upload with no type or size check | check `content_type` and read at most the limit plus one byte |
| passwords or tokens in query parameters | a header or a body |
| `RootModel` for a list body | `list[Item]` with `Annotated` constraints |

## Routing checklist

Go through this routing and request parameter checklist before merging a change.

```text
[ ] one function per HTTP method; static paths declared before parameterized ones
[ ] every parameter uses Annotated with Path/Query/Header/Cookie/Body/Form/File
[ ] filters and pagination grouped in a Query() parameter model with extra="forbid"
[ ] return type (or response_model) set on every endpoint; ORM objects never returned raw without a schema
[ ] success status_code in the decorator; error statuses in responses=
[ ] uploads checked for type and size; forms follow the browser rules in references/forms-and-files.md
[ ] strict_content_type left on unless a known client needs it off
```

## Common routing and request parameter problems

These are the routing and request parameter problems people hit most, each with its symptom, cause and fix.

1. **422 for a body that looks fine.** Missing `Content-Type: application/json` (0.132+), a `null` for a
   non-nullable field (0.151+), or two body parameters making FastAPI expect an embedded object.
2. **`/items/me` returns 422.** It matched `/items/{item_id}` first. Reorder the routes.
3. **Extra fields appear in the response.** The endpoint has no return type and returns a dict or ORM object. Add
   the schema.
4. **`Query(custom="x")` raises `TypeError`.** Removed in 0.155; use `json_schema_extra`.
5. **A trailing slash redirect.** `/items` and `/items/` are different paths; FastAPI answers 307 to the other one.
   Behind a proxy without forwarded headers the redirect points to `http://`. Pick one form and configure
   `--forwarded-allow-ips` (see fastapi-deployment).

## See also

- fastapi-pydantic-models: body and response schemas.
- fastapi-dependencies: shared parameters and loading a resource by id.
- fastapi-security: protecting routes.
- fastapi-openapi: documenting errors and operation ids.
- fastapi-websockets-streaming: streaming responses.
