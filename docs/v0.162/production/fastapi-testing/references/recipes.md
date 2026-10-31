---
title: FastAPI test recipes
author: MOZGII, Inc.
lang: en
---

# FastAPI test recipes

This reference collects tests that FastAPI services need beyond happy paths: tenant isolation, response contracts
for old clients, audit coverage, test data factories, external HTTP calls, editor setup and CI.

## Contents

This reference on FastAPI test recipes has these sections:

- Tenant isolation test
- Response contract test for released clients
- Audit coverage for admin writes
- Test data factories
- Faking external HTTP APIs
- Testing settings and feature flags
- Property-based tests from the OpenAPI schema
- VS Code setup
- GitHub Actions workflow

## Tenant isolation test

An isolation test seeds real rows for two tenants and asks as the wrong one. It must get nothing back: not a 403 that
confirms the data exists, not data filtered in the client.

```python
async def test_org_cannot_read_another_orgs_items(session: AsyncSession, as_user) -> None:
    org_a, org_b = await seed_orgs(session, 2)
    await seed_item(session, org=org_a, title="Private")

    response = await as_user(member_of(org_b)).get("/items")

    assert response.status_code == 200
    assert response.json() == []


async def test_org_cannot_fetch_another_orgs_item_by_id(session: AsyncSession, as_user) -> None:
    org_a, org_b = await seed_orgs(session, 2)
    item = await seed_item(session, org=org_a)

    response = await as_user(member_of(org_b)).get(f"/items/{item.id}")

    assert response.status_code == 404
```

Write one per read endpoint and one per write endpoint (PATCH and DELETE of another tenant's row must fail with
404 and leave the row unchanged). A parametrized test over every route that takes an id keeps this cheap.

## Response contract test for released clients

Released mobile and desktop builds keep running for months and read a fixed set of fields; a strictly typed client
(Dart, Swift, Kotlin) crashes on a missing one. Freeze the field set when a version ships.

```python
V1_ITEM_FIELDS = {"id", "title", "description", "ownerId", "createdAt"}


async def test_item_response_keeps_every_v1_field(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    created = (await client.post("/v1/items", json={"title": "x"}, headers=auth)).json()
    body = (await client.get(f"/v1/items/{created['id']}", headers=auth)).json()
    assert V1_ITEM_FIELDS <= body.keys()
```

Adding fields is safe; removing or renaming one fails this test, which is when you want to hear about it. A
snapshot of the OpenAPI schema (fastapi-openapi) catches the same change at the schema level.

## Audit coverage for admin writes

Every admin write must record who did what to which record, with the values before and after; an admin change
without an audit row cannot be told apart from a breach.

```python
async def test_admin_status_change_is_audited(session: AsyncSession, as_admin) -> None:
    record = await seed_record(session, status="active")

    await as_admin.patch(f"/admin/records/{record.id}", json={"status": "blocked"})

    entry = await latest_audit_entry(session)
    assert entry.operator_id is not None
    assert entry.target == f"record:{record.id}"
    assert entry.before == {"status": "active"}
    assert entry.after == {"status": "blocked"}
```

## Test data factories

Factories build valid objects with sensible defaults so a test states only what matters to it. Plain functions are
enough for most suites; polyfactory 3.5 generates Pydantic and SQLAlchemy objects from their types.

```python
from itertools import count

_ids = count(1)


async def make_user(session: AsyncSession, **overrides: object) -> User:
    n = next(_ids)
    user = User(email=f"user{n}@example.com", hashed_password="not-a-real-hash", **overrides)
    session.add(user)
    await session.flush()
    return user
```

Use `flush()`, not `commit()`, in factories: the row gets an id and stays inside the test's transaction.

## Faking external HTTP APIs

Code that calls another API through `httpx.AsyncClient` can be tested with `httpx.MockTransport`, without network
access or extra libraries.

```python
def handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/latest"
    return httpx.Response(200, json={"EUR": 1.0, "USD": 1.08})


@pytest.fixture
def rates_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://rates.example.com")
```

Inject the client through a dependency or lifespan state so the test can swap it.

## Testing settings and feature flags

Settings read through a dependency can be overridden per test.

```python
@pytest.fixture
def production_settings() -> Iterator[Settings]:
    s = Settings(environment="production", database_url="postgresql+asyncpg://t:t@db/t", secret_key="x" * 32)
    app.dependency_overrides[get_settings] = lambda: s
    yield s
    app.dependency_overrides.pop(get_settings, None)
```

## Property-based tests from the OpenAPI schema

Schemathesis (MIT) reads the app's OpenAPI schema and sends many generated requests to every endpoint, including
edge cases people do not write by hand (huge numbers, empty strings, odd Unicode); it fails on any 500 and on any
response that does not match the documented schema.

```python
# tests/test_schema.py   (uv add --dev schemathesis)
import schemathesis
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import response_schema_conformance

from app.main import app

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


@schema.parametrize()
def test_api_matches_its_schema(case: schemathesis.Case) -> None:
    # get_current_user is overridden in conftest, so no token is needed
    case.call_and_validate(checks=[not_a_server_error, response_schema_conformance])
```

Name the two checks. The default set also wants every schema-violating request refused, and Pydantic's lax mode accepts
`"1"` for an `int`, so a correct `x: int` body fails it.

Run it against the test database with the session and user overrides in place, and in CI on every change to routes or schemas. A failure
prints the exact request that broke, ready to copy into a regular test.

## VS Code setup

The Python extension discovers pytest and runs or debugs single async tests from the editor gutter.

```jsonc
// .vscode/settings.json
{
  "python.testing.pytestEnabled": true,
  "python.testing.unittestEnabled": false,
  "python.testing.pytestArgs": ["tests"],
  "python.analysis.typeCheckingMode": "strict"
}
```

"Debug Test" from the gutter stops on breakpoints inside async tests, which is the fastest way to understand an event
loop scope failure.

## GitHub Actions workflow

A CI job installs uv, syncs locked dependencies and runs lint, types, migrations checks and tests; Docker is
available on `ubuntu-latest`, so testcontainers works without a service container.

```yaml
name: test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10.3.3
        with:
          enable-cache: true
      - run: uv sync --locked
      - run: uv run ruff check . && uv run ruff format --check .
      - run: uv run pyright
      - run: uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=85
```

`setup-uv` has no moving `v10` tag, so name an exact release; `@v10` fails to resolve.
