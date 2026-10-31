---
name: fastapi-security
description: Secures a FastAPI API - password hashing with pwdlib and Argon2, OAuth2 password flow with JWT access tokens from PyJWT, a current-user dependency, roles and OAuth2 scopes, API keys, cookie sessions, WebSocket authentication, 401 versus 403 versus 404, and the hardening list (CORS, trusted hosts, rate limits, security headers, upload and body limits, docs in production, SQL injection, SSRF, mass assignment, dependency audits). Use when adding login, protecting endpoints, checking permissions, configuring CORS, or reviewing a FastAPI app for security.
metadata:
  tags:
  - fastapi
  - security
  - authentication
  - authorization
  - jwt
  - oauth2
  - pwdlib
  - argon2
  - cors
  - rate-limiting
---

# FastAPI authentication and security

This skill adds authentication and authorization to a FastAPI app and closes the common holes. Checked with FastAPI
0.162, PyJWT 2.15.1 and pwdlib 0.3.1, the libraries the FastAPI docs and the official full-stack template use.

## Step 0: installed versions for authentication

Read the versions first: security fixes land in patch releases. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastapi|pyjwt|cryptography|pwdlib|argon2-cffi|bcrypt) '
uv run --with pip-audit pip-audit                               # known advisories in the project's environment
```

401 for missing credentials needs FastAPI 0.122, and strict JSON `Content-Type` needs 0.132. If `passlib` or
`python-jose` shows up in `uv tree`, replace it first.

## Choose the authentication method

The right method depends on who calls the API.

| Caller | Method |
|---|---|
| your own web frontend on the same site | an HttpOnly, Secure, SameSite cookie holding a session id or a short JWT |
| mobile apps, SPAs on another domain, third-party clients | bearer access tokens (JWT) plus refresh tokens |
| other services and scripts | API keys or OAuth2 client credentials |
| enterprise users with an identity provider | OpenID Connect through the provider; validate its tokens, do not store passwords |

Prefer a hosted identity provider (Auth0, Keycloak, Entra ID, Cognito, Clerk) when you need social login, MFA or
account recovery; building those yourself is where most auth bugs live.

## Hash passwords with pwdlib and Argon2

Passwords are stored only as slow, salted hashes. `pwdlib` replaced the unmaintained `passlib` in the FastAPI docs;
`PasswordHash.recommended()` uses Argon2id.

```bash
uv add "pwdlib[argon2]" "pyjwt[crypto]"
```

```python
# app/security.py
from pwdlib import PasswordHash

password_hash = PasswordHash.recommended()
DUMMY_HASH = password_hash.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)
```

To migrate old bcrypt hashes, install `pwdlib[argon2,bcrypt]`, build `PasswordHash((Argon2Hasher(), BcryptHasher()))`
from `pwdlib.hashers.argon2` and `pwdlib.hashers.bcrypt`, and use
`valid, new_hash = password_hash.verify_and_update(password, hashed)`; store `new_hash` when it is not `None`.

An Argon2 check takes about 40 ms of CPU, so one `verify_password()` call on the event loop stalls every other request
in that worker for that long. Run it with `asyncio.to_thread`, as the login endpoint below does.

## Login endpoint with the OAuth2 password flow

The login endpoint accepts form fields `username` and `password` (`OAuth2PasswordRequestForm`), so Swagger UI's
Authorize button works. Verify a dummy hash when the user does not exist, so response time does not reveal which
emails are registered, and verify in a thread so the event loop stays free.

```python
import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token")
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], session: SessionDep) -> Token:
    user = await session.scalar(select(User).where(User.email == form.username.lower()))
    hashed = user.hashed_password if user else DUMMY_HASH       # same cost when the user does not exist
    valid = await asyncio.to_thread(verify_password, form.password, hashed)
    if user is None or not valid or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return Token(access_token=create_access_token(user.id))
```

Give the same message for "no such user" and "wrong password". Rate-limit this endpoint (see hardening).

## JWT access tokens with PyJWT

An access token is a signed JWT with a subject, an expiry and nothing secret inside; anyone can read its payload.

```python
from datetime import UTC, datetime, timedelta

import jwt

