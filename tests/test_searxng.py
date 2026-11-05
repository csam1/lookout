import httpx
import pytest

from lookout.app import build_provider
from lookout.config import Settings
from lookout.providers.base import ProviderError
from lookout.providers.searxng import SearxngProvider

SAMPLE = {
    "query": "x",
    "results": [
        {"title": "FDA AI", "url": "https://www.fda.gov/ai", "content": "AI device guidance.",
         "publishedDate": "2026-03-14T00:00:00"},
        {"title": "No snippet", "url": "https://example.com/a", "content": None},
        {"title": "Dup", "url": "https://example.com/a/", "content": "dup"},
        {"title": "Missing url", "content": "skip me"},
    ],
}


def provider_with(handler) -> SearxngProvider:
    return SearxngProvider("http://searx.test", transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_maps_searxng_results_and_sends_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["path"] = request.url.path
        return httpx.Response(200, json=SAMPLE)

    p = provider_with(handler)
    hits = await p.search("x", max_results=5, time_range="month", include_domains=[], exclude_domains=[])
    await p.aclose()

    assert seen["path"] == "/search"
    assert seen["params"]["format"] == "json"
    assert seen["params"]["q"] == "x"
    assert seen["params"]["time_range"] == "month"
    assert [h.url for h in hits] == ["https://www.fda.gov/ai", "https://example.com/a", "https://example.com/a/"]
    assert hits[0].published_at == "2026-03-14T00:00:00"
    assert hits[1].snippet == ""


@pytest.mark.anyio
async def test_any_time_range_sends_no_filter():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": []})

    p = provider_with(handler)
    await p.search("x", max_results=5, time_range="any", include_domains=[], exclude_domains=[])
    await p.aclose()
    assert "time_range" not in seen["params"]


@pytest.mark.anyio
async def test_http_error_raises_provider_error():
    p = provider_with(lambda r: httpx.Response(429))
    with pytest.raises(ProviderError, match="429"):
        await p.search("x", max_results=5, time_range="any", include_domains=[], exclude_domains=[])
    await p.aclose()


@pytest.mark.anyio
async def test_non_json_raises_provider_error():
    p = provider_with(lambda r: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(ProviderError, match="format=json"):
        await p.search("x", max_results=5, time_range="any", include_domains=[], exclude_domains=[])
    await p.aclose()


@pytest.mark.anyio
async def test_connection_error_raises_provider_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    p = provider_with(handler)
    with pytest.raises(ProviderError, match="request failed"):
        await p.search("x", max_results=5, time_range="any", include_domains=[], exclude_domains=[])
    await p.aclose()


def test_api_surfaces_searxng_outage_as_502(api):
    # Point the app at a SearXNG that is not running; the failure must surface as a 502 error body.
    client, auth = api(provider=SearxngProvider("http://127.0.0.1:9"))
    r = client.post("/v1/search", json={"query": "x"}, headers=auth)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_search_failed"


def test_build_provider_selects_searxng():
    settings = Settings(search_provider="searxng", searxng_base_url="http://searx.test")
    assert isinstance(build_provider(settings), SearxngProvider)
