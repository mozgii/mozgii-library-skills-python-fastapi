---
name: fastapi-deployment
description: Deploys FastAPI to production - fastapi run and Uvicorn workers, how many processes per container or VM, Gunicorn with uvicorn-worker, running behind Nginx, Traefik or a cloud load balancer with proxy headers and root path, a multi-stage Docker image built with uv, migrations as a release step, configuration and secrets, graceful shutdown, health probes and Kubernetes settings. Use when shipping a FastAPI app, writing its Dockerfile, configuring workers, putting it behind a proxy, or debugging redirects to http, wrong client IPs or dropped requests during deploys.
metadata:
  tags:
  - fastapi
  - deployment
  - docker
  - uvicorn
  - gunicorn
  - kubernetes
  - nginx
  - uv
---

# FastAPI deployment

This skill runs a FastAPI app in production: the right number of Uvicorn processes, a small reproducible image, a
proxy that passes the client's scheme and address, migrations run once, and deploys that drop no requests. Verified
on 2026-10-28 with FastAPI 0.162, fastapi-cli 0.0.32, Uvicorn 0.54 and uv 0.13.6; the Dockerfile in
[references/docker.md](references/docker.md) was built and run.

## Step 0: installed versions for deployment

Read what the image will run. A missing line means the package is not installed.

```bash
uv run python --version
uv pip list | grep -iE '^(fastapi|fastapi-cli|uvicorn|uvloop|httptools|gunicorn|uvicorn-worker) '
uv --version
docker --version
```

`--root-path` replaces `FastAPI(root_path=...)` from 0.146. Pin the same uv and Python minor in the Dockerfile as here,
so the image builds what you tested.

## The server command

`fastapi run` starts Uvicorn in production mode: no reload, listening on `0.0.0.0:8000`, importing the app from
`[tool.fastapi] entrypoint`. `uvicorn` directly is equivalent and has more options.

```bash
fastapi run --port 8000 --workers 4 --proxy-headers --forwarded-allow-ips "10.0.0.0/8"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --timeout-graceful-shutdown 25
```

`fastapi[standard]` installs `uvicorn[standard]`, which adds uvloop and httptools; Uvicorn picks them automatically
on Linux and macOS (`--loop auto`, `--http auto`).

## FastAPI Cloud with fastapi deploy: put the API online without a Dockerfile

The fastest way to put an API online without writing a Dockerfile is FastAPI Cloud, the hosting service from the
FastAPI team; `fastapi[standard]` includes its CLI, so a project with a `[tool.fastapi] entrypoint` deploys with one
command. It provides HTTPS and request-based autoscaling.

```bash
uv run fastapi deploy          # opens the browser to log in the first time, then prints the app's URL
```

Use it when you want no Dockerfile or cluster to run; the rest of this skill applies when you host the app yourself.
Install `fastapi[standard-no-fastapi-cloud-cli]` instead of `fastapi[standard]` to leave the cloud CLI out.

## How many processes

The process count depends on what manages replicas.

| Platform | Run |
|---|---|
| Kubernetes, ECS, Cloud Run, Nomad (the platform scales containers) | one Uvicorn process per container, no `--workers`; scale with replicas |
| a VM or a single Docker host | `--workers N`, starting with N = CPU cores; Uvicorn restarts dead workers |
| a team that already runs Gunicorn | `gunicorn app.main:app -k uvicorn_worker.UvicornWorker -w 4` with the `uvicorn-worker` package |

- `uvicorn.workers.UvicornWorker` is deprecated; install `uvicorn-worker` (0.4.0) and use `uvicorn_worker.UvicornWorker`.
- Each worker is a separate process with its own memory, its own database pool and its own lifespan. Size the
  database pool for workers × replicas (fastapi-sqlalchemy-async).
- An async app spends most of its time waiting on I/O; more workers than cores rarely helps. A CPU-bound app needs a
  queue, not more workers (fastapi-background-tasks).

