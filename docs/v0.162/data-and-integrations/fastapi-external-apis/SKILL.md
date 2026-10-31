---
name: fastapi-external-apis
description: Calls third-party HTTP APIs from FastAPI and receives their webhooks safely - a shared httpx AsyncClient from the lifespan, timeouts, retries with backoff only when safe, upstream errors mapped to 502/503/504, Idempotency-Key for your own POST endpoints, webhook signatures checked on the raw body, duplicate events dropped, signed outgoing webhooks and tests with httpx.MockTransport. Use when integrating Stripe, GitHub, Slack or an LLM provider, when outbound calls hang, when retries create duplicates, or when adding a webhook endpoint.
metadata:
  tags:
  - fastapi
  - httpx
  - external-api
  - webhooks
  - retries
  - timeouts
  - idempotency
  - integrations
---

# FastAPI and external APIs

This skill covers the two directions of an integration: your FastAPI app calling another service, and another
service calling your app with a webhook. Both fail in the same ways (slow networks, retries, duplicates), so the
rules are the same: bound every wait, repeat only what is safe to repeat, and remember what you already did.
Checked on 2026-10-28 with FastAPI 0.162, httpx 0.28.1 and stamina 26.1.

## Step 0: installed versions for outbound calls

Read the client versions first. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastapi|httpx|httpx2|stamina|standardwebhooks) '
```

Everything here runs on httpx 0.28 and on `httpx2`. stamina's backoff hook, a function passed to `on=` that returns a
wait, needs stamina 25.2 (26.1 was checked), so upgrade before using the retry example.

## Choose what may be retried

A retry is safe only when doing the work twice cannot hurt, so the method, the key and the answer decide.

| The call | On failure | Why the others lose |
|---|---|---|
| a read (`GET`) | retry 2 or 3 times with backoff on timeouts, connection errors and 502, 503, 504 | failing at once turns a brief network error into an error for the user |
| a write with an `Idempotency-Key` | the same retries, sending the same key | a new key per attempt does the work twice |
| a write with no key | never retry; report the failure | a timeout does not say whether the first attempt worked |
| any call answered 429 | wait for `Retry-After`, capped, then retry | retrying at once makes the rate limit worse |
| any call answered another 4xx | do not retry; map it to your own error | the same request gets the same answer |
| work that must finish eventually | a queue job with its own retry policy | a request cannot wait minutes |

## One shared HTTP client per service

An `httpx.AsyncClient` keeps a connection pool and TLS sessions, so create it once in the lifespan and share it;
a client created per request reconnects every time and adds a TLS handshake to each call.

```python
# app/clients.py
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, TypedDict

import httpx
from fastapi import Depends, FastAPI, Request

from app.config import get_settings


class State(TypedDict):
    payments: httpx.AsyncClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[State]:
    settings = get_settings()
    async with httpx.AsyncClient(
        base_url="https://api.payments.example.com/v2",
        headers={"Authorization": f"Bearer {settings.payments_key.get_secret_value()}"},
        timeout=httpx.Timeout(10.0, connect=3.0),       # 3 s to connect, 10 s for each read, write and pool wait
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
    ) as payments:
        yield {"payments": payments}


def get_payments(request: Request) -> httpx.AsyncClient:
    return request.state.payments


PaymentsClient = Annotated[httpx.AsyncClient, Depends(get_payments)]
```

- One client per upstream service, with its `base_url`, auth header and limits; the key comes from settings as a
  `SecretStr` and never appears in logs.
- The dependency makes the client easy to replace in tests (below).
- `fastapi[standard]` installs both `httpx` and `httpx2`, the Pydantic-maintained continuation; `httpx2` has the same
  `AsyncClient`, `Timeout`, `Limits` and `MockTransport` names, so either works with this code.

## Timeouts on every outbound call

Outbound call timeouts in httpx default to 5 seconds for each phase, which is often too long for a request path and
too short for a report API. Set timeouts per client on purpose, and cap the whole operation when an endpoint makes
several calls.

```python
import asyncio


