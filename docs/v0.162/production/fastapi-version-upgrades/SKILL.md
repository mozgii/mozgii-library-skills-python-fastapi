---
name: fastapi-version-upgrades
description: Upgrades a FastAPI service and its stack without surprises - pinning FastAPI to a minor range, a step-by-step upgrade with uv, deprecations as errors, an OpenAPI diff, and breaking changes by symptom for FastAPI 0.100 to 0.162, Starlette 1.0, Pydantic v1 to v2, SQLAlchemy 2.1, httpx 0.28 and pytest-asyncio 1.x. Use when upgrading FastAPI or a dependency, when an app broke after an upgrade (sudden 422s, 401 instead of 403, TypeError from an unknown Query keyword), or when modernizing old FastAPI or tutorial code.
metadata:
  tags:
  - fastapi
  - upgrade
  - migration
  - breaking-changes
  - pydantic-v2
  - starlette
  - deprecations
---

# Upgrading FastAPI and its stack

This skill moves a FastAPI service to current versions (October 2026: FastAPI 0.162, Starlette 1.10, Pydantic 2.14,
SQLAlchemy 2.1, pytest 9.2, pytest-asyncio 1.4) one step at a time, and maps each "it broke after the upgrade"
symptom to the release that caused it. FastAPI is still on 0.x: a minor release (0.161 → 0.162) can change behaviour.

## Step 0: installed versions before an upgrade

Write down what the project runs now; this list is the start of the upgrade plan.

```bash
uv run python --version
uv tree --depth 1
uv tree --outdated --depth 1                                    # what each direct dependency could move to
uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), sort_keys=True))" > /tmp/openapi-before.json
```

## Pin FastAPI to a minor range

Because breaking changes land in minor releases, pin FastAPI to the minor version you tested, and move it on purpose.
The FastAPI docs recommend the same.

```bash
uv add "fastapi[standard]>=0.162.0,<0.163.0"
uv tree --depth 1                                     # what is installed now
uv lock --upgrade-package fastapi --upgrade-package starlette    # move only these, keep the rest
```

Commit `uv.lock`. Pin Pydantic and SQLAlchemy to a major range (`pydantic>=2.14.2,<3`, `sqlalchemy[asyncio]>=2.1,<2.2`),
since those follow semantic versioning more strictly.

## The upgrade procedure

An upgrade is safe when each step is small enough that a failure points to one cause. Do these in order, with a
commit after each green run.

1. **Read the release notes** between your version and the target, every "Breaking Changes" section
   (fastapi.tiangolo.com/release-notes). The table below covers the ones that bite most.
2. **Make deprecations fail** on the current version first, and fix them while the old behaviour still works.
   `FastAPIDeprecationWarning` subclasses `UserWarning`, so the `DeprecationWarning` filters miss it and it needs its
   own line. That class arrived in 0.127.1; on older versions leave the line out, or pytest stops with `AttributeError`:

   ```toml
   [tool.pytest.ini_options]
   filterwarnings = [
       "error::DeprecationWarning:app.*",
       "error::DeprecationWarning:fastapi.*",
       "error::fastapi.exceptions.FastAPIDeprecationWarning",   # FastAPI 0.127.1+
   ]
   ```

3. **Upgrade one family at a time**: FastAPI with Starlette; then Pydantic; then SQLAlchemy with Alembic and the
   driver; then the test stack. Run the full suite after each.
4. **Diff the OpenAPI schema** before and after (`app.openapi()` exported to a file, then a diff or oasdiff); an
   upgrade can change parameter schemas, which breaks generated clients.
5. **Run the app for real**: `fastapi run`, hit `/docs`, log in, upload a file, open a stream. Form, header and
   streaming changes show up there before they show in unit tests.

## Symptoms after upgrading FastAPI

Most upgrade bugs are one of these, found by what you see rather than by version number.

