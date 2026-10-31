# FastAPI skills

A library of Claude skills for building backends with FastAPI: project structure, routing, Pydantic models,
dependencies, middleware, async SQLAlchemy and SQLModel, Alembic migrations, authentication and security, background
jobs, external APIs and webhooks, WebSockets and streaming, MCP servers for AI agents, testing on real PostgreSQL,
errors and observability, OpenAPI, deployment, performance and version upgrades.

Every skill targets the versions current on 2026-10-31 (FastAPI 0.162, Starlette 1.10, Pydantic 2.14, SQLAlchemy
2.1, Alembic 1.20, pytest 9.2, pytest-asyncio 1.4, httpx 0.28, Uvicorn 0.54, uv 0.13, FastMCP 4.1) and calls out the
breaking changes between them and older tutorials. The code in the testing, database, migration and deployment
skills was run against PostgreSQL 18 before publishing.

## Use these skills

Each skill is a folder under `docs/v0.162/<group>/` that works on its own; take one, several or all of them.

- **Claude Code, for all your projects:** copy the folder to `~/.claude/skills/`, for example
  `cp -r docs/v0.162/production/fastapi-testing ~/.claude/skills/`.
- **Claude Code, for one project:** copy it to the project's `.claude/skills/` and commit it, so everyone on the
  team gets it.
- **claude.ai and Claude Desktop:** zip the folder (the zip holds one folder named like the skill, with `SKILL.md`
  inside) and upload it in the Skills section of claude.ai's settings.

Claude reads each skill's `description` and loads the skill when a request matches; you can also call one by name,
such as `/fastapi-testing`. The front matter uses only the portable fields (`name`, `description`, `metadata`), so one
folder runs unchanged on claude.ai, through the API and under Claude Code. Skills mention each other as "see also" only; none needs
another to be installed.

## Skills

Each skill is `docs/v0.162/<group>/<name>/SKILL.md`, with longer material in `references/` and helper scripts in `scripts/`.

| Skill | What it covers |
|---|---|
| `fastapi-project-structure` | uv project, `fastapi dev`/`run`, entrypoint, layout by domain, APIRouter, lifespan, pydantic-settings |
| `fastapi-code-standards` | strict typing, thin handlers, no blocking calls in async code, safe SQL, error and secret rules, the pre-commit gate |
| `fastapi-routing` | path, query, header, cookie parameters, parameter models, bodies, forms and uploads, responses, HTML templates |
| `fastapi-pydantic-models` | Create/Update/Read schemas, constraints, validators, PATCH, camelCase aliases, discriminated unions |
| `fastapi-dependencies` | `Depends` with `Annotated`, yield dependencies and scope, chains, caching, resource loaders, overrides |
| `fastapi-middleware` | middleware order, pure ASGI middleware, built-in middleware, custom route classes, mounting static files and WSGI apps |
| `fastapi-sqlalchemy-async` | async engine and sessions, typed models, eager loading, transactions, lost updates, pagination, upserts, SQLModel |
| `fastapi-repository-pattern` | service and repository layers, transaction ownership, scope guards, domain exceptions |
| `fastapi-alembic-migrations` | async env.py, autogenerate review, immutable history, data migrations, zero-downtime changes, CI checks |
| `fastapi-security` | pwdlib/Argon2, OAuth2 password flow, JWT, current user, scopes, API keys, password reset, 401/403/404, hardening |
| `fastapi-background-tasks` | BackgroundTasks, task queues (Taskiq, Celery, arq in existing apps), 202 + job status, schedules, threads and processes |
| `fastapi-external-apis` | shared httpx client, timeouts, safe retries, upstream errors, idempotency keys, incoming and outgoing webhooks |
| `fastapi-websockets-streaming` | SSE, JSON Lines, byte streams, WebSockets with auth and broadcasting across workers |
| `fastapi-mcp-server` | MCP tools for AI agents over the service layer, mounting under `/mcp`, agent auth, converting an API, tests |
| `fastapi-testing` | pytest-asyncio auto mode, ASGITransport, testcontainers, migrations in tests, savepoint rollback, schema fuzzing |
| `fastapi-errors-observability` | problem-details errors, handlers, JSON logs with request ids, native OpenTelemetry, Sentry, health checks |
| `fastapi-openapi` | metadata, operation ids, documented errors, schema export and diff in CI, SDK generation, versioning |
| `fastapi-deployment` | workers, proxies and root path, uv Docker image, FastAPI Cloud, migrations at release, graceful shutdown, Kubernetes |
| `fastapi-performance` | load tests, profiling, event-loop blocking, thread pool, serialization, database, caching, middleware |
| `fastapi-version-upgrades` | pinning, the upgrade procedure, breaking changes by symptom across FastAPI, Starlette, Pydantic, SQLAlchemy, tests |

