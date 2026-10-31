---
title: FastAPI tokens, refresh and sessions
author: MOZGII, Inc.
lang: en
---

# FastAPI tokens, refresh and sessions

This reference covers token lifetimes beyond a single access token: refresh tokens with rotation, revocation,
asymmetric signing with key rotation, cookies for browser apps and validating tokens from an external identity
provider.

## Contents

This reference on FastAPI tokens, refresh and sessions has these sections:

- Access and refresh tokens
- Refresh token rotation
- Revoking tokens
- RS256 and key rotation
- Tokens in cookies for browser apps
- Password reset and email verification tokens
- Validating tokens from an identity provider

## Access and refresh tokens

A short access token (5–30 minutes) limits the damage of a leak; a long refresh token (days to weeks) lets the client
get new access tokens without the password. Keep them different in type so one cannot be used as the other.

```python
def create_token(
    user_id: int, kind: Literal["access", "refresh"], minutes: int, *, version: int, jti: str | None = None
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(user_id), "typ": kind, "ver": version, "iat": now, "exp": now + timedelta(minutes=minutes),
    }
    if jti:
        claims["jti"] = jti
    return jwt.encode(claims, secret(), algorithm="HS256")


def decode(token: str, expected: Literal["access", "refresh"]) -> dict[str, object]:
    required = ["exp", "sub", "typ", "ver", *(["jti"] if expected == "refresh" else [])]
    payload = jwt.decode(token, secret(), algorithms=["HS256"], options={"require": required})
    if payload["typ"] != expected:
        raise jwt.InvalidTokenError("wrong token type")
    return payload
```

The `ver` claim copies the user's `token_version` column, an integer that starts at 0. Raising that column cancels
every refresh token issued before, as the password reset below does.

## Refresh token rotation

Rotation issues a new refresh token on every use and invalidates the old one. If an old refresh token is used again,
someone copied it: revoke the whole family and make the user log in. Any token that fails to decode (bad signature,
expired, wrong type, missing `jti` or `ver`) answers 401, never a 500.

```python
INVALID_REFRESH = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")


@router.post("/refresh")
async def refresh(body: RefreshBody, session: SessionDep) -> TokenPair:
    try:
        payload = decode(body.refresh_token, "refresh")
    except jwt.InvalidTokenError:
        raise INVALID_REFRESH from None
    stored = await session.get(RefreshToken, payload["jti"], with_for_update=True)
    if stored is None:
        raise INVALID_REFRESH
    if stored.revoked_at is not None:
        await revoke_family(session, stored.family_id)       # reuse detected
        await session.commit()
        raise INVALID_REFRESH
    user = await session.get(User, stored.user_id)
    if user is None or not user.is_active or payload["ver"] != user.token_version:
        raise INVALID_REFRESH                                # the password changed after this token was issued
    stored.revoked_at = datetime.now(UTC)
    new = RefreshToken(id=str(uuid4()), user_id=user.id, family_id=stored.family_id)
    session.add(new)
    await session.commit()
    return TokenPair(
        access_token=create_token(user.id, "access", 15, version=user.token_version),
        refresh_token=create_token(user.id, "refresh", 60 * 24 * 14, version=user.token_version, jti=new.id),
    )
```

The lock from `with_for_update=True` closes a race in rotation. Without it, two requests with the same token both read
an unrevoked row and both get new tokens. With it, the second waits for the first commit, sees `revoked_at` and counts
as reuse.

Store refresh tokens by id (`jti`) only; the token itself is never needed again on the server. Access tokens keep
working until they expire; to cut them off at once, make `get_current_user` compare `ver` with `user.token_version`
too.

## Revoking tokens

A JWT cannot be "deleted"; it is valid until `exp`. To revoke access tokens before expiry (logout everywhere, a
compromised account), keep a deny-list of `jti` values in Redis with a TTL equal to the remaining lifetime, or store
a `tokens_valid_after` timestamp on the user and reject tokens with an older `iat`.

## RS256 and key rotation

Asymmetric signing lets other services verify tokens with a public key without being able to mint them. Put a key id
(`kid`) in the header and keep the previous public key until its tokens expire.

```python
token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "2026-10"})

kid = jwt.get_unverified_header(token)["kid"]
public_pem = PUBLIC_KEYS[kid]                 # current and previous keys
payload = jwt.decode(token, public_pem, algorithms=["RS256"], audience="items-api")
```

Needs `pyjwt[crypto]`. Generate keys with `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072`. Publish
public keys as a JWKS document for other services.

## Tokens in cookies for browser apps

For a browser frontend on your own domain, an HttpOnly cookie keeps the token away from JavaScript (and XSS). Cookies
are sent automatically, so add CSRF protection for state-changing requests.

```python
response.set_cookie(
    "access_token", token, httponly=True, secure=True, samesite="lax", max_age=900, path="/"
)

cookie_scheme = APIKeyCookie(name="access_token")       # fastapi.security
```

- `SameSite=Lax` blocks most cross-site POSTs; add a CSRF token (double-submit cookie or a header the frontend sets)
  for full protection, especially with `SameSite=None`.
- FastAPI's strict JSON Content-Type check (0.132+) stops one cross-site trick (a `Blob` body without preflight),
  but it is not a CSRF defence for form posts.

## Password reset and email verification tokens

A password reset link is a one-time credential, so treat its token like a password: random, short-lived, stored only
as a hash, and dead after one use. Email verification works the same way with a longer expiry.

```python
import asyncio
import hashlib
import secrets
from datetime import UTC, datetime, timedelta


async def start_reset(session: AsyncSession, email: str) -> None:
    user = await session.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        return                                            # same answer either way: no account discovery
    token = secrets.token_urlsafe(32)
    session.add(ResetToken(
        user_id=user.id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    ))
    await session.commit()
    await enqueue("send_reset_email", user.id, token)     # through a job queue, never BackgroundTasks


async def finish_reset(session: AsyncSession, token: str, new_password: str) -> None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = await session.scalar(
        select(ResetToken).where(ResetToken.token_hash == digest, ResetToken.used_at.is_(None),
                                 ResetToken.expires_at > func.now()).with_for_update()
    )
    if row is None:
        raise InvalidResetToken()
    user = await session.get(User, row.user_id)
    user.hashed_password = await asyncio.to_thread(hash_password, new_password)     # Argon2 would stall the event loop
    user.token_version += 1                               # invalidates every refresh token issued before
    row.used_at = func.now()
    await session.commit()
```

- `POST /auth/password-reset` always answers 202 with the same body, whether or not the email exists, and is rate
  limited per address and per IP.
- Put the token in the link's fragment or path, not in a query string your logs record, and serve the reset page with
  `Referrer-Policy: no-referrer`.
- After a reset, end the user's other sessions (a token version or a deny-list) and send a "your password changed"
  email.

## Validating tokens from an identity provider

With an external provider (OpenID Connect), the API never sees passwords; it validates the provider's access tokens
against the provider's published keys.

```python
from jwt import PyJWKClient

jwks = PyJWKClient("https://login.example.com/.well-known/jwks.json", cache_keys=True)


def verify_idp_token(token: str) -> dict[str, object]:
    key = jwks.get_signing_key_from_jwt(token)
    return jwt.decode(
        token, key.key, algorithms=["RS256"], audience="api://items", issuer="https://login.example.com/"
    )
```

Always check `aud` and `iss`; a token issued for another app of the same provider is otherwise accepted. `PyJWKClient`
fetches over the network synchronously, so call it from a `def` dependency or through `asyncio.to_thread`.