@router.get("/checkout/{order_id}/quote")
async def quote(order_id: int, payments: PaymentsClient) -> Quote:
    async with asyncio.timeout(8):                       # total budget for this endpoint's upstream work
        rates = await payments.get("/rates", params={"order": order_id})
        fees = await payments.get("/fees", params={"order": order_id})
    return Quote.from_upstream(rates.json(), fees.json())
```

- `httpx.Timeout(10.0, connect=3.0)` sets the read, write and pool waits to 10 s and the connect wait to 3 s.
- A slow upstream with no timeout holds a worker's connection slot and, with enough traffic, every slot.
- For a single slow call, pass `timeout=60` on that call instead of raising the client default.

## Retries with backoff, only when the call is safe to repeat

A retry helps with a brief network error or a 503, but repeating a non-idempotent call (creating a charge) can do
the work twice. Retry reads and calls that carry an idempotency key; never retry a plain POST.

`Retry-After` holds either a number of seconds or an HTTP date, so `float()` alone raises `ValueError` on the second
form; parse both, and cap the wait so one answer cannot hold a request.

```python
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import stamina


def retry_after_seconds(value: str | None, default: float = 1.0, limit: float = 10.0) -> float:
    if value is None:
        return default
    if value.strip().isdecimal():                       # Retry-After: 120
        return min(float(value), limit)
    try:
        when = parsedate_to_datetime(value)             # Retry-After: Wed, 21 Oct 2026 07:28:00 GMT
    except ValueError:
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return min(max((when - datetime.now(UTC)).total_seconds(), 0.0), limit)


def should_retry(exc: Exception) -> bool | float:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429:
            return retry_after_seconds(exc.response.headers.get("Retry-After"))   # wait as the server asks
        return exc.response.status_code in {502, 503, 504}
    return isinstance(exc, httpx.TransportError)                        # connection errors and timeouts


@stamina.retry(on=should_retry, attempts=3, timeout=15)
async def fetch_customer(payments: httpx.AsyncClient, customer_id: str) -> dict[str, object]:
    response = await payments.get(f"/customers/{customer_id}")
    response.raise_for_status()
    return response.json()
```

- stamina (MIT, built on Tenacity) adds exponential backoff with jitter; a hook that returns a number sets that
  wait. Tenacity works too, with more configuration.
- `httpx.AsyncHTTPTransport(retries=2)` retries only failed connections, not 5xx answers.
- Keep attempts low inside a request (2–3). A job that must eventually succeed belongs in a queue with its own retry
  policy (see also fastapi-background-tasks).

## Map upstream failures to your own errors

A client of your API should see your error format, not the upstream's body or stack. Translate failures once, near
the call, into a domain error that a handler maps to 502, 503 or 504.

```python
class UpstreamError(Exception):
    def __init__(self, service: str, status: int) -> None:
        super().__init__(f"{service} failed")
        self.service, self.status = service, status


async def charge(payments: httpx.AsyncClient, body: ChargeRequest, key: str) -> ChargeResult:
    try:
        response = await payments.post("/charges", json=body.model_dump(mode="json"), headers={"Idempotency-Key": key})
        response.raise_for_status()
    except httpx.TimeoutException:
        raise UpstreamError("payments", 504) from None
    except httpx.HTTPStatusError as exc:
        logger.warning("payments error", extra={"upstream_status": exc.response.status_code})
        if exc.response.status_code in {400, 402, 409, 422}:
            raise PaymentDeclined() from None                      # the caller's problem: 4xx in your API
        raise UpstreamError("payments", 502) from None
    except httpx.TransportError:
        raise UpstreamError("payments", 503) from None
    return ChargeResult.model_validate(response.json())         # validate what came back, too
```

Validate upstream responses with a Pydantic model: a changed field upstream then fails at the boundary with a clear
error instead of deep inside your code.

## Do not hold a database transaction during an outbound call

A transaction left open while waiting on the network keeps a pooled connection and its row locks for the whole
wait; under load the pool runs dry. Read what you need, end the transaction, call out, then write the result in a new
short transaction.

```python
async def sync_customer(session: AsyncSession, payments: httpx.AsyncClient, user_id: int) -> None:
    user = await session.get(User, user_id)
    external_id = user.payments_id
    await session.commit()                                   # release the connection before the call
    data = await fetch_customer(payments, external_id)
    user.payments_status = str(data["status"])
    await session.commit()
