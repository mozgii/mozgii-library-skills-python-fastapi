---
name: fastapi-websockets-streaming
description: Builds real-time and streaming endpoints in FastAPI - WebSocket endpoints with authentication, disconnect handling and a connection manager, broadcasting across workers with Redis pub/sub, Server-Sent Events with EventSourceResponse and ServerSentEvent, JSON Lines streaming with yield, binary streaming with StreamingResponse, and testing all of them. Use when adding chat, live updates, notifications, progress bars, streaming LLM tokens, large exports, or when choosing between WebSockets and SSE.
metadata:
  tags:
  - fastapi
  - websockets
  - sse
  - server-sent-events
  - streaming
  - jsonl
  - real-time
---

# FastAPI WebSockets and streaming responses

This skill picks the right real-time transport and writes it with FastAPI 0.162's built-in support: SSE since 0.135,
JSON Lines since 0.134, security dependencies on WebSockets since 0.144.

## Step 0: installed versions for streaming

Read the versions first, since most of this API is recent. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastapi|starlette|websockets|uvicorn|redis) '
```

`fastapi.sse` needs FastAPI 0.135 and JSON Lines needs 0.134. Without the `websockets` package, which
`uvicorn[standard]` brings, Uvicorn refuses WebSocket upgrades.

## Choose the transport

The direction of the data decides the transport.

| Need | Use |
|---|---|
| server pushes updates, client only listens (notifications, progress, LLM tokens) | Server-Sent Events: plain HTTP, automatic reconnect, works through most proxies |
| many records for a program to process line by line (exports, logs) | JSON Lines (`application/jsonl`) |
| bytes (files, media, generated archives) | `StreamingResponse` subclass |
| both sides send messages at any time (chat, collaborative editing, games) | WebSockets |

Prefer SSE when the client does not need to send messages over the same connection; it is simpler to secure, cache
headers and load-balance.

## Server-Sent Events

Declare `response_class=EventSourceResponse` and `yield` items; plain values and Pydantic models are JSON-encoded as
`data:` lines. FastAPI sets `Cache-Control: no-cache` and `X-Accel-Buffering: no` and sends a `: ping` comment every
15 seconds when the generator is idle.

```python
from collections.abc import AsyncIterable

from fastapi.sse import EventSourceResponse, ServerSentEvent


class Progress(BaseModel):
    job_id: str
    percent: int


@router.get("/jobs/{job_id}/events", response_class=EventSourceResponse)
async def job_events(job_id: str, session: SessionDep, user: CurrentUser) -> AsyncIterable[ServerSentEvent]:
    await session.commit()                     # CurrentUser's lookup holds a connection; give it back before the stream
    async for update in jobs_service.watch(job_id, user.id):
        yield ServerSentEvent(data=Progress(job_id=job_id, percent=update.percent), event="progress", id=str(update.seq))
    yield ServerSentEvent(data={"job_id": job_id}, event="done")
```

Output on the wire (verified):

```text
event: progress
data: {"job_id":"42","percent":50}
id: 7

```

- Yield `ServerSentEvent` for `event`, `id`, `retry` or `comment`; use `raw_data=` for text that must not be
  JSON-encoded.
- `EventSourceResponse` works with any method, including `POST` (as MCP and LLM streaming APIs use).
- A reconnecting browser sends `Last-Event-ID`; read it with `last_event_id: Annotated[str | None, Header()] = None`
  and resume from there.
- When the client disconnects, the generator is cancelled; release resources in `try/finally`.

## Streaming LLM tokens with SSE

Streaming model output token by token is the most common SSE use; send each chunk as an event and a final event with
usage data.

```python
@router.post("/chat", response_class=EventSourceResponse)
async def chat(body: ChatRequest, session: SessionDep, user: CurrentUser) -> AsyncIterable[ServerSentEvent]:
    await session.commit()                     # same reason: nothing below needs the database
    try:
        async for chunk in llm_client.stream(body.messages):
            yield ServerSentEvent(data={"delta": chunk.text}, event="delta")
        yield ServerSentEvent(data={"finish_reason": "stop"}, event="end")
    finally:
        await llm_client.aclose_stream()
```

The request's database session (default yield scope) stays open while the stream runs; do not hold a transaction
open for the whole stream. Read what you need first, commit, then stream. `CurrentUser` counts as a read: `session`
here is the same session its lookup used, so the commit above ends that transaction. On a pool of 2, four streams
without it gave 200, 200, 500, 500; with it all four gave 200.

## JSON Lines streaming

An endpoint that `yield`s Pydantic models with an `AsyncIterable[Model]` return type streams JSON Lines
(`application/jsonl`), one object per line, validated and serialized per item.

```python
@router.get("/items/export")
async def export_items(session: SessionDep, user: CurrentUser) -> AsyncIterable[ItemRead]:
    stream = await session.stream_scalars(select(Item).where(Item.owner_id == user.id).order_by(Item.id))
    async for item in stream:
        yield ItemRead.model_validate(item)