ALGORITHM = "HS256"


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=settings.access_token_minutes)
    claims = {"sub": str(user_id), "typ": "access", "iat": now, "exp": exp}
    return jwt.encode(claims, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> int:
    payload = jwt.decode(
        token,
        get_settings().secret_key.get_secret_value(),
        algorithms=[ALGORITHM],                       # a fixed list: never read the algorithm from the token
        options={"require": ["exp", "sub", "typ"]},
    )
    if payload["typ"] != "access":                    # a refresh token never opens an endpoint
        raise jwt.InvalidTokenError("wrong token type")
    return int(payload["sub"])
```

- The HS256 key is at least 32 random bytes (`openssl rand -hex 32`), kept in the environment as a `SecretStr`.
- `sub` must be a string. Keep access tokens short (5–30 minutes) and use refresh tokens for longer sessions.
- Install `pyjwt[crypto]` for RS256/ES256 and for clean strict type checking.
- Refresh tokens, revocation, RS256 with key rotation, cookie storage, and password reset and email verification
  links: [references/tokens.md](references/tokens.md).

## Current-user dependency

One dependency turns the bearer token into a user, and every protected endpoint asks for it by type.

```python
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(session: SessionDep, token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    try:
        user_id = decode_access_token(token)
    except (jwt.InvalidTokenError, ValueError):
        raise CREDENTIALS_ERROR from None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise CREDENTIALS_ERROR
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
```

Protect a whole router with `APIRouter(dependencies=[Depends(get_current_user)])`, or take `user: CurrentUser` in
endpoints that need the user object.

## 401, 403 and 404

The status code tells the client what to do next, so use each one for its meaning.

- **401 Unauthorized**: no credentials or invalid ones; the client should log in. Since FastAPI 0.122 the built-in
  security classes (`OAuth2PasswordBearer`, `HTTPBearer`, `APIKeyHeader`) answer 401 when credentials are missing;
  older versions answered 403.
- **403 Forbidden**: the user is known but lacks the role or scope.
- **404 Not Found**: for another user's resource, so the response does not confirm that the id exists.

## Roles and OAuth2 scopes

Authorization checks are dependencies layered on the current user. Simple roles use a flag or a role column; finer
rights use OAuth2 scopes checked with `Security()` and `SecurityScopes`.

```python
from fastapi import Security
from fastapi.security import SecurityScopes


def require_role(role: str):
    async def check(user: CurrentUser) -> User:
        if role not in user.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not enough permissions")
        return user
    return check


AdminUser = Annotated[User, Depends(require_role("admin"))]


async def scoped_user(security_scopes: SecurityScopes, user: CurrentUser, token: TokenDep) -> User:
    granted = set(token_scopes(token))
    if missing := set(security_scopes.scopes) - granted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing scope: {sorted(missing)[0]}")
    return user


@router.delete("/{item_id}")
async def delete_item(user: Annotated[User, Security(scoped_user, scopes=["items:write"])]) -> None: ...
```

Ownership is checked in the query (`WHERE owner_id = :user_id`), not after loading; see fastapi-repository-pattern.

## API keys for service clients

API keys identify a program, not a person. Store only a hash of each key, compare in constant time and give each key
its own scopes and an expiry.

```python
import hashlib
import hmac

from fastapi.security import APIKeyHeader

api_key_header = APIKeyHeader(name="X-API-Key")


async def get_client(key: Annotated[str, Depends(api_key_header)], session: SessionDep) -> ApiClient:
    digest = hashlib.sha256(key.encode()).hexdigest()
    client = await session.scalar(select(ApiClient).where(ApiClient.key_hash == digest, ApiClient.revoked.is_(False)))
    if client is None or not hmac.compare_digest(client.key_hash, digest):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    return client
```

Generate keys with `secrets.token_urlsafe(32)` and show them once. A prefix (`sk_live_`) helps secret scanners find
leaked keys.

## WebSocket authentication

Browsers cannot set an `Authorization` header on a WebSocket. Since FastAPI 0.144 security dependencies work in
WebSocket endpoints, so read the token from a header for non-browser clients, or from a short-lived ticket in the
query string or the first message for browsers. Never put a long-lived token in a URL: URLs end up in proxy logs.
Details in fastapi-websockets-streaming.

## Hardening list

Authentication is one layer; these settings close the other common holes. Code for each is in
[references/hardening.md](references/hardening.md).

1. **CORS**: list exact origins; never `allow_origins=["*"]` together with `allow_credentials=True`.
2. **Trusted hosts**: `TrustedHostMiddleware` with your domains, so Host-header tricks do not reach the app.
3. **HTTPS only**: terminate TLS at the proxy, set `Strict-Transport-Security`, mark cookies `Secure`.
4. **Rate limits** on login, signup, password reset and expensive endpoints.
5. **Body and upload limits** at the proxy, plus size checks in upload endpoints.
6. **Strict JSON Content-Type** (FastAPI 0.132+ default): keep it on.
7. **API docs** off or protected in production: `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`.
8. **SQL injection**: only bound parameters; no f-string SQL.
9. **Mass assignment**: input schemas list writable fields; never `Model(**request.json())`.
10. **SSRF**: never fetch a URL from user input without an allow-list.
11. **Errors**: no stack traces, SQL or `str(exc)` in responses.
12. **Dependencies**: `uv audit` (experimental in uv 0.11+) or `uv run --with pip-audit pip-audit` in CI; keep `uv.lock` committed.

## Verify authentication with attack cases

Every token and redirect path gets its attack cases, because a login that works proves nothing about one that refuses.
The token tests need the app and a bearer-protected route; the redirect helper below keeps `?next=` on your own site.

```python
import re

SAFE_PATH = re.compile(r"/(?![/\\])[A-Za-z0-9\-._~/?=&%]*")


def safe_next(target: str | None, default: str = "/") -> str:
    return target if target and SAFE_PATH.fullmatch(target) else default
```

```python
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest

SECRET = "k" * 40                                               # the test settings' secret


def claims(**extra: object) -> dict[str, object]:
    now = datetime.now(UTC)
    return {"sub": "1", "typ": "access", "iat": now, "exp": now + timedelta(minutes=5), **extra}


@pytest.mark.parametrize(
    "token",
    [
        jwt.encode(claims(), None, algorithm="none"),
        jwt.encode(claims(exp=datetime(2020, 1, 1, tzinfo=UTC)), SECRET, algorithm="HS256"),
        jwt.encode(claims(typ="refresh"), SECRET, algorithm="HS256"),
        jwt.encode(claims(), "x" * 32, algorithm="HS256"),
    ],
    ids=["alg-none", "expired", "refresh-as-access", "wrong-key"],
)
async def test_bad_token_is_401(client: httpx.AsyncClient, token: str) -> None:
    response = await client.get("/items/7", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_another_users_item_is_404(client: httpx.AsyncClient, alice: User, bobs_item: Item) -> None:
    token = create_access_token(alice.id)
    response = await client.get(f"/items/{bobs_item.id}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "target", ["//evil.example", "/\t/evil.example", "/\n/evil.example", "https://evil.example", "/\\evil.example"]
)
def test_next_stays_on_this_site(target: str) -> None:
    assert safe_next(target) == "/"
```

```bash
uv run pytest -k "bad_token or another_user or next_stays"
```

## Authentication and security anti-patterns

Each of these is a known way in.

| Do not | Do instead |
|---|---|
| call `verify_password` directly in `async def` | `await asyncio.to_thread(verify_password, ...)` |
| decode a JWT without `algorithms=[...]` | a fixed list, never read from the token |
| accept any valid JWT as an access token | check `typ`, so a refresh token never opens an endpoint |
| redirect to `?next=` unchecked | `safe_next()`, which keeps a single-slash path on your site |
| a long-lived token in a URL | a header, a cookie or a short ticket |
| different messages for "no user" and "wrong password" | one message, and the dummy hash |
| `allow_origins=["*"]` with credentials | exact origins |
| `passlib`, `python-jose` | `pwdlib`, `pyjwt` |

## Security checklist

Go through this authentication and security checklist before merging a change.

```text
[ ] passwords hashed with pwdlib Argon2; constant-time login with a dummy hash
[ ] JWT signed with a 32+ byte secret from settings; algorithms fixed; exp and sub required; short expiry
[ ] one get_current_user dependency; 401 with WWW-Authenticate on failure
[ ] 403 for missing rights, 404 for other users' resources
[ ] ownership and tenant checks inside the SQL WHERE
[ ] API keys stored hashed, compared with hmac.compare_digest, scoped and revocable
[ ] no tokens in URLs or logs
[ ] hardening list applied: CORS, trusted hosts, HTTPS, rate limits, size limits, docs, audits
```

## Common authentication and security problems

These are the authentication and security problems people hit most, each with its symptom, cause and fix.

1. **Every token answers 401, or `jwt.decode` raises a `DecodeError` about the `algorithms` argument.** It is missing,
   and PyJWT requires it. Pass a fixed list; never the algorithm read from the token header, or an attacker picks
   `none` or a public key as an HMAC secret.
2. **Tests expect 403 for a missing token.** FastAPI 0.122 changed missing credentials to 401. Update the tests; the
   docs page "Use Old 403 Authentication Error Status Codes" shows how to subclass for the old behaviour if a client
   depends on it.
3. **`InsecureKeyLengthWarning`.** The HMAC key is shorter than 32 bytes. Generate a new one.
4. **CORS errors in the browser only.** CORS is enforced by browsers; curl works regardless. Check the exact origin
   string, including scheme and port.
5. **Logout does not log out.** A JWT stays valid until it expires. Keep expiry short, or keep a deny-list of token
   ids (`jti`) in Redis until their expiry.

## See also

- fastapi-dependencies: chaining checks on the current user.
- fastapi-repository-pattern: scope guards in `WHERE`.
- fastapi-middleware: CORS and trusted hosts.
- fastapi-websockets-streaming: tickets for browser WebSockets.
- fastapi-errors-observability: what stays out of logs and responses.