Scripts: `fastapi-code-standards/scripts/find_blocking_calls.py` (blocking calls in `async def`),
`fastapi-alembic-migrations/scripts/check_migrations.sh` (edited revisions, heads, round trip, `alembic check`),
`fastapi-openapi/scripts/export_openapi.py` (write or check `openapi.json`).

## Repository layout

- `docs/v0.162/<group>/`: the skills. Each `SKILL.md` has portable front matter; each reference file has `title`, `author`
  and `lang`.
- `library.json`: the catalog entry (name `python-fastapi`, title, version).
- `questions.json`: developer questions, each naming the phrase and the file that answers it; used to check that a
  search over the skills finds the right passage.
- `versions.json`: every package the skills name, the version they target and the date it was read from its registry.

## Sources

Every text and script in this library is the own work of MOZGII, Inc. The sources below were read as context: they
showed what the skills should cover and which facts are true, and each fact was checked against the versions above.
No wording, example or code was copied from any of them, whatever its licence, so no third-party notice applies. They
are listed as thanks and as a map for the next upgrade; `sources.json` records the exact commit of every GitHub source
that was read. Licences are as each project published them when it was read.

| Source | Licence | Used for |
|---|---|---|
| [FastAPI documentation and release notes](https://fastapi.tiangolo.com) ([repo](https://github.com/fastapi/fastapi)) | MIT | read as context for current APIs and every version-specific change cited (0.100–0.162), upgrade and version-pinning advice |
| [FastAPI's official agent skill](https://github.com/fastapi/fastapi/tree/master/fastapi/.agents/skills/fastapi) | MIT | read as context: `Annotated` everywhere, no Ellipsis or `RootModel`, return types over `response_model`, router-level parameters, function dependencies over class dependencies, SSE, JSON Lines and byte streaming patterns, `app.frontend()`, OpenTelemetry settings; written in our own words with our own examples |
| [Full-stack FastAPI template](https://github.com/fastapi/full-stack-fastapi-template) | MIT | read as context for layer layout, pwdlib with Argon2 and bcrypt, PyJWT, pre-start migrations, tooling choices |
| [Starlette release notes](https://github.com/Kludex/starlette) | BSD-3-Clause | read as context for 1.0 removals (`on_event`, `on_startup`, the old `TemplateResponse` form), middleware order, WebSocket and TestClient changes |
| [Pydantic](https://docs.pydantic.dev), [pydantic-settings](https://github.com/pydantic/pydantic-settings) | MIT | read as context for model configuration, validators, aliases, settings sources |
| [SQLAlchemy 2.1 docs and migration notes](https://docs.sqlalchemy.org/en/21/) | MIT | read as context for asyncio extension, greenlet extra, loading strategies, 2.1 behaviour changes |
| [Alembic](https://alembic.sqlalchemy.org) | MIT | read as context for async templates, autogenerate limits, `autocommit_block` |
| [SQLModel](https://sqlmodel.tiangolo.com) | MIT | read as context for table and data models, async session, `UTCDateTime` timestamps |
| [Asyncer](https://github.com/fastapi/asyncer) | MIT | read as context for `asyncify` with a `CapacityLimiter`, on AnyIO's worker threads |
| [httpx](https://github.com/encode/httpx), [httpx2](https://github.com/pydantic/httpx2) | BSD-3-Clause | read as context for `ASGITransport`, removal of `app=`, TestClient's move to httpx2 |
| [pytest](https://docs.pytest.org) and [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | MIT, Apache-2.0 | read as context for auto mode, loop scopes, 1.0 and 1.4 changes, async test failures in pytest 8.4+ |
| [testcontainers-python](https://github.com/testcontainers/testcontainers-python) | Apache-2.0 | read as context for `PostgresContainer(driver=...)`, the `community` module path |
| [Uvicorn](https://github.com/Kludex/uvicorn), [uvicorn-worker](https://github.com/Kludex/uvicorn-worker) | BSD-3-Clause | read as context for workers, proxy headers, graceful shutdown, Gunicorn worker class |
| [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/) | Apache-2.0 / MIT | read as context for multi-stage image, cache and bind mounts, `UV_*` settings |
| [PyJWT](https://github.com/jpadilla/pyjwt), [pwdlib](https://github.com/frankie567/pwdlib) | MIT | read as context for token handling, key-length warning, password hashing |
| [arq](https://github.com/python-arq/arq), [Taskiq](https://github.com/taskiq-python/taskiq), [taskiq-redis](https://github.com/taskiq-python/taskiq-redis), [slowapi](https://github.com/laurentS/slowapi), [asgi-lifespan](https://github.com/florimondmanca/asgi-lifespan), [Locust](https://locust.io), [py-spy](https://github.com/benfred/py-spy) | MIT | read as context for queue status, broker and retry settings, rate-limit, lifespan-in-tests, load-test and profiling facts |
| [pyinstrument](https://github.com/joerick/pyinstrument) | BSD-3-Clause | read as context for profiling middleware |
| [Celery](https://github.com/celery/celery) | BSD-3-Clause | read as context for sync tasks and beat schedules |
| [asyncpg](https://github.com/MagicStack/asyncpg) | Apache-2.0 | read as context for statement caches and error classes |
| [sentry-python](https://github.com/getsentry/sentry-python), [Polyfactory](https://github.com/litestar-org/polyfactory) | MIT | read as context for the FastAPI integration, how logged errors reach Sentry, and test factories |
| [structlog](https://github.com/hynek/structlog) | Apache-2.0 / MIT | read as context for processors and redaction |
| [FastMCP](https://github.com/PrefectHQ/fastmcp) and [gofastmcp.com](https://gofastmcp.com/integrations/fastapi) | Apache-2.0 | read as context for `FastMCP.from_fastapi`, route maps, `http_app()`, `combine_lifespans`, `JWTVerifier`, the in-memory `Client`; the advice to prefer curated tools |
| [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MIT | read as context for `AccessToken` fields, the session manager's startup error |
| [stamina](https://github.com/hynek/stamina) | MIT | read as context for retry decorator and backoff hooks |
| [Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks) | Apache-2.0 (spec), MIT (Python library) | read as context for webhook signature verification |
| [a2wsgi](https://github.com/abersheeran/a2wsgi) | Apache-2.0 | read as context for mounting WSGI apps |
| [Schemathesis](https://github.com/schemathesis/schemathesis) | MIT | read as context for property-based tests from the OpenAPI schema |
| [fastapi-cli and FastAPI Cloud docs](https://fastapi.tiangolo.com/deployment/fastapicloud/) | MIT | read as context for `fastapi deploy` |
| [oasdiff](https://github.com/oasdiff/oasdiff), [OpenAPI Generator](https://openapi-generator.tech) | Apache-2.0 | read as context for breaking-change checks, SDK generation |
| [Hey API openapi-ts](https://heyapi.dev) | MIT | read as context for TypeScript SDK generation |
| [jeffallan/claude-skills `fastapi-expert` skill](https://github.com/jeffallan/claude-skills) | MIT | read as context for coverage |
| [wshobson/agents `fastapi-templates` skill](https://github.com/wshobson/agents/tree/main/plugins/api-scaffolding/skills/fastapi-templates) | MIT | read as context for coverage |
| [awesome-cursorrules FastAPI rules](https://github.com/PatrickJS/awesome-cursorrules/tree/main/rules) | CC0-1.0 | read as context for coverage |
| [awesome-fastapi](https://github.com/mjhea0/awesome-fastapi) | CC0-1.0 | read as context for finding tools and libraries |
| [Anthropic skills](https://github.com/anthropics/skills) and the Claude skill documentation | see repository | read as context for skill format and description rules; no FastAPI skill there |
| [zhanymkanov/fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices) | no licence | read as context: domain layout, chained validation dependencies, naming conventions are described in our own words |
| [Kludex/fastapi-tips](https://github.com/Kludex/fastapi-tips) | no licence | read as context: pure ASGI middleware, lifespan state, `async for` on WebSockets, asyncio debug mode |

Three skills started from earlier in-house rules (backend standards, repository pattern, Python testing) and were
rewritten for general use, with outdated statements corrected against the sources above. Provider documentation
(Stripe, GitHub webhooks) was read as context and linked.
