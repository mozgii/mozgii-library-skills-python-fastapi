---
title: Tests for FastAPI errors and request ids
author: MOZGII, Inc.
lang: en
---

# Tests for FastAPI errors and request ids

This reference holds pytest tests for the catch-all middleware and the request id check. They need a `/health` route
and the app with its three middleware.

## Contents

This reference on tests for FastAPI errors and request ids has these sections:

- A 500 that keeps its headers
- Hostile X-Request-Id values

## A 500 that keeps its headers

The client below sets `raise_app_exceptions=False`, so the test sees the 500 and not the exception the catch-all raises
again after answering.

```python
from collections.abc import AsyncIterator

import httpx
import pytest

from app.main import app  # the app with the three middleware above


@app.get("/boom", include_in_schema=False)
async def boom() -> None:
    raise RuntimeError("secret table name")


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_unhandled_error_keeps_every_header(http: httpx.AsyncClient) -> None:
    response = await http.get("/boom", headers={"Origin": "https://app.example.com"})
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["x-request-id"]
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "secret" not in response.text
```

## Hostile X-Request-Id values

A real client cannot send most of these bytes, so the test calls the ASGI app directly. Each value must get a fresh id.

```python
@pytest.mark.parametrize("bad", [b"\xff\xfe", b"a\r\nset-cookie: x=1", b"a b", b"x" * 65])
async def test_bad_request_id_is_replaced(bad: bytes) -> None:
    sent: dict[bytes, bytes] = {}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            sent.update(message["headers"])

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "path": "/health",
             "raw_path": b"/health", "query_string": b"", "root_path": "", "scheme": "http", "server": ("t", 80),
             "client": ("c", 1), "headers": [(b"x-request-id", bad)]}
    await app(scope, receive, send)
    assert sent[b"x-request-id"] != bad
```
