---
name: fastapi-openapi
description: Makes a FastAPI app's OpenAPI schema a reliable contract - app metadata and tags, clean operation ids with generate_unique_id_function, documented error responses and examples, deprecations, hiding internal routes, Swagger UI settings, exporting openapi.json without a server, catching breaking changes in CI, generating TypeScript and other SDKs, and API versioning. Use when documenting an API, generating a frontend or mobile client, reviewing a change for breaking clients, or turning the docs off in production.
metadata:
  tags:
  - fastapi
  - openapi
  - swagger
  - documentation
  - sdk
  - client-generation
  - versioning
---

# FastAPI OpenAPI, docs and client generation

FastAPI builds an OpenAPI 3.1.0 schema from the code; this skill makes that schema precise enough to generate
clients from and stable enough that a change which breaks clients fails CI.

## Step 0: installed versions for the schema

Read the versions first, because a FastAPI upgrade can change generated schemas. A missing line means it is not
installed.

```bash
uv pip list | grep -iE '^(fastapi|pydantic) '
uv run python -c "from app.main import app; print(app.openapi()['openapi'])"    # the OpenAPI version, 3.1.0
```

Export `openapi.json` before and after any upgrade of either package, and read the diff.

## Choose a versioning strategy

Versioning cost grows quickly, so pick the simplest strategy that keeps released clients working.

| Strategy | Wins when | Loses because |
|---|---|---|
| Additive changes only, within one version: add fields and endpoints, never remove or rename | almost always; most APIs never need a second version | a change that must break clients has nowhere to go |
| URL prefix for breaking changes: `APIRouter(prefix="/v2")` beside `/v1`, sharing services | a real breaking change on some routes; remove `/v1` when its traffic is gone | two routers to keep and test while both live |
| Separate apps mounted with `app.mount("/v1", v1_app)`, each with its own `/docs` | versions differ deeply | middleware, auth and schemas are maintained twice |

## App metadata and tags

Metadata and tag descriptions appear at the top of `/docs` and in generated clients. Set them once on the app.

```python
tags_metadata = [
    {"name": "items", "description": "Create, read, update and archive items."},
    {"name": "auth", "description": "Log in and refresh tokens."},
]

app = FastAPI(
    title="Items API",
    version="1.4.0",                     # the API's version, not FastAPI's
    summary="Inventory items for the Example app.",
    contact={"name": "API team", "email": "api@example.com"},
    license_info={"name": "Proprietary"},
    openapi_tags=tags_metadata,
    servers=[{"url": "https://api.example.com", "description": "production"}],
)
```

Every router has `tags=[...]`, and each tag used appears in `openapi_tags`, so `/docs` groups endpoints the way
clients think about them.

## Clean operation ids for generated clients

By default an operation id is the function name plus path and method (`list_items_items__get`), which becomes an ugly
SDK method name. Generate ids from the tag and the function name instead.

```python
from fastapi.routing import APIRoute


def operation_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}" if route.tags else route.name


app = FastAPI(generate_unique_id_function=operation_id)
```

A client generator then produces `ItemsService.listItems()`. Function names must be unique within a tag, and every
route needs a tag or a unique name. Changing ids later renames every SDK method: decide before the first client ships.

## Document errors and examples

Clients can only handle errors the schema lists. Declare non-2xx responses with a model, and give request bodies an
example so `/docs` is usable without reading code.

```python
@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"model": Problem, "description": "An item with this title already exists"},
        422: {"model": Problem, "description": "Invalid input"},
    },
    summary="Create an item",
)
async def create_item(
    body: Annotated[
        ItemCreate,
        Body(openapi_examples={"minimal": {"summary": "Title only", "value": {"title": "Desk lamp"}}}),
    ],
    session: SessionDep,
) -> ItemRead:
    """Create an item owned by the current user. Titles are unique per user."""
```

- The docstring becomes the operation description; `summary=` the one-line title.
- Field-level docs come from `Field(description=..., examples=[...])` in the schema.
- Router-level `responses={401: {...}}` applies to every route in it.

## Deprecate, hide and version routes

Deprecation marks a route in the schema so generated clients warn; `include_in_schema=False` hides internal routes.

```python
@router.get("/search", deprecated=True)                 # still works, flagged in docs and SDKs
async def old_search(q: str) -> list[ItemRead]: ...


@app.get("/health", include_in_schema=False)            # probes, webhooks receivers, admin tools
async def health() -> dict[str, str]: ...
```

Versioning strategies, from simplest:

1. **Additive changes only** within one version: add fields and endpoints, never remove or rename. Most APIs never
   need a second version.
2. **URL prefix** for breaking changes: `APIRouter(prefix="/v2")` beside `/v1`, sharing services; remove `/v1` when
   its traffic is gone.
3. **Separate apps** mounted with `app.mount("/v1", v1_app)` when versions differ deeply; each gets its own `/docs`.

## Swagger UI and docs in production

The docs endpoints are configurable or can be removed. Keep OAuth2 tokens across page reloads while developing with
`persistAuthorization`.

```python
app = FastAPI(
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.environment != "production" else None,
    swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True},
)
```

When the public API needs docs, publish the exported `openapi.json` on a docs site instead of exposing the live app's
schema.

## Export openapi.json without a server

