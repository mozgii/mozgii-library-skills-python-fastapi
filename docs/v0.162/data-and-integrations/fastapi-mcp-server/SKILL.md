---
name: fastapi-mcp-server
description: "Lets AI agents use a FastAPI backend through the Model Context Protocol (MCP) - hand-written FastMCP 4 tools over the service layer, the MCP endpoint mounted in the FastAPI app with a combined lifespan, JWT auth and scopes for agents, tool annotations, FastMCP.from_fastapi with route maps for a prototype, and in-memory tool tests. Use when adding MCP to a FastAPI app, exposing endpoints as tools for Claude or another agent, or fixing an MCP endpoint that returns 404 or \"StreamableHTTPSessionManager task group was not initialized\"."
metadata:
  tags:
  - fastapi
  - mcp
  - model-context-protocol
  - fastmcp
  - ai-agents
  - tools
  - llm
---

# FastAPI as an MCP server for AI agents

This skill gives AI agents a safe, useful way into a FastAPI backend: a small set of MCP tools that call the same
services as the HTTP routes, served from the same process under `/mcp`. Checked on 2026-10-28 with FastMCP 4.1.4
(Apache-2.0) and FastAPI 0.162. MCP is the open protocol agents such as Claude use to discover and call tools.

## Step 0: installed versions for MCP

FastMCP moves fast, so read what is installed first. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastmcp|mcp|fastapi|httpx2) '
```

This skill targets FastMCP 4. On an older major, check `fastmcp.utilities.lifespan.combine_lifespans` and
`fastmcp.server.providers.openapi` before copying the imports.

## Hand-written tools or a converted API

An agent works best with a few tools named after tasks ("find an order by email"), with short descriptions and
narrow inputs; a REST API converted one-to-one gives it dozens of CRUD endpoints and pagination parameters. FastMCP's
own documentation says the same: conversion is for bootstrapping, curated tools perform better.

| Situation | Do |
|---|---|
| production use by agents, any write operation, or an API with more than a handful of routes | write MCP tools by hand over the service layer (default) |
| a quick prototype, an internal read-only API, or a demo | `FastMCP.from_fastapi(app)` with route maps that exclude everything risky |

## Install FastMCP

FastMCP builds MCP servers in Python with decorators and type hints; it does not install FastAPI for you.

```bash
uv add fastmcp
```

FastMCP 4 is a large dependency (it brings the MCP SDK and `httpx2`). Keep MCP code in its own module, `app/mcp.py`,
so the rest of the app does not import it.

## Write tools over the service layer

A tool is an `async def` with typed parameters and a docstring; FastMCP turns the signature into the input schema
and a Pydantic return type into structured output. FastAPI's `Depends()` does not run for MCP tools, so open the
database session yourself and call the service, exactly as a background job would.

```python
# app/mcp.py
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from app.db import SessionFactory
from app.orders import service as orders_service
from app.orders.schemas import OrderRead

mcp = FastMCP("Orders")


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def find_orders_by_email(
    email: Annotated[str, Field(description="Customer email address, exact match", max_length=320)],
    limit: Annotated[int, Field(ge=1, le=20)] = 5,
) -> list[OrderRead]:
    """Find a customer's most recent orders by email. Use before refunding or answering a question about an order."""
    async with SessionFactory() as session:
        orders = await orders_service.recent_by_email(session, email, limit)
    return [OrderRead.model_validate(o) for o in orders]


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
async def refund_order(order_id: int, reason: Annotated[str, Field(max_length=200)]) -> OrderRead:
    """Refund a paid order in full. Ask the user to confirm before calling."""
    async with SessionFactory() as session:
        try:
            order = await orders_service.refund(session, order_id, reason)
        except orders_service.OrderNotRefundable as exc:
            raise ToolError(f"Order {order_id} cannot be refunded: {exc.reason}") from None
    return OrderRead.model_validate(order)