## Behind a reverse proxy

A TLS-terminating proxy (Nginx, Traefik, Caddy, a cloud load balancer) talks plain HTTP to Uvicorn, so the app must
trust the proxy's `X-Forwarded-*` headers to know the real scheme, host and client address.

```bash
fastapi run --proxy-headers --forwarded-allow-ips "10.0.0.0/8"     # the proxy's address or network
```

- Without it, redirects (the trailing-slash 307, `RedirectResponse`) point to `http://`, and `request.client.host` is
  the proxy's address, which breaks rate limiting and audit logs.
- `--forwarded-allow-ips "*"` trusts any sender; use it only when nothing but the proxy can reach the container.
- Nginx needs `proxy_set_header X-Forwarded-Proto $scheme;` and friends, plus `Upgrade`/`Connection` headers for
  WebSockets; a full config is in the reference.

## Serving under a path prefix

When the proxy serves the app under `/api` and strips the prefix, tell the app its public prefix so `/docs` and
generated URLs are right. Since FastAPI 0.146 `FastAPI(root_path=...)` is deprecated; set it on the server.

```bash
fastapi run --root-path /api
```

If the proxy does not strip the prefix, use `APIRouter(prefix="/api")` instead and no root path.

## Docker image with uv

A two-stage build installs locked dependencies in a builder layer that is cached until `uv.lock` changes, then copies
only the virtual environment and code into a slim runtime image that runs as a non-root user.

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_NO_DEV=1
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

