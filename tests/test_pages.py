import asyncio
import time

import httpx
import pytest
from conftest import FakeFetcher

from lookout.cache import MemoryCache
from lookout.config import Settings
from lookout.fetch import FetchResult, HttpFetcher
from lookout.pages import NEGATIVE_TTL_SECONDS, PageReader, page_key, ttl_for_status
from lookout.providers.base import ProviderHit
from lookout.providers.fake import FakeProvider
from lookout.service import PARTIAL_RESULT_TTL_SECONDS

# Each paragraph is longer than half a chunk, so they become separate passages.
ALPHA = "Alpha section explains how the predetermined change control plan works for model updates. " * 4
BETA = "Beta section covers the cybersecurity requirements for connected medical devices in detail. " * 4
PAGE = f"<html><body><main><p>{ALPHA}</p><p>{BETA}</p></main></body></html>"

FAST_HIT = ProviderHit(title="Fast", url="https://fast.example.com/a", snippet="fast snippet")
SLOW_HIT = ProviderHit(title="Slow", url="https://slow.example.com/b", snippet="slow snippet")


def deep(client, auth, **extra):
    body = {"query": "device guidance", "mode": "deep", **extra}
    return client.post("/v1/search", json=body, headers=auth)


# --- page cache --------------------------------------------------------------


def test_page_is_fetched_once_and_reused_across_objectives(api):
    fetcher = FakeFetcher({FAST_HIT.url: PAGE})
    client, auth = api(provider=FakeProvider([FAST_HIT]), fetcher=fetcher, cache=MemoryCache())

    first = deep(client, auth, objective="change control plan").json()["results"][0]
    second = deep(client, auth, objective="cybersecurity requirements").json()["results"][0]

    assert fetcher.calls == [FAST_HIT.url]  # second query read the page from cache
    assert first["excerpts"][0].startswith("Alpha")
    assert second["excerpts"][0].startswith("Beta")  # excerpts still follow each objective
    assert second["fetch_status"] == "ok"
    assert second["fetched_at"] == first["fetched_at"]  # time of the original fetch


@pytest.mark.parametrize(
    "status,ttl",
    [
        ("ok", 6 * 3600),
        ("robots_disallowed", NEGATIVE_TTL_SECONDS),
        ("http_403", NEGATIVE_TTL_SECONDS),
        ("http_404", NEGATIVE_TTL_SECONDS),
        ("blocked:non_public_address", NEGATIVE_TTL_SECONDS),
        ("empty_content", NEGATIVE_TTL_SECONDS),
        ("http_429", 0),
        ("http_503", 0),
        ("timeout", 0),
        ("error", 0),
    ],
)
def test_page_cache_ttl_by_status(status, ttl):
    assert ttl_for_status(status, ok_ttl=6 * 3600) == ttl


@pytest.mark.anyio
async def test_transient_failures_are_not_cached():
    cache = MemoryCache()
    url = "https://x.example.com/"
    reader = PageReader(FakeFetcher({url: FetchResult(url, url, "timeout")}), cache)
    page = await reader.read(url)
    assert page.status == "timeout"
    assert cache.store == {}


@pytest.mark.anyio
async def test_permanent_failures_are_cached_briefly():
    cache = MemoryCache()
    url = "https://x.example.com/"
    reader = PageReader(FakeFetcher({url: FetchResult(url, url, "robots_disallowed")}), cache)
    await reader.read(url)
    assert cache.ttls[page_key(url)] == NEGATIVE_TTL_SECONDS


@pytest.mark.anyio
async def test_reader_never_raises():
    class Exploding:
        async def fetch(self, url):
            raise RuntimeError("boom")

    page = await PageReader(Exploding()).read("https://x.example.com/")
    assert page.status == "error"


# --- deadline ----------------------------------------------------------------


class SlowFetcher(FakeFetcher):
    def __init__(self, pages, slow_url, delay):
        super().__init__(pages)
        self.slow_url = slow_url
        self.delay = delay

    async def fetch(self, url):
        if url == self.slow_url:
            await asyncio.sleep(self.delay)
        return await super().fetch(url)


def test_deadline_returns_ready_pages_and_finishes_slow_ones_in_background(api, db_url):
    cache = MemoryCache()
    fetcher = SlowFetcher({FAST_HIT.url: PAGE, SLOW_HIT.url: PAGE}, SLOW_HIT.url, delay=0.6)
    settings = Settings(database_url=db_url, redis_url="", search_provider="fake", deep_deadline_seconds=0.2)
    client, auth = api(provider=FakeProvider([FAST_HIT, SLOW_HIT]), fetcher=fetcher, cache=cache, settings=settings)

    started = time.perf_counter()
    body = deep(client, auth).json()
    elapsed = time.perf_counter() - started

    fast, slow = body["results"]
    assert elapsed < 0.55  # did not wait for the slow page
    assert fast["fetch_status"] == "ok"
    assert slow["fetch_status"] == "timeout"
    assert slow["excerpts"] == ["slow snippet"]

    # The partial response is cached only briefly.
    response_keys = [k for k in cache.store if k.startswith("lo:search:")]
    assert [cache.ttls[k] for k in response_keys] == [PARTIAL_RESULT_TTL_SECONDS]

    # The slow page keeps loading after the response and lands in the page cache.
    deadline = time.time() + 3
    while page_key(SLOW_HIT.url) not in cache.store and time.time() < deadline:
        time.sleep(0.05)
    assert cache.store[page_key(SLOW_HIT.url)]["status"] == "ok"


def test_complete_deep_response_gets_full_ttl(api):
    cache = MemoryCache()
    client, auth = api(provider=FakeProvider([FAST_HIT]), fetcher=FakeFetcher({FAST_HIT.url: PAGE}), cache=cache)
    deep(client, auth)
    response_keys = [k for k in cache.store if k.startswith("lo:search:")]
    assert [cache.ttls[k] for k in response_keys] == [24 * 3600]


# --- robots.txt in the shared store ----------------------------------------------


async def public_resolve(host, port):
    return ["93.184.216.34"]


def make_fetcher(handler, store):
    return HttpFetcher(
        timeout=2.0, max_bytes=100_000, concurrency=4, user_agent="lookout-bot/0.1",
        resolver=public_resolve, transport=httpx.MockTransport(handler), store=store,
    )


@pytest.mark.anyio
async def test_robots_rules_are_shared_between_fetchers():
    calls = {"robots": 0}

    def handler(req):
        if req.url.path == "/robots.txt":
            calls["robots"] += 1
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    store = MemoryCache()
    first, second = make_fetcher(handler, store), make_fetcher(handler, store)  # e.g. two workers
    assert (await first.fetch("https://site.test/ok")).status == "ok"
    assert (await second.fetch("https://site.test/private/x")).status == "robots_disallowed"
    await first.aclose()
    await second.aclose()
    assert calls["robots"] == 1


@pytest.mark.anyio
async def test_unreachable_robots_means_disallow_with_short_ttl():
    def handler(req):
        if req.url.path == "/robots.txt":
            raise httpx.ConnectError("reset")
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    store = MemoryCache()
    f = make_fetcher(handler, store)
    result = await f.fetch("https://site.test/page")
    await f.aclose()
    assert result.status == "robots_disallowed"
    assert store.ttls["lo:robots:v1:https://site.test"] == 300
