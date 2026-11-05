"""Search result cache.

Keyed by provider + normalised request parameters. TTL depends on the requested time range:
recent-news queries go stale quickly, reference queries do not.
If Redis is unavailable the cache acts as a miss; it never fails a request.
"""

import hashlib
import json
import logging
from typing import Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from lookout.models import SearchRequest

log = logging.getLogger("lookout.cache")

TTL_SECONDS = {
    "day": 10 * 60,
    "week": 60 * 60,
    "month": 6 * 60 * 60,
    "year": 24 * 60 * 60,
    "any": 24 * 60 * 60,
}

CACHE_VERSION = "v1"  # bump when the cached payload shape changes


def cache_key(provider: str, req: SearchRequest) -> str:
    normalised = {
        "q": " ".join(req.query.lower().split()),
        "objective": " ".join((req.objective or "").lower().split()),
        "mode": req.mode,
        "max_results": req.max_results,
        "max_chars": req.max_chars_per_result,
        "time_range": req.time_range,
        "include": sorted(d.lower() for d in req.include_domains),
        "exclude": sorted(d.lower() for d in req.exclude_domains),
        "raw": req.include_raw_content,
    }
    digest = hashlib.sha256(json.dumps(normalised, sort_keys=True).encode()).hexdigest()
    return f"lo:search:{CACHE_VERSION}:{provider}:{digest}"


def ttl_for(req: SearchRequest) -> int:
    return TTL_SECONDS[req.time_range]


class ResultCache(Protocol):
    async def get(self, key: str) -> dict | None: ...
    async def set(self, key: str, value: dict, ttl: int) -> None: ...
    async def close(self) -> None: ...


class RedisCache:
    def __init__(self, url: str) -> None:
        self._redis = aioredis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)

    async def get(self, key: str) -> dict | None:
        try:
            raw = await self._redis.get(key)
        except RedisError as exc:
            log.warning("cache get failed: %s", exc)
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: dict, ttl: int) -> None:
        try:
            await self._redis.set(key, json.dumps(value), ex=ttl)
        except RedisError as exc:
            log.warning("cache set failed: %s", exc)

    async def close(self) -> None:
        await self._redis.aclose()


class NullCache:
    async def get(self, key: str) -> dict | None:
        return None

    async def set(self, key: str, value: dict, ttl: int) -> None:
        return None

    async def close(self) -> None:
        return None


class MemoryCache:
    """In-process cache for tests. Records TTLs but never expires entries."""

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> dict | None:
        return self.store.get(key)

    async def set(self, key: str, value: dict, ttl: int) -> None:
        self.store[key] = value
        self.ttls[key] = ttl

    async def close(self) -> None:
        return None