FROM python:3.14-slim
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
CMD ["fastapi", "run", "--port", "8000", "--proxy-headers"]
```

- Always the exec form of `CMD` (a JSON list): the shell form wraps the server in `/bin/sh`, which does not forward
  `SIGTERM`, so graceful shutdown never happens.
- Pin the uv image tag and the Python minor version; `uv.lock` pins the rest.
- A `.dockerignore` with `.venv`, `.git`, `.env` and `tests` keeps secrets and junk out of the image.
- The image names no trusted proxy on purpose: pass the proxy's network at start (`docker run -e
  FORWARDED_ALLOW_IPS=10.0.0.0/8 ...`), which `fastapi run` reads when the flag is absent. Unset, only `127.0.0.1` and
  `::1` are trusted. Never bake `"*"` into the image: a forged `X-Forwarded-For` then becomes `client.host`, the key
  for a `slowapi` login limit.

## Migrations as a release step

Migrations run once per release, before new code takes traffic, and never from the app's lifespan (every worker and
replica would race).

- Kubernetes: a `Job` (or a Helm pre-upgrade hook) running `alembic upgrade head` with the same image.
- Docker Compose or a single host: a one-off `docker compose run --rm api alembic upgrade head` before `up -d`.
- Platforms with a release phase (Heroku, Render, Fly.io): put `alembic upgrade head` in the release command.

Expand-and-contract migrations (fastapi-alembic-migrations) keep old and new code working during the rollout.

## Configuration and secrets

The image is the same in every environment; configuration comes from environment variables or mounted secret files,
read by pydantic-settings (fastapi-project-structure).

- Secrets come from the platform's secret store (Kubernetes Secrets, AWS Secrets Manager, Vault), never baked into
  the image or committed.
- `FASTAPI_ENV=production` if code branches on it; `fastapi run` does not set it.
- Turn off `/docs` and `/openapi.json` in production or protect them (fastapi-openapi).

## Graceful shutdown and zero-downtime deploys

On `SIGTERM` Uvicorn stops accepting connections, waits for in-flight requests, runs the lifespan shutdown, then
exits. A deploy drops requests only if the platform kills the process first or keeps routing to it.

- `uvicorn --timeout-graceful-shutdown 25` caps the wait; keep it below the platform's grace period (Kubernetes
  `terminationGracePeriodSeconds`, 30 s by default).
- In Kubernetes, add a `preStop` sleep of 5–10 s so the endpoint is removed from the load balancer before shutdown
  starts.
- Readiness checks the database (`/ready`); liveness only checks the process (`/health`). A failing database must
  not restart every pod.
- Long-lived streams and WebSockets are cut at shutdown; clients must reconnect (SSE does it automatically).

## Verify the image and the deploy

Build the image, then prove it runs as non-root, answers, stops cleanly and honours the proxy headers.

```bash
docker build -t items-api .
docker run --rm items-api id -u                                 # expected: not 0
docker run -d --name items-check -p 8000:8000 --env-file .env.staging items-api
curl -fsS http://127.0.0.1:8000/health
curl -si -H 'X-Forwarded-Proto: https' http://127.0.0.1:8000/items | grep -i '^location: https://'   # behind a proxy
time docker stop items-check                                    # expected: far less than Docker's 10 s
docker logs items-check 2>&1 | grep 'Finished server process'
docker rm items-check
```

If `docker stop` takes the full 10 seconds, `SIGTERM` never reached Uvicorn: look for a shell-form `CMD`.

## Deployment anti-patterns

Each of these works on a laptop and drops requests in production.

| Do not | Do instead |
|---|---|
| `--workers` inside a Kubernetes pod | one process per container, and scale with replicas |
| a shell-form `CMD` | the exec form, a JSON list |
| `alembic upgrade head` in the lifespan | one release step |
| `--forwarded-allow-ips "*"` on a reachable container | the proxy's address or network |
| `uvicorn.workers.UvicornWorker` | `uvicorn_worker.UvicornWorker` |
| secrets in the image | the platform's secret store |
| liveness probes that check the database | `/health` for liveness, `/ready` for readiness |

## Deployment checklist

Go through this deployment checklist before merging a change.

```text
[ ] one process per container on orchestrators; --workers N on VMs; uvicorn-worker if Gunicorn is required
[ ] --proxy-headers and --forwarded-allow-ips set to the proxy's network
[ ] path prefix via --root-path, not FastAPI(root_path=...)
[ ] multi-stage uv image: locked deps, non-root user, exec-form CMD, .dockerignore
[ ] migrations run once per release in a separate step
[ ] secrets from the platform; docs off or protected in production
[ ] graceful-shutdown timeout below the platform grace period; preStop delay on Kubernetes
[ ] /health for liveness, /ready for readiness; probes excluded from telemetry
[ ] database connections: workers × replicas × (pool_size + max_overflow) below max_connections
```

## Common deployment problems

These are the deployment problems people hit most, each with its symptom, cause and fix.

1. **Redirects go to `http://` behind HTTPS.** Uvicorn ignores `X-Forwarded-Proto` from an untrusted address. Set
   `--forwarded-allow-ips`.
2. **Every client IP is the load balancer's.** Same cause; also check the proxy sets `X-Forwarded-For`.
3. **Requests fail during each deploy.** Shell-form `CMD`, no `preStop` delay, or a shutdown timeout longer than the
   grace period, so the process is killed mid-request.
4. **Production logs say `WatchFiles detected changes` and the service restarts, or warn that the `workers` flag is
   ignored.** `--reload` is on. It watches files and restarts on change; it is for `fastapi dev` only and cannot be
   combined with `--workers`.
5. **`too many clients already` from PostgreSQL after scaling out.** Replicas multiplied the pools. Lower
   `pool_size`, add PgBouncer, or raise `max_connections` deliberately.
6. **Old images ship with dev tools.** `uv sync` without `UV_NO_DEV=1` (or `--no-dev`) installs the dev group.

## See also

- fastapi-alembic-migrations: expand-and-contract releases.
- fastapi-errors-observability: health checks and telemetry.
- fastapi-project-structure: settings from the environment.
- fastapi-sqlalchemy-async: pool sizing across workers.
- fastapi-performance: sizing a service before launch.
