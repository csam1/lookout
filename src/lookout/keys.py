"""API key generation and hashing."""

import hashlib
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from lookout.db import ApiKey

KEY_PREFIX = "lo_"


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    # Keys are 256-bit random values, so a plain SHA-256 is sufficient (no password-style KDF needed).
    return hashlib.sha256(key.encode()).hexdigest()


def display_prefix(key: str) -> str:
    return key[:10]


async def create_key(
    session: AsyncSession, name: str, monthly_credit_limit: int, *, is_admin: bool = False
) -> tuple[str, ApiKey]:
    """Create a key and return (plaintext, row). The plaintext is shown once and never stored."""
    plaintext = generate_key()
    row = ApiKey(
        name=name,
        key_prefix=display_prefix(plaintext),
        key_hash=hash_key(plaintext),
        monthly_credit_limit=monthly_credit_limit,
        active=True,
        is_admin=is_admin,
    )
    session.add(row)
    await session.commit()
    return plaintext, row