```

`session.stream_scalars()` uses a server-side cursor, so memory stays flat for millions of rows.

## Binary streaming

For bytes, declare a `StreamingResponse` subclass as `response_class` and `yield` chunks; the FastAPI maintainers
recommend it over building a `StreamingResponse` by hand and returning it.

```python
from fastapi.responses import StreamingResponse


class CSVStream(StreamingResponse):
    media_type = "text/csv"


@router.get("/items/export.csv", response_class=CSVStream)
async def export_csv(session: SessionDep, user: CurrentUser) -> AsyncIterable[bytes]:
    yield b"id,title\n"
    async for item in await session.stream_scalars(select(Item).where(Item.owner_id == user.id)):
        yield f"{item.id},{csv_escape(item.title)}\n".encode()
```

## WebSocket endpoints

A WebSocket endpoint accepts the connection, loops over messages and treats a disconnect as normal. Security
dependencies (`APIKeyHeader`, `OAuth2PasswordBearer`) work here since FastAPI 0.144.

```python
from fastapi import WebSocket, WebSocketDisconnect, WebSocketException, status
from pydantic import ValidationError


@router.websocket("/ws/rooms/{room_id}")
async def room(websocket: WebSocket, room_id: str, user: Annotated[User, Depends(get_ws_user)]) -> None:
    if not await rooms_service.can_join(user.id, room_id):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    await manager.connect(room_id, websocket)
    try:
        async for text in websocket.iter_text():
            try:
                message = ChatMessage.model_validate_json(text)    # validate every incoming message
            except ValidationError:
                await websocket.send_json({"error": "invalid message"})
                continue
            await manager.broadcast(room_id, message.model_dump_json())
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(room_id, websocket)
```

- `async for ... in websocket.iter_text()` ends cleanly on disconnect; a `while True: receive_text()` loop needs the
  `WebSocketDisconnect` handler.
- Validate each message with Pydantic; a WebSocket is input like any request body. Catch `ValidationError` inside the
  loop: left uncaught, one bad frame ends the handler with an ASGI traceback and the client gets no close frame.
- Since Starlette 1.7, sending on a closed socket raises `WebSocketDisconnected` (a `RuntimeError` subclass); the
  connection manager below drops such sockets.

## WebSocket authentication for browsers

Browsers cannot send an `Authorization` header with the WebSocket handshake, so browser clients authenticate with a
cookie or a short-lived ticket.

```python
async def get_ws_user(websocket: WebSocket, ticket: str | None = None) -> User:
    token = websocket.cookies.get("access_token") or (await tickets.redeem(ticket) if ticket else None)
    if token is None:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    async with SessionFactory() as session:                     # closed before the socket starts its loop
        try:
            user = await session.get(User, decode_access_token(token))
        except (jwt.InvalidTokenError, ValueError):             # ValueError: a `sub` that is not a number
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION) from None
    if user is None or not user.is_active:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    return user
```

The lookup uses its own short session on purpose. A `SessionDep` here lives as long as the socket, so every open
socket stays `idle in transaction` and holds a pool connection; on a pool of 2 the third socket fails with HTTP 500.
This session comes from the global factory, so tests override the factory as well as `get_session`.

A ticket is a random single-use value valid for about 30 seconds, issued by an authenticated HTTP endpoint; it is
safe in a query string because it expires before logs are read. Also check the `Origin` header against your allowed
origins: cookies make cross-site WebSocket hijacking possible.

## Connection manager and broadcasting across workers

A connection manager tracks sockets per room in one process. With several workers or machines, a client on worker A
never sees a message sent on worker B, so publish through Redis and let each worker deliver to its own sockets.

```python
import asyncio
from collections import defaultdict


class ConnectionManager:
    def __init__(self) -> None:
        self.rooms: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, room: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.rooms[room].add(websocket)

    def disconnect(self, room: str, websocket: WebSocket) -> None:
        self.rooms[room].discard(websocket)

    async def send_local(self, room: str, text: str) -> None:
        sockets = list(self.rooms[room])
        results = await asyncio.gather(*(ws.send_text(text) for ws in sockets), return_exceptions=True)
        for ws, result in zip(sockets, results, strict=True):
            if isinstance(result, Exception):
                self.disconnect(room, ws)

    async def broadcast(self, room: str, text: str) -> None:
        await redis.publish(f"room:{room}", text)          # every worker's subscriber calls send_local
```

Start one subscriber task per worker in the lifespan that reads `redis.pubsub()` for `room:*` and calls `send_local`.
Use `gather` with `return_exceptions=True` so one slow or dead client cannot stop the broadcast.

## Testing streams and WebSockets

`TestClient` drives WebSockets; streaming endpoints can be read in full or line by line.

```python
from fastapi.testclient import TestClient


