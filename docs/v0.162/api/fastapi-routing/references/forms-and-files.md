---
title: FastAPI forms and file uploads
author: MOZGII, Inc.
lang: en
---

# FastAPI forms and file uploads

This reference covers HTML form handling and uploads in FastAPI 0.162: form models, how browser quirks are treated,
`UploadFile`, size limits and storing uploads safely.

## Contents

This reference on FastAPI forms and file uploads has these sections:

- Form fields and form models
- Browser form rules (FastAPI 0.147 to 0.162)
- Boolean checkboxes
- UploadFile versus bytes
- Several files in one request
- Upload size limits
- Storing uploads safely

## Form fields and form models

Form data is read with `Form()`, either one field at a time or as a Pydantic model, and needs `python-multipart`.

```python
from typing import Annotated

from fastapi import Form
from pydantic import BaseModel, Field


class ContactForm(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=100)
    email: str
    message: str = Field(max_length=5000)
    subscribe: bool = False


@router.post("/contact")
async def contact(form: Annotated[ContactForm, Form()]) -> dict[str, str]:
    return {"status": "received"}
```

Form models can be combined (0.158+), optional (`Annotated[ContactForm | None, Form()] = None`, 0.158.2+) and given
as unions of models (0.161+).

## Browser form rules (FastAPI 0.147 to 0.162)

Browsers send every text input, even empty ones, and send nothing for an unchecked checkbox. FastAPI's current rules:

| Browser sends | FastAPI 0.162 does |
|---|---|
| empty text input (`name=`) for a scalar field | treats it as missing: the default applies, or a required field fails with 422 (0.149) |
| empty strings inside a list field | keeps them |
| no key at all for a `bool` field (unchecked checkbox) | uses the declared default, or 422 if there is none (0.162; 0.147–0.161 forced `False`) |
| an upload part with no filename and no bytes (nothing was chosen) | treats it as missing (0.150); a named zero-byte file is still accepted |
| a default applied to an omitted field | not counted as set: `model_dump(exclude_unset=True)` leaves it out (0.148) |

JSON bodies are not affected by any of these rules.

## Boolean checkboxes

An HTML checkbox sends its value only when checked, so a checkbox field must default to `False`.

```python
class Preferences(BaseModel):
    newsletter: bool = False      # unchecked → key absent → False
    terms: bool                   # required: absent → 422, which is right for "I accept the terms"
```

## UploadFile versus bytes

`UploadFile` streams the upload into a spooled temporary file and gives async `read`, `seek` and `close`; `bytes` reads
the whole file into memory. Use `bytes` only for small files.

```python
from fastapi import File, UploadFile


@router.post("/avatar")
async def avatar(file: UploadFile) -> dict[str, str | int | None]:
    head = await file.read(512)          # sniff the start
    await file.seek(0)
    return {"name": file.filename, "type": file.content_type, "size": file.size}
```

`file.content_type` and `file.filename` come from the client and can be anything; check the content itself (magic
bytes, an image library) before trusting the type.

## Several files in one request

A list annotation accepts several files under the same field name.

```python
@router.post("/attachments")
async def attachments(files: Annotated[list[UploadFile], File()]) -> list[str | None]:
    return [f.filename for f in files]
```

## Upload size limits

FastAPI does not cap upload size by default, so set a limit at the proxy and check it in the endpoint.

- At the proxy: `client_max_body_size 10m;` in Nginx, or the load balancer's body limit.
- In the endpoint: read at most limit + 1 bytes and reject when more arrived, as in the SKILL.md example, or check
  `file.size` after the upload is spooled.
- Starlette 1.6 added `max_body_size` to `Starlette` and its route classes, but FastAPI 0.162 does not expose it on
  `FastAPI()` or `APIRouter`; rely on the proxy limit and the endpoint check.

## Storing uploads safely

Uploaded files are untrusted input, so store them under names you generate, outside the code tree, ideally in object
storage.

- Never join `file.filename` into a filesystem path: `../../etc/passwd` is a valid filename. Generate a UUID name and
  keep the original name as data.
- Write large files in chunks off the event loop:

```python
import shutil
import uuid
from pathlib import Path

UPLOADS = Path("/var/lib/app/uploads")


def save(file: UploadFile) -> Path:          # plain def: runs in the thread pool when called with to_thread
    target = UPLOADS / f"{uuid.uuid4()}.bin"
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out, length=1024 * 1024)
    return target


path = await asyncio.to_thread(save, upload)
```

- For S3-compatible storage, upload from the client with a presigned URL and send only the object key to the API;
  the API never carries the bytes.
