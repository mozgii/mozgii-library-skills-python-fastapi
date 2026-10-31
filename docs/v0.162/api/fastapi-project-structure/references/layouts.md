---
title: FastAPI project layouts compared
author: MOZGII, Inc.
lang: en
---

# FastAPI project layouts compared

This reference compares the three layouts FastAPI projects use in practice and says when each one fits.

## Contents

This reference on FastAPI project layouts has these sections:

- Layout by domain
- Layout by layer (the full-stack template)
- Single-module start
- Moving from one layout to another

## Layout by domain

The domain layout puts each business area in its own package with its router, schemas, models, service and
repository. It suits teams, long-lived services and anything past five or six resources.

```text
app/
├── main.py
├── config.py
├── db.py
├── deps.py
├── items/{router,schemas,models,service,repository,exceptions}.py
└── users/{router,schemas,models,service,repository,exceptions}.py
```

Strengths: a feature is one folder; ownership is clear; deleting a domain is deleting a folder plus its migration.
Weakness: shared helpers need a home (`app/core/` or `app/common/`) or they get copied.

## Layout by layer (the full-stack template)

The official full-stack FastAPI template (`fastapi/full-stack-fastapi-template`, MIT) groups files by kind: all
routes together, all models together. It suits small and medium apps and teaching.

```text
backend/app/
├── main.py
├── api/
│   ├── deps.py            SessionDep, CurrentUser, superuser check
│   ├── main.py            api_router that includes every route module
│   └── routes/{items,login,users,utils,private}.py
├── core/{config,db,security}.py
├── crud.py                create_user, authenticate, update_user ...
├── models.py              SQLModel table models and API models in one file
└── alembic/               migrations
```

The template (checked 2026-10-28) uses Python 3.14, `fastapi[standard]>=0.141`, SQLModel with a synchronous
`Session`, psycopg 3, PyJWT and `pwdlib[argon2,bcrypt]`, mypy and ty for types, Ruff for lint, and a
`scripts/prestart.sh` that waits for the database, runs `alembic upgrade head` and seeds the first superuser.

Strengths: easy to read for newcomers; every route file looks the same. Weakness: `models.py` and `crud.py` grow
without limit, and unrelated changes collide in them.

## Single-module start

A prototype can live in `app/main.py` with its models and schemas in two more files. Keep the app object in
`app/main.py` and the entrypoint in `pyproject.toml` from day one, so moving to packages later changes imports only.

```text
app/
├── main.py
├── models.py
├── schemas.py
└── routers/{items,users}.py
```

## Moving from one layout to another

Moving from a layer layout to a domain layout is a mechanical refactor when routers only call services:

1. Create `app/<domain>/` and move that domain's route module to `router.py`.
2. Move its models, schemas and CRUD functions next to it; keep the table names unchanged so no migration is needed.
3. Import every model module in one place that Alembic's `env.py` imports (for example `app/models.py` re-exporting
   each domain's models), or autogenerate will think the tables were dropped.
4. Run `uv run alembic check`; it must report no changes.