```

- The docstring is the tool's description: say what it does and when to use it, in one or two sentences.
- Bound every input (`max_length`, `le`) and every result (`limit`); an agent will ask for everything if it can.
- Raise `ToolError` with a sentence the agent can act on; other exceptions reach it as a generic error.
- Set the four annotations honestly (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`): clients
  use them to decide when to ask the user for approval.
- A plain `def` tool runs in a thread (`run_in_thread=True` is the default); an `async def` tool must not block.

## Mount the MCP endpoint in the FastAPI app

`mcp.http_app()` returns an ASGI app speaking MCP's Streamable HTTP transport; mount it in FastAPI and combine its
lifespan with yours, because a mounted app's lifespan never runs on its own and the MCP session manager would not
start.

```python
# app/main.py
from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans

from app.lifespan import app_lifespan             # your own lifespan: pools, clients
from app.mcp import mcp

mcp_app = mcp.http_app(path="/")                   # mounted at /mcp below, so the endpoint is /mcp/
app = FastAPI(lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan))
app.mount("/mcp", mcp_app)
```

- `http_app()` defaults to the path `/mcp`; mounted at `/mcp` that becomes `/mcp/mcp`. Pass `path="/"` when you
  mount under a prefix.
- The real URL ends in a slash: `http://127.0.0.1:8000/mcp/`. A `POST /mcp` answers `307` with `Location: .../mcp/`,
  so configure clients with `/mcp/`.
- `combine_lifespans` enters the lifespans in order and exits in reverse; the dicts they yield are merged into the
  request state.
- With several Uvicorn workers, MCP sessions live in one process. Use `mcp.http_app(path="/", stateless_http=True)`
  so any worker can answer any request, unless your tools need server-side session state.
- Keep app-wide `CORSMiddleware` away from an OAuth-protected MCP mount; FastMCP's documentation recommends separate
  sub-apps for your own CORS needs.

## Authenticate agents

An MCP endpoint is an API like any other: require a token, check scopes for writes, and know which user a call
acts for. FastMCP verifies bearer tokens itself; `JWTVerifier` checks tokens from your identity provider.

```python
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

auth = JWTVerifier(
    jwks_uri="https://auth.example.com/.well-known/jwks.json",
    issuer="https://auth.example.com/",
    audience="orders-mcp",
)
mcp = FastMCP("Orders", auth=auth)


@mcp.tool(annotations={"destructiveHint": True})
async def cancel_order(order_id: int) -> OrderRead:
    """Cancel an order that has not shipped."""
    token = get_access_token()
    if token is None or "orders:write" not in token.scopes:
        raise ToolError("This tool needs the orders:write scope")
    user_id = int(token.claims["sub"])
    async with SessionFactory() as session:
        order = await orders_service.cancel(session, order_id, acting_user=user_id)
    return OrderRead.model_validate(order)
```

- Scope every query by the token's user, exactly as the HTTP routes do; an agent acting for one user must not reach
  another user's data.
- Leave admin operations out of the MCP server entirely rather than guarding them with a flag.
- Never put secrets or tokens in tool results; agents may repeat results to the user or keep them in logs.

## Convert an existing API for a prototype

`FastMCP.from_fastapi(app)` reads the app's OpenAPI schema and calls the routes in-process, so every route becomes
a tool named after its `operation_id`. Exclude what agents must not call and give the rest clear operation ids
(see also fastapi-openapi).

```python
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

from app.main import app

mcp = FastMCP.from_fastapi(
    app=app,
    route_maps=[
        RouteMap(pattern=r"^/admin/.*", mcp_type=MCPType.EXCLUDE),
        RouteMap(methods=["POST", "PUT", "PATCH", "DELETE"], mcp_type=MCPType.EXCLUDE),   # read-only prototype
        RouteMap(methods=["GET"], pattern=r".*\{.*\}.*", mcp_type=MCPType.RESOURCE_TEMPLATE),
    ],
    httpx_client_kwargs={"headers": {"Authorization": f"Bearer {settings.mcp_service_token.get_secret_value()}"}},
)
```

