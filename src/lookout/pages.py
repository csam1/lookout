"""Read a page for deep mode: page cache, then fetch and extract.

Pages are cached by URL, independent of the query, so a page read for one query is reused
by every later query that returns it. Failures that will not change soon (robots, 4xx,
blocked, unsupported type, no text) are cached briefly; transient ones (timeouts, 5xx,
429, network errors) are not.
"""

import asyncio
import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from lookout.cache import NullCache, ResultCache
from lookout.extract import extract
from lookout.fetch import PageFetcher

log = logging.getLogger("lookout.pages")

PAGE_KEY = "lo:page:v1:"
NEGATIVE_TTL_SECONDS = 3600
NEGATIVE_STATUSES = {"robots_disallowed", "unsupported_type", "too_large", "empty_content"}
MAX_CACHED_CHARS = 200_000  # keeps Redis entries bounded; excerpts never need more


@dataclass
class Page:
    status: str
    blocks: list[str] = field(default_factory=list)
    title: str | None = None
    published_at: str | None = None
    fetched_at: str | None = None  # ISO 8601, time of the original fetch
    from_cache: bool = False

    @property
    def markdown(self) -> str:
        return "\n\n".join(self.blocks)


def page_key(url: str) -> str:
    return PAGE_KEY + hashlib.sha256(url.encode()).hexdigest()


def ttl_for_status(status: str, ok_ttl: int) -> int:
    """Seconds to cache a page outcome; 0 means do not cache."""
    if status == "ok":
        return ok_ttl
    if status == "http_429":
        return 0
    if status in NEGATIVE_STATUSES or status.startswith("blocked:") or status.startswith("http_4"):
        return NEGATIVE_TTL_SECONDS
    return 0


def _cap(blocks: list[str]) -> list[str]:
    out, total = [], 0
    for block in blocks:
        if total + len(block) > MAX_CACHED_CHARS:
            break
        out.append(block)
        total += len(block)
    return out


class PageReader:
    def __init__(self, fetcher: PageFetcher, cache: ResultCache | None = None, ok_ttl: int = 6 * 3600) -> None:
        self.fetcher = fetcher
        self.cache = cache or NullCache()
        self.ok_ttl = ok_ttl

    async def read(self, url: str) -> Page:
        """Never raises: unexpected failures come back as Page(status="error")."""
        try:
            key = page_key(url)
            cached = await self.cache.get(key)
            if cached is not None:
                return Page(**cached, from_cache=True)
            page = await self._fetch_and_extract(url)
            ttl = ttl_for_status(page.status, self.ok_ttl)
            if ttl:
                payload = asdict(page)
                payload.pop("from_cache")
                await self.cache.set(key, payload, ttl)
            return page
        except Exception:
            log.exception("page read failed")
            return Page(status="error")

    async def _fetch_and_extract(self, url: str) -> Page:
        fetched = await self.fetcher.fetch(url)
        if fetched.status != "ok" or fetched.body is None:
            return Page(status=fetched.status)
        now = datetime.now(timezone.utc).isoformat()
        # Parsing a large page is CPU-bound; keep it off the event loop.
        extracted = await asyncio.to_thread(extract, fetched.body, fetched.charset)
        if not extracted.blocks:
            return Page(status="empty_content", fetched_at=now)
        return Page(
            status="ok",
            blocks=_cap(extracted.blocks),
            title=extracted.title,
            published_at=extracted.published_at,
            fetched_at=now,
        )
