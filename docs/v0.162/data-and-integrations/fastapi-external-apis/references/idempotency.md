---
title: Idempotency keys for FastAPI POST endpoints
author: MOZGII, Inc.
lang: en
---

# Idempotency keys for FastAPI POST endpoints

This reference holds the table and the endpoint behind an `Idempotency-Key` header: the first request claims the key,
and a retry gets the stored response instead of creating a second order.

## Contents

This reference on idempotency keys for FastAPI POST endpoints has these sections:

- The idempotency table
- The endpoint that claims a key

## The idempotency table

One row per key and user holds a hash of the request and the stored response, so a retry can replay it and a reused
key with another body can be refused.

```python
from sqlalchemy.dialects.postgresql import JSONB, insert


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]
    key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int | None]
    response: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

## The endpoint that claims a key

The endpoint inserts the key first, so of two requests with one key only the first gets a row back. The order and the
stored response then commit in that same transaction.

```python
@router.post("/orders", status_code=201)
async def create_order(
    body: OrderCreate,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: Annotated[str, Header(max_length=100)],
) -> JSONResponse:
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    claimed = await session.scalar(
        insert(IdempotencyRecord)
        .values(user_id=user.id, key=idempotency_key, request_hash=digest)
        .on_conflict_do_nothing(index_elements=["user_id", "key"])
        .returning(IdempotencyRecord.id)
    )
    if claimed is None:                                      # this key was used before
        record = await session.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user.id, IdempotencyRecord.key == idempotency_key))
        if record is None or record.request_hash != digest:
            raise HTTPException(422, "Idempotency-Key reused with a different request")
        return JSONResponse(record.response, status_code=record.status_code or 200)
    order = await orders_service.create(session, user.id, body)       # flushes, does not commit
    payload = OrderRead.model_validate(order).model_dump(mode="json")
    await session.execute(update(IdempotencyRecord).where(IdempotencyRecord.id == claimed)
                          .values(status_code=201, response=payload))
    await session.commit()                                   # order and stored response commit together
    return JSONResponse(payload, status_code=201)
```
