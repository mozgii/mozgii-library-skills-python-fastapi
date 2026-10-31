---
title: FastAPI settings with pydantic-settings
author: MOZGII, Inc.
lang: en
---

# FastAPI settings with pydantic-settings

This reference covers configuration patterns with pydantic-settings 2.16: one settings class, nested groups,
per-domain settings, secrets files and overriding settings in tests.

## Contents

This reference on FastAPI settings with pydantic-settings has these sections:

- One cached Settings object
- Settings as a dependency
- Nested settings groups
- Per-domain settings
- Secrets from files (Docker and Kubernetes secrets)
- Overriding settings in tests
- Rules for secrets

## One cached Settings object

A single `Settings` class read once through an `lru_cache` function gives every module the same validated values and
parses the environment only once per process.

```python
from functools import lru_cache

from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    database_url: PostgresDsn
    secret_key: SecretStr
    debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
```

Environment variables win over the `.env` file, and the `.env` file wins over defaults. With `env_prefix="APP_"`,
`database_url` reads `APP_DATABASE_URL`. Lists and dicts are read as JSON: `APP_CORS_ORIGINS='["https://a.example.com"]'`.

Pyright reports `reportCallIssue` on `Settings()` when a field has no default, because it cannot see that values come
from the environment; the scoped ignore on that one line is the accepted fix.

## Settings as a dependency

Reading settings through a dependency lets one test change a value without touching the environment.

```python
from typing import Annotated

from fastapi import Depends

SettingsDep = Annotated[Settings, Depends(get_settings)]


@app.get("/info")
async def info(settings: SettingsDep) -> dict[str, bool]:
    return {"debug": settings.debug}
```

## Nested settings groups

Nested models group related values; `env_nested_delimiter` maps `APP_DB__POOL_SIZE` to `settings.db.pool_size`.

```python
from pydantic import BaseModel


class DatabaseSettings(BaseModel):
    url: PostgresDsn
    pool_size: int = 10
    echo: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_nested_delimiter="__")

    db: DatabaseSettings
```

## Per-domain settings

A domain with its own configuration (a payment provider, an email sender) gets its own small settings class with its
own prefix, so the main class does not collect every key in the system.

```python
class MailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAIL_")

    host: str
    port: int = 587
    username: str
    password: SecretStr
```

## Secrets from files (Docker and Kubernetes secrets)

`secrets_dir` reads each field from a file named after it, which is how Docker secrets (`/run/secrets`) and
Kubernetes secret volumes deliver values without putting them in the environment.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets")

    secret_key: SecretStr        # read from /run/secrets/secret_key
```

## Overriding settings in tests

Tests override the dependency, or clear the cache after setting environment variables with `monkeypatch`.

```python
import pytest

from app.config import Settings, get_settings
from app.main import app


@pytest.fixture
def settings_override():
    test_settings = Settings(database_url="postgresql+asyncpg://t:t@localhost/t", secret_key="x" * 32)
    app.dependency_overrides[get_settings] = lambda: test_settings
    yield test_settings
    app.dependency_overrides.pop(get_settings, None)


def test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_DEBUG", "true")
    get_settings.cache_clear()
    assert get_settings().debug is True
    get_settings.cache_clear()
```

Code that calls `get_settings()` at import time (for example to build the engine) is not affected by a dependency
override; build such objects in the lifespan or through a factory that takes the settings.

## Rules for secrets

Secrets are the one kind of setting that must never get a default, a log line or a place in the repository.

- Type them as `SecretStr`; `print(settings)` shows `secret_key=SecretStr('**********')`.
- No default value: a missing secret must stop the app at startup.
- An HMAC key for JWT is at least 32 random bytes: `openssl rand -hex 32`. PyJWT 2.11+ warns with
  `InsecureKeyLengthWarning` below that (`enforce_minimum_key_length=True` turns the warning into an error).
- Keep `.env` in `.gitignore` and `.dockerignore`; commit `.env.example` with placeholders such as `APP_SECRET_KEY=change-me`.