| You see | Cause (version) | Fix |
|---|---|---|
| 422 for a JSON request that worked | the client sends no `Content-Type` (0.132) | send `application/json`; `FastAPI(strict_content_type=False)` only for clients you cannot change |
| 422 when the client sends `null` for a field with a default | `null` is no longer "omitted" (0.151) | omit the key, or make the type nullable |
| 422 `extra_forbidden` for a query parameter you declared | a parameter model with `extra="forbid"` now covers sibling parameters (0.160) | move all query fields of that endpoint into the model |
| a validator no longer runs for an omitted parameter | defaults are not validated (0.148) | `Query(validate_default=True)` or `Field(validate_default=True)` |
| a checkbox form field is missing instead of `False` | omitted booleans use their default again (0.162; 0.147–0.161 forced `False`) | declare `newsletter: bool = False` |
| an empty form text field now uses the default | empty strings count as missing in forms (0.149) | declare `str = ""` if you need the empty string |
| `TypeError: Query() got an unexpected keyword argument` | unknown keywords removed (0.155) | `json_schema_extra={...}` |
| `FastAPIDeprecationWarning` from `Query(example=...)` | `example` is deprecated, and still runs | `examples=[...]` or `openapi_examples={...}` |
| tests expect 403 for a missing token, get 401 | security classes answer 401 (0.122) | update the tests; subclass the scheme only for a client that depends on 403 |
| `FastAPIError: Response not awaited` | an `except` after `yield` in a dependency swallowed the error (0.110) | re-raise in the dependency |
| a streaming response says the session is closed | yield cleanup ran before streaming in 0.106–0.117 | upgrade to 0.118+; keep the session dependency on the default scope |
| code walking `app.routes` misses included routes | routers are kept as a tree, not copied (0.137) | `fastapi.routing.iter_route_contexts()` or the OpenAPI schema |
| `pip install fastapi-slim` finds nothing new | the slim package was dropped in 0.129.2; its last release is 0.129.1 | `fastapi` without extras |
| traces stopped after an upgrade | exporter setup became opt-in (0.143) | `FASTAPI_OTEL_AUTO_CONFIGURE=true` |
| `FastAPIDeprecationWarning` for `FastAPI(root_path=...)` | deprecated (0.146) | `fastapi run --root-path /api` |
| `openapi_prefix` argument refused | removed (0.145) | `--root-path` |
| a header with underscores is no longer read | underscore headers are refused with the default conversion (0.136.3) | send the hyphenated name, or `Header(convert_underscores=False)` |
| models imported from `pydantic.v1` fail at startup | Pydantic v1 support dropped (0.126, `pydantic.v1` in 0.128) | migrate the models (below) |
| install refuses Python 3.9 | dropped in 0.129; Starlette 1.8+ and SQLAlchemy 2.1 need 3.11 | move to Python 3.13 or 3.14 |

## Starlette 1.0 removals

FastAPI supports Starlette 1.0 since 0.133, and Starlette 1.0 removed the event-handler API that old tutorials use.

- `@app.on_event("startup")`, `on_startup=` and `on_shutdown=`: FastAPI still accepts them. `@app.on_event` warns at
  runtime; `on_startup=` and `on_shutdown=` give no warning at all, so search for them. Move the code into a
  `lifespan` function now (see also fastapi-project-structure).
- `Jinja2Templates.TemplateResponse(name, {"request": request})` is gone: write
  `templates.TemplateResponse(request, "page.html", {"item": item})`.
- `FileResponse(method=...)` is gone.
- Starlette's own `@app.route()`, `@app.middleware()` and `@app.exception_handler()` decorators are gone from plain
  Starlette apps; FastAPI's `@app.middleware("http")` and `@app.exception_handler()` still work.
- Starlette's `TestClient` now runs on `httpx2`; `fastapi[standard]` installs it.

## Pydantic v1 to v2