`app.openapi()` builds the schema in-process, so CI can export it without running Uvicorn. `scripts/export_openapi.py`
writes it with sorted keys for clean diffs, or checks that the committed file is current.

```bash
uv run python scripts/export_openapi.py app.main:app openapi.json            # write
uv run python scripts/export_openapi.py app.main:app openapi.json --check    # CI: exit 1 when stale
```

When a script from `scripts/` is not on disk (a pack install), read that script from the library that served this page and save it first.

Commit `openapi.json`. A pull request that changes the API then shows the schema diff for review.

## Catch breaking changes in CI

A schema diff tool compares the branch's schema with the main branch's and fails on breaking changes: a removed
endpoint, a removed response field, a new required request field, a narrowed type.

```bash
git show origin/main:openapi.json > /tmp/openapi-main.json
uv run python scripts/export_openapi.py app.main:app openapi.json
docker run --rm -v "$PWD:/work" -v /tmp:/base tufin/oasdiff breaking /base/openapi-main.json /work/openapi.json --fail-on ERR
```

oasdiff (Apache-2.0) runs as a Docker image or a Go binary. Pair it with the contract tests in fastapi-testing for the
fields that released clients read.

## Generate SDKs from the schema

Generated clients keep frontends and mobile apps in step with the API. The FastAPI docs show Hey API for TypeScript;
OpenAPI Generator covers most other languages.

```bash
npx @hey-api/openapi-ts -i ./openapi.json -o src/client                 # TypeScript (0.99 on 2026-10-28)
docker run --rm -v "$PWD:/local" openapitools/openapi-generator-cli generate \
  -i /local/openapi.json -g dart-dio -o /local/clients/dart               # Dart, Kotlin, Swift, Go ...
```

Generate from the exported file in CI, not from a running server, so the client matches the commit exactly.

## Verify the OpenAPI contract

These commands fail when the committed schema is stale, when a client-facing name is ugly, or when a change breaks a
client.

```bash
uv run python scripts/export_openapi.py app.main:app openapi.json --check     # exit 1 when stale
uv run python - <<'PY'
from app.main import app
ids = [op["operationId"] for ops in app.openapi()["paths"].values() for op in ops.values()]
assert len(ids) == len(set(ids)), "duplicate operation ids"
assert not [i for i in ids if i.endswith(("_get", "_post", "_put", "_patch", "_delete"))], "default ids left"
print(len(ids), "operations")
PY
git show origin/main:openapi.json > /tmp/openapi-main.json
docker run --rm -v "$PWD:/work" -v /tmp:/base tufin/oasdiff breaking /base/openapi-main.json /work/openapi.json --fail-on ERR
```

## OpenAPI anti-patterns

These make the schema look finished while clients still guess.

| Do not | Do instead |
|---|---|
| default operation ids | `generate_unique_id_function` before the first client ships |
| undocumented errors | `responses={409: {"model": Problem}}` |
| `Query(example=...)` | `examples=[...]` or `openapi_examples={...}` |
| generate an SDK from a running server | generate from the exported file, so it matches the commit |
| rename a released field | add the new one, deprecate the old |
| public `/docs` on an internal API | turn the docs off, or publish the exported file |

## OpenAPI checklist

Go through this OpenAPI and client generation checklist before merging a change.

```text
[ ] title, version, description, tags metadata set on the app
[ ] every route tagged; operation ids generated from tag and function name before the first client ships
[ ] error responses declared with a model; bodies have examples; routes have summaries or docstrings
[ ] internal routes hidden; old routes marked deprecated before removal
[ ] docs and openapi.json disabled or protected in production
[ ] openapi.json exported by script, committed, checked in CI
[ ] breaking-change diff against main fails the build
[ ] SDKs generated from the exported file
```

## Common OpenAPI and client generation problems

These are the OpenAPI and client generation problems people hit most, each with its symptom, cause and fix.

1. **SDK methods named `readItemItemsItemIdGet`.** Default operation ids. Set `generate_unique_id_function`.
2. **`Input` and `Output` schema names** (`ItemRead-Input`, `ItemRead-Output`). FastAPI splits a model when its
   input and output JSON Schema differ (defaults, computed fields). This is correct; set
   `separate_input_output_schemas=False` only for a generator that cannot cope.
3. **`/openapi.json` keeps the old title or description after a test changes it.** `app.openapi()` caches
   `app.openapi_schema`. Since 0.137 a change to the routes rebuilds it, but nothing else does; before 0.137 not even
   that did. Set `app.openapi_schema = None` to rebuild.
4. **`FastAPIDeprecationWarning` from `Query(example=...)`.** `example` is deprecated, and the route still runs; use
   `examples=[...]` or `openapi_examples={...}`. A keyword FastAPI does not know, such as `Query(custom="x")`, raises
   `TypeError` since 0.155; put it in `json_schema_extra={...}`.
5. **Docs show the wrong server URL behind a proxy.** The app does not know its public prefix; run
   `fastapi run --root-path /api` or set `servers=` (fastapi-deployment).

## See also

- fastapi-pydantic-models: how models become schemas.
- fastapi-routing: status codes and response models.
- fastapi-testing: contract tests for released clients.
- fastapi-deployment: `--root-path` and docs in production.
- fastapi-mcp-server: operation ids become tool names.
