"""API key authentication and monthly credit quotas, backed by Postgres."""

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lookout.db import ApiKey, credits_used_this_month
from lookout.errors import ApiError
from lookout.keys import hash_key


def extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get("x-api-key") or None


async def authenticate(request: Request, session: AsyncSession) -> ApiKey:
    presented = extract_key(request)
    if not presented:
        raise ApiError(401, "unauthorized", "Missing or invalid API key.")
    # Lookup is by hash, so the comparison never touches the plaintext of stored keys.
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(presented)))
    ).scalar_one_or_none()
    if row is None or not row.active:
        raise ApiError(401, "unauthorized", "Missing or invalid API key.")
    return row


async def check_quota(session: AsyncSession, key: ApiKey, cost: int) -> None:
    used = await credits_used_this_month(session, key.id)
    if used + cost > key.monthly_credit_limit:
        raise ApiError(
            429,
            "quota_exceeded",
            f"Monthly credit limit reached ({used}/{key.monthly_credit_limit}).",
        )
