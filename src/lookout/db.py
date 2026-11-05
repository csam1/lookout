"""Postgres storage for API keys and usage events.

All timestamps are stored as naive UTC so Postgres and SQLite (tests) compare them the same way.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, case, false, func, inspect, select, text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """Mark a stored naive-UTC timestamp as UTC so the API serialises it with an offset."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # First characters of the key, safe to show in logs and listings.
    key_prefix: Mapped[str] = mapped_column(String(16))
    # SHA-256 of the full key. The plaintext key is never stored.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    monthly_credit_limit: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Admin keys can manage keys and read everyone's usage (/v1/admin, /admin).
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_key_created", "api_key_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    request_id: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(String(50))
    mode: Mapped[str] = mapped_column(String(10))
    credits: Mapped[int] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    result_count: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)

    async def close(self) -> None:
        await self.engine.dispose()


def _add_missing_columns(sync_conn) -> None:
    """Stopgap until Alembic: create_all never alters existing tables, so add columns that
    were introduced after a database was first created. Idempotent."""
    columns = {c["name"] for c in inspect(sync_conn).get_columns("api_keys")}
    if "is_admin" not in columns:
        sync_conn.execute(text("ALTER TABLE api_keys ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"))


async def keys_with_usage(session: AsyncSession) -> list[tuple[ApiKey, int, int, datetime | None]]:
    """Every key with (this month's calls, this month's credits, last use ever)."""
    month = (
        select(
            UsageEvent.api_key_id.label("key_id"),
            func.count(UsageEvent.id).label("calls"),
            func.sum(UsageEvent.credits).label("credits"),
        )
        .where(UsageEvent.created_at >= month_start())
        .group_by(UsageEvent.api_key_id)
        .subquery()
    )
    last = (
        select(UsageEvent.api_key_id.label("key_id"), func.max(UsageEvent.created_at).label("last_used"))
        .group_by(UsageEvent.api_key_id)
        .subquery()
    )
    stmt = (
        select(ApiKey, month.c.calls, month.c.credits, last.c.last_used)
        .outerjoin(month, month.c.key_id == ApiKey.id)
        .outerjoin(last, last.c.key_id == ApiKey.id)
        .order_by(ApiKey.active.desc(), ApiKey.id)
    )
    rows = await session.execute(stmt)
    return [(key, int(calls or 0), int(credits or 0), last_used) for key, calls, credits, last_used in rows]


async def month_totals(session: AsyncSession) -> dict:
    stmt = select(
        func.count(UsageEvent.id),
        func.coalesce(func.sum(UsageEvent.credits), 0),
        func.coalesce(func.sum(case((UsageEvent.cache_hit, 1), else_=0)), 0),
        func.count(func.distinct(UsageEvent.api_key_id)),
        func.coalesce(func.sum(case((UsageEvent.mode == "deep", 1), else_=0)), 0),
    ).where(UsageEvent.created_at >= month_start())
    calls, credits, cache_hits, keys_used, deep_calls = (await session.execute(stmt)).one()
    return {
        "calls": int(calls),
        "credits": int(credits),
        "cache_hits": int(cache_hits),
        "keys_used": int(keys_used),
        "fast_calls": int(calls) - int(deep_calls),
        "deep_calls": int(deep_calls),
    }


async def daily_usage(session: AsyncSession, days: int) -> list[dict]:
    """Calls and credits per UTC day for the last `days` days, oldest first, zero-filled.

    `deep_credits` is the share of each day's credits spent in deep mode.
    """
    today = utcnow().date()
    first = today - timedelta(days=days - 1)
    day = func.date(UsageEvent.created_at)
    deep = func.coalesce(func.sum(case((UsageEvent.mode == "deep", UsageEvent.credits), else_=0)), 0)
    stmt = (
        select(day, func.count(UsageEvent.id), func.coalesce(func.sum(UsageEvent.credits), 0), deep)
        .where(UsageEvent.created_at >= datetime.combine(first, datetime.min.time()))
        .group_by(day)
    )
    # Postgres returns date objects, SQLite returns 'YYYY-MM-DD' strings.
    found = {
        str(d)[:10]: (int(calls), int(credits), int(deep_credits))
        for d, calls, credits, deep_credits in await session.execute(stmt)
    }
    out = []
    for offset in range(days):
        d: date = first + timedelta(days=offset)
        calls, credits, deep_credits = found.get(d.isoformat(), (0, 0, 0))
        out.append({"date": d.isoformat(), "calls": calls, "credits": credits, "deep_credits": deep_credits})
    return out


async def usage_this_month(session: AsyncSession, api_key_id: int) -> tuple[int, int]:
    """Return (calls, credits) for the key since the start of the current UTC month."""
    stmt = select(func.count(UsageEvent.id), func.coalesce(func.sum(UsageEvent.credits), 0)).where(
        UsageEvent.api_key_id == api_key_id,
        UsageEvent.created_at >= month_start(),
    )
    calls, credits = (await session.execute(stmt)).one()
    return int(calls), int(credits)


async def credits_used_this_month(session: AsyncSession, api_key_id: int) -> int:
    stmt = select(func.coalesce(func.sum(UsageEvent.credits), 0)).where(
        UsageEvent.api_key_id == api_key_id,
        UsageEvent.created_at >= month_start(),
    )
    return int((await session.execute(stmt)).scalar_one())