FastAPI runs only on Pydantic v2 since 0.128. Most of the work is renames; `bump-pydantic` (`uvx bump-pydantic app/`)
does many of them automatically, though it is unmaintained since 2023, so review its diff.

| Pydantic v1 | Pydantic v2 |
|---|---|
| `obj.dict()`, `obj.json()`, `obj.copy()` | `obj.model_dump()`, `obj.model_dump_json()`, `obj.model_copy()` |
| `Model.parse_obj(d)`, `Model.parse_raw(s)` | `Model.model_validate(d)`, `Model.model_validate_json(s)` |
| `class Config: orm_mode = True` | `model_config = ConfigDict(from_attributes=True)` |
| `allow_population_by_field_name` | `validate_by_name=True` (2.11+; `populate_by_name` before) |
| `schema_extra` | `json_schema_extra` |
| `@validator("x")`, `@root_validator` | `@field_validator("x")` with `@classmethod`, `@model_validator(mode="after")` |
| `Field(regex=...)`, `min_items`, `max_items` | `Field(pattern=...)`, `min_length`, `max_length` |
| `Optional[int]` meaning "may be omitted" | `int \| None = None`: in v2 `int \| None` without a default is required |
| `from pydantic import BaseSettings` | `from pydantic_settings import BaseSettings` (`uv add pydantic-settings`) |
| `Model.update_forward_refs()`, `__fields__` | `Model.model_rebuild()`, `Model.model_fields` |
| `__root__` models | a plain annotated type such as `list[Item]`; FastAPI's guidance is to avoid `RootModel` |

The trap that tests rarely catch: in v1 `Optional[X]` gave an implicit `None` default; in v2 it does not, so a field
that clients used to omit becomes required and returns 422.

## SQLAlchemy 2.0 to 2.1

SQLAlchemy 2.1 keeps the 2.0 API but changes packaging and a few defaults that async FastAPI apps notice at once.

- `greenlet` is no longer installed by default: `uv add "sqlalchemy[asyncio]"`, or async code fails with
  `No module named 'greenlet'`.
- Python 3.11 or later is required.
- A plain `postgresql://` URL now selects psycopg 3, not psycopg2; name the driver (`postgresql+asyncpg://`).
- `filter_by()` searches every entity in the `FROM` and raises on an ambiguous name; prefer `where(Model.col == v)`.
- Code still on 1.x patterns (`session.query()`, `Column()` on declarative classes) runs, but write new code with
  `select()` and `Mapped[...]` (see also fastapi-sqlalchemy-async).

## Test stack: httpx, pytest and pytest-asyncio

The test stack changed under many FastAPI suites at once, so an old suite can fail before any app code runs.

| You see | Cause | Fix |
|---|---|---|
| `TypeError: ... unexpected keyword argument 'app'` | httpx 0.28 removed `AsyncClient(app=...)` | `AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")` |
| `unexpected keyword argument 'proxies'` | httpx 0.28 removed `proxies=` | `proxy="http://..."` or a mounted transport |
| `async def functions are not natively supported` | pytest 8.4+ fails unmarked async tests (older pytest skipped them) | `asyncio_mode = "auto"` in pytest settings |
| `fixture 'event_loop' not found` | pytest-asyncio 1.0 removed it | delete the override; set `asyncio_default_fixture_loop_scope` |
| deprecation for an `event_loop_policy` override | pytest-asyncio 1.4 | the `pytest_asyncio_loop_factories` hook |
| `testcontainers.postgres` is deprecated | the module moved | `from testcontainers.community.postgres import PostgresContainer` |

Count your tests before and after the move to auto mode: suites that ran under old pytest-asyncio strict mode often
contained async tests that never ran, and some of them fail once they do.

## Libraries that were replaced

Old FastAPI tutorials recommend libraries that the FastAPI docs and the official full-stack template have since
replaced; swap them during an upgrade.

