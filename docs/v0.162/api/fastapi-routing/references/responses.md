---
title: FastAPI responses and response classes
author: MOZGII, Inc.
lang: en
---

# FastAPI responses and response classes

This reference covers what an endpoint can return in FastAPI 0.162: schemas, response classes, headers and cookies,
redirects, files, and why `ORJSONResponse` is deprecated.

## Contents

This reference on FastAPI responses and response classes has these sections:

- JSON serialization through the return type
- ORJSONResponse and UJSONResponse are deprecated
- Setting headers and cookies
- Returning a Response directly
- Redirects
- Files and static frontends
- HTML pages with Jinja2 templates
- Different schemas for input and output

## JSON serialization through the return type

When an endpoint has a Pydantic return type or `response_model`, FastAPI 0.130+ serializes the result with Pydantic
in Rust, skipping `jsonable_encoder`. That is the fastest path and needs no custom response class.

```python
@router.get("/")
async def list_items(session: SessionDep) -> list[ItemRead]:
    rows = await session.scalars(select(Item).limit(100))
    return [ItemRead.model_validate(r) for r in rows]
```

An endpoint without a return type falls back to `jsonable_encoder` and the standard `json` module, which is slower
and serializes whatever you return, including fields you did not mean to expose.

## ORJSONResponse and UJSONResponse are deprecated

Since FastAPI 0.131 `ORJSONResponse` and `UJSONResponse` are deprecated: declaring a return type gives the same or
better speed through Pydantic. Remove `default_response_class=ORJSONResponse` from old apps and add return types.

## Setting headers and cookies

Declare a `Response` parameter to set headers, cookies or the status code while still returning a schema.

```python
from fastapi import Response


@router.post("/login")
async def login(response: Response, body: LoginBody) -> Token:
    token = await auth_service.login(body)
    response.set_cookie(
        "session", token.access_token, httponly=True, secure=True, samesite="lax", max_age=3600
    )
    response.headers["Cache-Control"] = "no-store"
    return token
```

## Returning a Response directly

Returning a `Response` subclass skips validation and serialization; use it for non-JSON content and set
`response_model=None` or a `response_class` so OpenAPI stays correct.

```python
from fastapi.responses import HTMLResponse, PlainTextResponse


@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots() -> str:
    return "User-agent: *\nDisallow: /admin\n"


@router.get("/page", response_class=HTMLResponse)
async def page() -> str:
    return "<h1>Items</h1>"
```

## Redirects

`RedirectResponse` defaults to 307, which keeps the method and body; use 303 after a form POST so the browser follows
with GET.

```python
from fastapi.responses import RedirectResponse


@router.post("/form")
async def submit() -> RedirectResponse:
    return RedirectResponse("/thanks", status_code=303)
```

Never redirect to a URL taken from the request (`?next=...`) without checking it against an allow-list; that is an
open redirect.

## Files and static frontends

`FileResponse` streams a file from disk with range support; `app.frontend()` (FastAPI 0.138+) serves a built
single-page app with client-side routing fallbacks.

```python
from fastapi.responses import FileResponse

app.frontend("/", directory="dist")       # dist/index.html, dist/assets/*; API routes still match first


@router.get("/reports/{report_id}.pdf")
async def report(report_id: int) -> FileResponse:
    path = await reports_service.path_for(report_id)      # a path you built, never one from the client
    return FileResponse(path, media_type="application/pdf", filename=f"report-{report_id}.pdf")
```

`app.frontend()` serves `index.html` only for `GET`/`HEAD` requests that accept `text/html`; missing assets still
return 404, and a `404.html` in the directory is used automatically. It raises at startup when the directory is
missing unless `FASTAPI_ENV=development` (set by `fastapi dev`) or `check_dir=False`.

## HTML pages with Jinja2 templates

FastAPI renders server-side HTML with `Jinja2Templates` (Jinja2 comes with `fastapi[standard]`), which suits admin
pages, emails and apps built with HTMX. Since Starlette 1.0 the request is the first argument of `TemplateResponse`.

```python
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


@router.get("/items/{item_id}/page", response_class=HTMLResponse, include_in_schema=False)
async def item_page(request: Request, item: OwnedItem) -> HTMLResponse:
    name = "items/_row.html" if request.headers.get("HX-Request") else "items/page.html"   # HTMX partial
    return templates.TemplateResponse(request, name, {"item": item})
```

- The old form `TemplateResponse("page.html", {"request": request})` was removed in Starlette 1.0.
- Jinja2 autoescapes `.html` templates here; never mark user input `|safe`.
- `request.url_for("static", path="app.css")` builds asset URLs that respect `--root-path`.
- Forms posted from these pages need CSRF protection when the session lives in a cookie: a SameSite cookie plus a
  CSRF token in each form.

## Different schemas for input and output

FastAPI generates separate OpenAPI schemas for a model's input and output when they differ (for example a field with
a default is optional on input but always present on output). Keep that default
(`separate_input_output_schemas=True`) so generated clients get exact types; set it to `False` only when a client
generator cannot handle two schemas per model.