```

## Idempotency keys for your own POST endpoints

Clients retry a POST after a timeout without knowing whether the first one worked; an `Idempotency-Key` header lets
the server return the first result instead of creating a second order or charge. Store the key with the response,
unique per user.

The table and endpoint code that claim a key with `INSERT ... ON CONFLICT DO NOTHING` are in
[references/idempotency.md](references/idempotency.md).

- The key row, the order and the stored response commit in one transaction. A retry that arrives while the first
  request is still running waits on the unique index until the first commits, then replays its response; if the
  first fails, its key rolls back with it and the retry runs normally.
- Delete keys older than a day or two with a scheduled job. Stripe and many payment APIs use the same header name,
  so clients already know it.

## Receiving webhooks with a signature check

A webhook is an unauthenticated POST from the internet until you check its signature, and the signature covers the
exact bytes sent. Read the raw body, verify it, and only then parse it; a Pydantic body parameter would re-encode the
JSON and the check would fail.

```python
import hashlib
import hmac

from fastapi import Header, Request


@router.post("/webhooks/github", status_code=202, include_in_schema=False)
async def github_webhook(
    request: Request,
    session: SessionDep,
    x_hub_signature_256: Annotated[str, Header()],
    x_github_delivery: Annotated[str, Header()],
) -> None:
    raw = await request.body()
    secret = get_settings().github_webhook_secret.get_secret_value().encode()
    expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(401, "Bad signature")
    event = GitHubEvent.model_validate_json(raw)
    inserted = await session.scalar(
        insert(WebhookEvent).values(source="github", event_id=x_github_delivery, payload=event.model_dump(mode="json"))
        .on_conflict_do_nothing(index_elements=["source", "event_id"]).returning(WebhookEvent.id)
    )
    await session.commit()
    if inserted is not None:
        await enqueue("process_github_event", inserted)      # do the work in a worker, answer fast
```

- Compare with `hmac.compare_digest`, never `==`, so timing does not leak the signature.
- Providers retry on timeouts and non-2xx answers, so the same event arrives more than once: a unique
  `(source, event_id)` turns duplicates into no-ops.
- Answer within a few seconds; providers give up or retry when you are slow. Store, enqueue, return 2xx.
- When the signature includes a timestamp (Stripe, Standard Webhooks), reject events older than about five minutes
  to stop replays. Use the provider's library when it has one: `stripe.Webhook.construct_event(raw, signature,
  secret)`, or `standardwebhooks.Webhook(secret).verify(raw, headers)` for the Standard Webhooks format.

## Sending webhooks to your customers

When your API notifies other systems, sign each delivery and send it from a queue worker, so a slow receiver never
slows your requests and a failed delivery is retried.

```python
def sign(secret: bytes, event_id: str, timestamp: int, body: bytes) -> str:
    message = f"{event_id}.{timestamp}.".encode() + body
    return "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest()
```

- Send headers with the event id, the timestamp and the signature; keep the event id stable across retries so the
  receiver can drop duplicates.
- Retry with growing delays for a day or so, record each attempt, and disable an endpoint that keeps failing.
- Never deliver to an address a customer typed without an allow-list check: block private and link-local addresses
  (SSRF), and resolve the host once per delivery.

Document the events in your OpenAPI schema with `app.webhooks`, so customers see the payloads in `/docs`:

```python
@app.webhooks.post("order-paid")
def order_paid(body: OrderPaidEvent) -> None:
    """Sent when an order is paid. Retried for 24 hours until your endpoint answers 2xx."""