def test_room_echo(app_with_overrides: FastAPI) -> None:
    client = TestClient(app_with_overrides)
    client.cookies.set("access_token", token_for(user))      # on the client: cookies= on one call is deprecated
    with client.websocket_connect("/ws/rooms/1") as ws:
        ws.send_text('{"text": "hi"}')
        assert "hi" in ws.receive_text()


async def test_jsonl_export(client: httpx.AsyncClient) -> None:
    async with client.stream("GET", "/items/export", headers=auth) as response:
        lines = [line async for line in response.aiter_lines() if line]
    assert response.headers["content-type"].startswith("application/jsonl")
```

Since Starlette 1.9, `TestClient.stream()` returns before the app finishes, so infinite SSE streams can be tested by
reading a few events and closing.

## Verify streams and sockets

Check the bytes on the wire, then the failure paths: a client that leaves, and an origin that should be refused.

```bash
curl -N -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/jobs/42/events   # events appear one at a time
curl -s -D - -o /dev/null -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/items/export | grep -i '^content-type'
uv run pytest -k "stream or websocket"
```

The Origin check is a small dependency, and the test proves a cross-site page cannot open the socket.

```python
import pytest
from fastapi import WebSocket, WebSocketException, status
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ALLOWED_ORIGINS = {"https://app.example.com"}


async def check_origin(websocket: WebSocket) -> None:
    if websocket.headers.get("origin") not in ALLOWED_ORIGINS:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)


def test_room_refuses_a_foreign_origin(client: TestClient) -> None:      # needs check_origin on the route
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws/rooms/1", headers={"Origin": "https://evil.example"}):
            pass
    assert closed.value.code == 1008
```

A malformed frame gets an error reply, and the socket stays open for the next message.

```python
def test_bad_frame_keeps_the_socket(client: TestClient) -> None:
    with client.websocket_connect("/ws/rooms/1", headers={"Origin": "https://app.example.com"}) as ws:
        ws.send_text("not json")
        assert ws.receive_json() == {"error": "invalid message"}
        ws.send_text('{"text": "hi"}')
        assert "hi" in ws.receive_text()
```

Then open a stream, close it from the client, and confirm the `finally` block ran.

## WebSocket and streaming anti-patterns

Each of these works with one client on one worker.

| Do not | Do instead |
|---|---|
| a WebSocket where the client only listens | SSE |
| a `StreamingResponse` built by hand for SSE | `EventSourceResponse` with `yield` |
| hold a transaction open during a stream | read first, commit, then stream |
| a long-lived token in the WebSocket URL | a cookie or a short single-use ticket |
| trust a WebSocket message | validate each one with Pydantic |
| broadcast from one process when you run several | Redis pub/sub |
| `.all()` before streaming millions of rows | `session.stream_scalars()` |

## WebSocket and streaming checklist

Go through this WebSocket and streaming checklist before merging a change.

```text
[ ] transport chosen by direction: SSE for server push, WebSockets for two-way, JSONL for record streams
[ ] SSE endpoints use EventSourceResponse with yield; ids set when clients may resume
[ ] streams release resources in finally; no long transaction held during a stream
[ ] large exports read with stream_scalars, not .all()
[ ] WebSocket messages validated with Pydantic; disconnect handled; dead sockets removed
[ ] browser WebSockets authenticated with a cookie or a short-lived ticket; Origin checked
[ ] broadcasts across workers go through Redis pub/sub
[ ] proxies configured for long-lived connections (timeouts, Upgrade headers for WebSockets)
```

## Common WebSocket and streaming problems

These are the WebSocket and streaming problems people hit most, each with its symptom, cause and fix.

1. **SSE arrives all at once.** A proxy buffers the response. FastAPI sets `X-Accel-Buffering: no` for Nginx; other
   proxies can still buffer, so turn buffering off there. `GZipMiddleware` is not the cause: Starlette excludes
   `text/event-stream` from compression.
2. **WebSocket closes after 60 seconds behind Nginx.** Set `proxy_read_timeout 3600s;` and forward the `Upgrade` and
   `Connection` headers.
3. **Messages reach only some users.** Several workers, no pub/sub. Add Redis broadcasting.
4. **`RuntimeError` when sending.** The client is gone (`WebSocketDisconnected` in Starlette 1.7+). Remove the socket
   on any send error.
5. **A stream holds a database connection for minutes.** Each open stream keeps its session; with a pool of 15, the
   sixteenth stream waits. Load data, close the session, then stream from memory or a cursor you control.

## See also

- fastapi-security: tokens and hardening.
- fastapi-background-tasks: the jobs behind progress events.
- fastapi-deployment: proxy timeouts and shutdown.
- fastapi-sqlalchemy-async: sessions during long reads.
- fastapi-testing: `TestClient` for WebSocket tests.
