"""Search orchestration: cache, provider call, filtering, dedupe, page reads, excerpting."""

import asyncio
import time
from datetime import datetime
from urllib.parse import urlparse

from lookout.cache import NullCache, ResultCache, cache_key, ttl_for
from lookout.errors import ApiError
from lookout.excerpt import select_excerpts
from lookout.fetch import PageFetcher
from lookout.models import SearchRequest, SearchResponse, SearchResult, Usage
from lookout.pages import Page, PageReader
from lookout.providers.base import ProviderError, ProviderHit, SearchProvider

CREDITS = {"fast": 1, "deep": 2}
# A deep response with pages still loading is cached only briefly: those pages land in the
# page cache shortly after, and the next identical query should see them.
PARTIAL_RESULT_TTL_SECONDS = 120


def credits_for(req: SearchRequest) -> int:
    return CREDITS[req.mode]


def normalize_domain(host: str) -> str:
    host = host.lower().strip().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def domain_of(url: str) -> str:
    return normalize_domain(urlparse(url).hostname or "")


def matches_domain(domain: str, rules: list[str]) -> bool:
    """True if domain equals a rule or is a subdomain of it."""
    for rule in rules:
        r = normalize_domain(rule)
        if domain == r or domain.endswith("." + r):
            return True
    return False


def canonical_url(url: str) -> str:
    p = urlparse(url)
    host = normalize_domain(p.hostname or "")
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{host}{path}"


def rank_score(rank: int) -> float:
    # Placeholder: provider order. Phase 2 replaces this with reranker scores.
    return round(max(0.0, 1.0 - 0.1 * rank), 2)


class SearchService:
    def __init__(
        self,
        provider: SearchProvider,
        fetcher: PageFetcher | None = None,
        cache: ResultCache | None = None,
        *,
        deep_deadline_seconds: float = 3.0,
        page_cache_ttl_seconds: int = 6 * 3600,
    ) -> None:
        self.provider = provider
        self.cache = cache or NullCache()
        self.pages = PageReader(fetcher, self.cache, page_cache_ttl_seconds) if fetcher else None
        self.deep_deadline = deep_deadline_seconds
        # Page reads still running after a response was sent. Held so they are not garbage-collected.
        self._background: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        for task in self._background:
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def search(self, req: SearchRequest, request_id: str) -> SearchResponse:
        if req.mode == "deep" and self.pages is None:
            raise ApiError(501, "deep_mode_not_available", "Deep mode is not configured.")

        started = time.perf_counter()
        key = cache_key(self.provider.name, req)
        cached = await self.cache.get(key)
        if cached is not None:
            results = [SearchResult.model_validate(r) for r in cached["results"]]
        else:
            results = await self._run(req)
            ttl = self._cache_ttl(req, results)
            if ttl:
                payload = {"results": [r.model_dump(mode="json") for r in results]}
                await self.cache.set(key, payload, ttl)

        return SearchResponse(
            request_id=request_id,
            query=req.query,
            results=results,
            usage=Usage(credits=credits_for(req)),
            latency_ms=int((time.perf_counter() - started) * 1000),
            cached=cached is not None,
        )

    @staticmethod
    def _cache_ttl(req: SearchRequest, results: list[SearchResult]) -> int:
        """Seconds to cache this response; 0 means do not cache."""
        # Empty results are often a transient upstream problem; don't pin them for hours.
        if not results:
            return 0
        if req.mode == "fast":
            return ttl_for(req)
        if not any(r.fetch_status == "ok" for r in results):
            return 0
        if any(r.fetch_status == "timeout" for r in results):
            return min(ttl_for(req), PARTIAL_RESULT_TTL_SECONDS)
        return ttl_for(req)

    async def _run(self, req: SearchRequest) -> list[SearchResult]:
        try:
            hits = await self.provider.search(
                req.query,
                max_results=req.max_results,
                time_range=req.time_range,
                include_domains=req.include_domains,
                exclude_domains=req.exclude_domains,
            )
        except ProviderError as exc:
            raise ApiError(502, "upstream_search_failed", str(exc)) from exc

        selected = self._select(hits, req)
        if req.mode == "fast":
            return [self._fast_result(hit, domain, rank, req) for rank, (hit, domain) in enumerate(selected)]
        pages = await self._read_pages([hit.url for hit, _ in selected])
        return [
            self._deep_result(hit, domain, rank, req, page)
            for rank, ((hit, domain), page) in enumerate(zip(selected, pages))
        ]

    async def _read_pages(self, urls: list[str]) -> list[Page]:
        """Read pages concurrently until the deadline. Unfinished reads keep running in the
        background (bounded by the fetcher's own timeout) and fill the page cache."""
        assert self.pages is not None
        if not urls:
            return []
        tasks = [asyncio.create_task(self.pages.read(url)) for url in urls]
        done, pending = await asyncio.wait(tasks, timeout=self.deep_deadline)
        for task in pending:
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return [task.result() if task in done else Page(status="timeout") for task in tasks]

    @staticmethod
    def _select(hits: list[ProviderHit], req: SearchRequest) -> list[tuple[ProviderHit, str]]:
        """Apply domain filters and dedupe, keeping provider order, up to max_results."""
        seen: set[str] = set()
        out: list[tuple[ProviderHit, str]] = []
        for hit in hits:
            domain = domain_of(hit.url)
            if req.include_domains and not matches_domain(domain, req.include_domains):
                continue
            if req.exclude_domains and matches_domain(domain, req.exclude_domains):
                continue
            key = canonical_url(hit.url)
            if key in seen:
                continue
            seen.add(key)
            out.append((hit, domain))
            if len(out) >= req.max_results:
                break
        return out

    @staticmethod
    def _snippet_excerpts(hit: ProviderHit, req: SearchRequest) -> list[str]:
        return [hit.snippet[: req.max_chars_per_result]] if hit.snippet else []

    def _fast_result(self, hit: ProviderHit, domain: str, rank: int, req: SearchRequest) -> SearchResult:
        return SearchResult(
            title=hit.title,
            url=hit.url,
            domain=domain,
            published_at=hit.published_at,
            score=rank_score(rank),
            excerpts=self._snippet_excerpts(hit, req),
        )

    def _deep_result(
        self, hit: ProviderHit, domain: str, rank: int, req: SearchRequest, page: Page
    ) -> SearchResult:
        result = SearchResult(
            title=hit.title,
            url=hit.url,
            domain=domain,
            published_at=hit.published_at or page.published_at,
            score=rank_score(rank),
            excerpts=self._snippet_excerpts(hit, req),
            fetch_status=page.status,
            fetched_at=datetime.fromisoformat(page.fetched_at) if page.fetched_at else None,
        )
        if page.status != "ok":
            return result
        query_text = f"{req.query} {req.objective or ''}"
        result.excerpts = select_excerpts(page.blocks, query_text, req.max_chars_per_result)
        if req.include_raw_content:
            result.content = page.markdown[: req.max_chars_per_result]
        return result