```

## Testing calls to other services

`httpx.MockTransport` answers requests from a function in the test, with no network and no extra library; override
the client dependency so the app uses it.

```python
def payments_stub(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v2/customers/cus_1":
        return httpx.Response(200, json={"id": "cus_1", "status": "active"})
    return httpx.Response(503)


@pytest.fixture
async def payments() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.MockTransport(payments_stub)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.payments.example.com/v2") as c:
        app.dependency_overrides[get_payments] = lambda: c
        yield c
    app.dependency_overrides.pop(get_payments, None)
```

Write one test per failure branch (timeout, 429, 503, a malformed body). For a webhook endpoint, sign a fixture body
with the test secret and post it as `content=` (raw bytes), plus one test with a bad signature and one that sends the
same event twice.

## Verify outbound calls and webhooks

Run one test per failure branch, plus the attack cases for a webhook: a bad signature, the same event twice, and a
body changed after signing.

```python
@pytest.mark.parametrize(
    ("header", "expected"),
    [("7", 7.0), ("600", 10.0), ("Wed, 21 Oct 2020 07:28:00 GMT", 0.0), ("soon", 1.0), ("nan", 1.0), (None, 1.0)],
)
def test_retry_after(header: str | None, expected: float) -> None:
    assert retry_after_seconds(header) == expected


async def test_bad_signature_is_refused(client: httpx.AsyncClient) -> None:
    headers = {"X-Hub-Signature-256": "sha256=" + "0" * 64, "X-GitHub-Delivery": "1"}
    response = await client.post("/webhooks/github", content=b'{"action":"opened"}', headers=headers)
    assert response.status_code == 401
```

```bash
uv run pytest -k "upstream or webhook or retry"
grep -rn 'httpx.AsyncClient(' app/ | grep -v lifespan           # expected: only the lifespan module
grep -rnE 'httpx\.(get|post)\(|requests\.' app/                 # expected: nothing
```

## External API anti-patterns

Each of these works in a demo and fails on the first bad day of the service you call.

| Do not | Do instead |
|---|---|
| `async with httpx.AsyncClient()` in an endpoint | one client per upstream, opened in the lifespan |
| `float(response.headers["Retry-After"])` | parse seconds and HTTP dates, and cap the wait |
| retry a plain `POST` | send an idempotency key, or do not retry |
| return the upstream's error body | map it to your own 502, 503 or 504 |
| hold a transaction during the call | commit, call, then write the result in a new transaction |
| `signature == expected` | `hmac.compare_digest` on the raw body |
| process a webhook inside the request | store, enqueue, answer 2xx |

## External API checklist

Go through this external API checklist before merging a change.

```text
[ ] one AsyncClient per upstream, opened in the lifespan, closed on shutdown; key from settings
[ ] explicit timeouts; a total budget with asyncio.timeout where an endpoint makes several calls
[ ] retries only for reads or keyed calls; few attempts in a request; 429 honours Retry-After
[ ] upstream failures mapped to 502/503/504 with your error body; upstream responses validated
[ ] no database transaction open during an outbound call
[ ] POST endpoints that create money or orders accept an Idempotency-Key
[ ] webhooks verified on the raw body with compare_digest; duplicates dropped by event id; work enqueued
[ ] outgoing webhooks signed, sent from a queue, retried, documented with app.webhooks
[ ] tests use MockTransport for every failure branch
```

## Common external API problems

These are the external API problems people hit most, each with its symptom, cause and fix.

1. **Requests pile up when a partner API is slow.** No timeout, or one far longer than the endpoint can afford. Set
   `httpx.Timeout` and an `asyncio.timeout` budget.
2. **Customers are charged twice.** A POST was retried after a timeout without an idempotency key. Send a key
   upstream and accept one from your own clients.
3. **Webhook signature check always fails.** The code verified `json.dumps(body)` instead of the raw bytes, or a
   middleware decoded and re-encoded the body. Verify `await request.body()`.
4. **The same webhook is processed several times.** Providers deliver at least once. Store the event id under a
   unique constraint before processing.
5. **`PoolTimeout` from httpx under load.** More concurrent calls than `max_connections`, or responses opened with
   `client.stream()` and never closed. Raise the limit or close streams with `async with`.
6. **Every call to a fast partner API costs a new TLS handshake.** `async with httpx.AsyncClient()` inside the endpoint
   reconnects and renegotiates TLS every time. Share one client from the lifespan.

## See also

- fastapi-background-tasks: queues for work that must finish.
- fastapi-security: SSRF rules for user-supplied URLs.
- fastapi-errors-observability: your own error body.
- fastapi-testing: fixtures that override dependencies.
- fastapi-performance: running independent calls together.