| Old | Current | Note |
|---|---|---|
| `passlib[bcrypt]` | `pwdlib[argon2,bcrypt]` | passlib is unmaintained and breaks with new bcrypt; the `bcrypt` extra reads old hashes and `verify_and_update()` rehashes them on login; drop it to plain `pwdlib[argon2]` once none are left |
| `python-jose` | `pyjwt[crypto]` | `jwt.decode(token, key, algorithms=["HS256"])` |
| `ORJSONResponse`, `UJSONResponse` | a return type or `response_model` | deprecated in 0.131; Pydantic's Rust serializer is faster |
| `@app.on_event` | `lifespan` | Starlette 1.0 removed its own version |
| `uvicorn.workers.UvicornWorker` | `uvicorn_worker.UvicornWorker` | `uv add uvicorn-worker` |
| `requests` in `async def` | `httpx.AsyncClient` | requests blocks the event loop |
| `tiangolo/uvicorn-gunicorn-fastapi` image | your own `python:3.14-slim` image with uv | the old image is deprecated (see also fastapi-deployment) |
| `fastapi-slim` | `fastapi` | dropped in 0.129.2 |

## Verify an upgrade

Compare the end state with the list from Step 0.

```bash
uv run pytest -q                                                 # filterwarnings from the procedure above stays on
uv run pytest --collect-only -o addopts="" -q | tail -1          # the same test count, or more
uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), sort_keys=True))" > /tmp/openapi-after.json
diff /tmp/openapi-before.json /tmp/openapi-after.json && echo "schema unchanged"
grep -rnE 'on_startup|on_shutdown|on_event\(' app/               # expected: nothing, since on_startup= never warns
grep -nE 'passlib|python-jose|orjson' pyproject.toml             # expected: nothing
```

## Upgrade anti-patterns

Each of these turns one small step into a long search for the cause.

| Do not | Do instead |
|---|---|
| bump everything in one commit | one library family per commit |
| `fastapi>=0.100` with no upper bound | a minor range |
| skip the release notes because tests pass | read every "Breaking Changes" section |
| silence a deprecation warning | fix it while the old behaviour still works |
| trust a green suite after moving to auto mode | compare test counts |
| search-and-replace a Pydantic v1 name | run `bump-pydantic`, then review the diff |

## Upgrade checklist

Go through this upgrade checklist before merging the version bump.

```text
[ ] FastAPI pinned to one minor range; uv.lock committed
[ ] release notes read for every version skipped; breaking changes listed in the PR
[ ] deprecation warnings from app and fastapi made errors and fixed before the bump
[ ] one library family per commit; full test suite green after each
[ ] test count compared before and after; no async tests silently skipped
[ ] OpenAPI schema diffed; generated clients regenerated if it changed
[ ] forms, uploads, headers, streams and login tried by hand on fastapi run
[ ] passlib, python-jose, ORJSONResponse, on_event and the old worker class gone
```

## Common upgrade problems

These are the upgrade problems people hit most, each with its symptom, cause and fix.

1. **Everything breaks at once.** Several majors were bumped in one go. Revert, then move one family per commit.
2. **The lock file pulls a newer FastAPI on CI.** The dependency was unpinned (`fastapi>=0.100`). Pin a minor range.
3. **Mobile clients start getting 422.** A strictness change (0.132 Content-Type, 0.151 `null`) hit a client you
   do not control. Ship a server-side fix or keep the compatibility switch until the old client versions retire.
4. **Pyright errors appear after the upgrade.** New type hints in FastAPI or Pydantic expose real mismatches; fix
   them rather than pinning back.
5. **Tests pass, production fails on startup.** The production image has a different Python or a stale lock. Build
   the image in CI from the same `uv.lock` the tests used.

## See also

- fastapi-pydantic-models: schemas on Pydantic 2.14.
- fastapi-testing: the current test stack.
- fastapi-sqlalchemy-async: SQLAlchemy 2.1 settings.
- fastapi-openapi: schema diffs in CI.
- fastapi-deployment: replacing old images and worker classes.
