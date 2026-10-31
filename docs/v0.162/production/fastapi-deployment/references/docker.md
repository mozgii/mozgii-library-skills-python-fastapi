---
title: FastAPI Docker, Compose, Nginx and Kubernetes files
author: MOZGII, Inc.
lang: en
---

# FastAPI Docker, Compose, Nginx and Kubernetes files

This reference holds deployment files for a FastAPI app managed with uv. The Dockerfile and `.dockerignore` were
built and run on 2026-10-28 (Python 3.14-slim, uv 0.13.6): the container started as a non-root user, served
`/health`, answered 401 on protected routes and had uvloop available.

## Contents

This reference on FastAPI Docker, Compose, Nginx and Kubernetes files has these sections:

- Dockerfile
- .dockerignore
- Docker Compose with a migration step
- Nginx in front of Uvicorn
- Kubernetes Deployment and migration Job
- Gunicorn with uvicorn-worker

## Dockerfile

The builder stage installs locked dependencies first (cached until `uv.lock` or `pyproject.toml` change), then the
project; the runtime stage carries only the virtual environment and code.

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
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["fastapi", "run", "--port", "8000", "--proxy-headers"]
```

Build and run:

```bash
docker build -t items-api .
docker run --rm -p 8000:8000 \
  -e FORWARDED_ALLOW_IPS=10.0.0.0/8 \
  -e APP_DATABASE_URL=postgresql+asyncpg://app:CHANGE_ME@db:5432/app \
  -e APP_SECRET_KEY=CHANGE_ME_32_BYTES_HEX \
  items-api
```

Kubernetes ignores `HEALTHCHECK`; it uses its own probes (below).

## .dockerignore

The ignore file keeps local environments, secrets and caches out of the build context and the image.

```text
.venv
.git
.env
__pycache__
*.pyc
.pytest_cache
.ruff_cache
tests
Dockerfile
```

## Docker Compose with a migration step

Compose runs PostgreSQL, a one-off migration service and the API, which starts only after migrations succeed.

```yaml
services:
  db:
    image: postgres:18-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: ${DB_PASSWORD:?set DB_PASSWORD}
      POSTGRES_DB: app
    volumes: [pgdata:/var/lib/postgresql]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d app"]
      interval: 5s
      retries: 10

  migrate:
    build: .
    command: ["alembic", "upgrade", "head"]
    environment: &app-env
      APP_DATABASE_URL: postgresql+asyncpg://app:${DB_PASSWORD}@db:5432/app
      APP_SECRET_KEY: ${APP_SECRET_KEY:?set APP_SECRET_KEY}
    depends_on:
      db: {condition: service_healthy}

  api:
    build: .
    environment: *app-env
    ports: ["8000:8000"]
    depends_on:
      migrate: {condition: service_completed_successfully}

volumes:
  pgdata:
```

The PostgreSQL 18 image keeps data under `/var/lib/postgresql` (versioned subfolders); mount that path, not
`/var/lib/postgresql/data` as with older images.

## Nginx in front of Uvicorn

Nginx terminates TLS, passes the original scheme, host and client address, supports WebSockets and SSE, and limits
body size.

```nginx
upstream api { server api:8000; keepalive 32; }

server {
    listen 443 ssl;
    server_name api.example.com;
    client_max_body_size 10m;

    location / {
        proxy_pass http://api;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header Upgrade $http_upgrade;          # WebSockets
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;                        # long-lived streams
    }
}

map $http_upgrade $connection_upgrade { default upgrade; '' ''; }
```

Start Uvicorn with `--forwarded-allow-ips` set to the Nginx container's network so it trusts these headers.

## Kubernetes Deployment and migration Job

The Deployment runs one Uvicorn process per pod with separate liveness and readiness probes and a short `preStop`
delay; the Job runs migrations with the same image before the rollout.

```yaml
apiVersion: batch/v1
kind: Job
metadata: {name: items-api-migrate-v142}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: registry.example.com/items-api:1.4.2
          command: ["alembic", "upgrade", "head"]
          envFrom: [{secretRef: {name: items-api}}]
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: items-api}
spec:
  replicas: 3
  selector: {matchLabels: {app: items-api}}
  template:
    metadata: {labels: {app: items-api}}
    spec:
      terminationGracePeriodSeconds: 30
      containers:
        - name: api
          image: registry.example.com/items-api:1.4.2
          command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000",   # trust only the proxy network
                    "--proxy-headers", "--forwarded-allow-ips", "10.0.0.0/8", "--timeout-graceful-shutdown", "20"]
          ports: [{containerPort: 8000}]
          envFrom: [{secretRef: {name: items-api}}]
          resources:
            requests: {cpu: 250m, memory: 256Mi}
            limits: {memory: 512Mi}
          readinessProbe: {httpGet: {path: /ready, port: 8000}, periodSeconds: 5}
          livenessProbe: {httpGet: {path: /health, port: 8000}, periodSeconds: 10, failureThreshold: 3}
          lifecycle:
            preStop: {exec: {command: ["sleep", "5"]}}
```

The `sleep` binary must exist in the image (it does in `python:*-slim`; distroless images need another approach).

## Gunicorn with uvicorn-worker

Gunicorn as the process manager is optional since Uvicorn manages workers itself; use it when your platform or team
standardizes on Gunicorn.

```bash
uv add gunicorn uvicorn-worker
gunicorn app.main:app -k uvicorn_worker.UvicornWorker -w 4 -b 0.0.0.0:8000 \
  --graceful-timeout 25 --forwarded-allow-ips "10.0.0.0/8"
```

The old `uvicorn.workers.UvicornWorker` import still exists in Uvicorn 0.54 but warns that it is deprecated.