- Route maps are checked in order; unmatched routes become tools.
- Without `operation_id`s the tool names are `list_items_items_get`; set `generate_unique_id_function` first.
- The converted server calls your routes with the headers you give it, so it acts as one fixed identity: fine for a
  demo, wrong for a multi-user product.

## Test tools in memory

FastMCP's `Client` connects to a server object directly, with no network, so tool tests are as fast as unit tests
and run against the real database fixtures.

```python
from fastmcp import Client

from app.mcp import mcp


async def test_find_orders(seed_orders: None) -> None:
    async with Client(mcp) as client:
        tools = {t.name for t in await client.list_tools()}
        assert "find_orders_by_email" in tools
        result = await client.call_tool("find_orders_by_email", {"email": "ana@example.com"})
    assert result.structured_content is not None
```

Also try the server with a real agent: point Claude Code or another MCP client at `http://127.0.0.1:8000/mcp/` and
ask for the tasks the tools were written for; vague descriptions show up at once as the wrong tool being picked.

## Verify the MCP endpoint

Check the mounted endpoint over HTTP as a client would, then the tools in memory. Start the app with
`uv run fastapi dev` first.

```bash
BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}'
HEADERS=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
curl -s -o /dev/null -w '%{http_code}\n' "${HEADERS[@]}" -d "$BODY" http://127.0.0.1:8000/mcp/   # 200, or 401 with auth on
curl -s -o /dev/null -w '%{http_code}\n' "${HEADERS[@]}" -d "$BODY" http://127.0.0.1:8000/mcp    # 307 to /mcp/
uv run pytest -k "mcp or tools"
```

With auth on, also test the attack cases: a call with no token answers 401, a token from another issuer answers 401,
and a read-only token calling a write tool gets the `ToolError`.

## MCP server anti-patterns

Each of these gives an agent more than it should have, or less than it needs.

| Do not | Do instead |
|---|---|
| convert a whole API with `from_fastapi` for production | hand-written tools named after tasks |
| `Depends()` in a tool | open the session in the tool and call the service |
| tools without limits on inputs and results | `max_length`, `le` and a `limit` |
| admin tools guarded by a flag | leave them out of the server |
| one token shared by every agent user | scope each query by the token's user |
| configure clients with `/mcp` | use `/mcp/`, the real URL |

## MCP server checklist

Go through this MCP server checklist before merging a change.

```text
[ ] tools written by hand over the service layer; converted routes only in prototypes
[ ] each tool named after a task, described in one or two sentences, inputs and results bounded
[ ] annotations set honestly; destructive tools say to confirm with the user
[ ] errors raised as ToolError with an actionable sentence
[ ] http_app(path="/") mounted at /mcp; lifespans combined; stateless_http with several workers
[ ] token verification on; scopes checked for writes; every query scoped by the token's user
[ ] no admin tools, no secrets in results
[ ] tools tested in memory with Client and tried once with a real agent
```

## Common MCP server problems

These are the MCP server problems people hit most, each with its symptom, cause and fix.

1. **`404` at `/mcp/`.** The app was mounted at `/mcp` with the default path, so the endpoint is `/mcp/mcp`. Use
   `http_app(path="/")`.
2. **`RuntimeError: FastMCP's StreamableHTTPSessionManager task group was not initialized`.** The MCP app's lifespan
   was not passed to FastAPI. Use `combine_lifespans`.
3. **`Depends()` parameters show up as tool inputs or fail.** FastAPI dependency injection does not run for MCP
   tools. Open sessions and load users inside the tool.
4. **The agent picks the wrong tool or loops.** Too many similar tools, or descriptions that say how instead of
   when. Merge tools and rewrite descriptions as tasks.
5. **Sessions are lost between calls with several workers.** Stateful sessions live in one process. Turn on
   `stateless_http=True`, or route a session to one worker.

## See also

- fastapi-repository-pattern: the service layer tools call.
- fastapi-security: tokens and scopes.
- fastapi-openapi: operation ids for converted tools.
- fastapi-middleware: mounted apps and lifespans.
- fastapi-deployment: several workers and stateless mode.
