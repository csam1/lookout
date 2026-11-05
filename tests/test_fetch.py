import httpx
import pytest

from lookout.fetch import FetchBlocked, HttpFetcher, check_url, ip_is_public

DNS = {
    "public.test": ["93.184.216.34"],
    "other.test": ["93.184.216.35"],
    "internal.test": ["10.0.0.5"],
    "mixed.test": ["93.184.216.34", "127.0.0.1"],
    "metadata.test": ["169.254.169.254"],
}


async def fake_resolve(host: str, port: int) -> list[str]:
    if host not in DNS:
        raise OSError("NXDOMAIN")
    return DNS[host]


HTML = "<html><body><p>Hello from the public page, long enough.</p></body></html>"


def fetcher(handler, *, max_bytes=10_000, robots=True) -> HttpFetcher:
    return HttpFetcher(
        timeout=2.0,
        max_bytes=max_bytes,
        concurrency=4,
        user_agent="lookout-bot/0.1",
        respect_robots=robots,
        resolver=fake_resolve,
        transport=httpx.MockTransport(handler),
    )


def html(body=HTML, status=200, **headers):
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


# --- address checks ---------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
     "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "224.0.0.1"],
)
def test_internal_addresses_are_not_public(address):
    assert not ip_is_public(address)


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_public(address):
    assert ip_is_public(address)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url,reason",
    [
        ("ftp://public.test/file", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("http://user:pw@public.test/", "credentials_in_url"),
        ("http://public.test:8080/", "port"),
        ("http://internal.test/", "non_public_address"),
        ("http://mixed.test/", "non_public_address"),
        ("http://metadata.test/latest/meta-data", "non_public_address"),
        ("http://nowhere.test/", "dns_failed"),
    ],
)
async def test_check_url_blocks(url, reason):
    with pytest.raises(FetchBlocked) as exc:
        await check_url(url, fake_resolve)
    assert exc.value.reason == reason


# --- fetching ---------------------------------------------------------------


@pytest.mark.anyio
async def test_fetches_public_html():
    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else html()

    f = fetcher(handler)
    result = await f.fetch("https://public.test/page")
    await f.aclose()
    assert result.status == "ok"
    assert b"Hello from the public page" in result.body
    assert result.charset == "utf-8"


@pytest.mark.anyio
async def test_redirect_to_internal_host_is_blocked():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://internal.test/admin"})

    f = fetcher(handler)
    result = await f.fetch("https://public.test/start")
    await f.aclose()
    assert result.status == "blocked:non_public_address"
    assert result.final_url == "http://internal.test/admin"


@pytest.mark.anyio
async def test_relative_redirect_is_followed():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return html()

    f = fetcher(handler)
    result = await f.fetch("https://public.test/old")
    await f.aclose()
    assert result.status == "ok"
    assert result.final_url == "https://public.test/new"


@pytest.mark.anyio
async def test_redirect_loop_stops():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "/loop"})

    f = fetcher(handler)
    result = await f.fetch("https://public.test/loop")
    await f.aclose()
    assert result.status == "too_many_redirects"


@pytest.mark.anyio
async def test_oversized_body_is_rejected():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return html("x" * 5_000)

    f = fetcher(handler, max_bytes=1_000)
    result = await f.fetch("https://public.test/big")
    await f.aclose()
    assert result.status == "too_large"
    assert result.body is None


@pytest.mark.anyio
async def test_non_html_is_rejected():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})

    f = fetcher(handler)
    result = await f.fetch("https://public.test/doc.pdf")
    await f.aclose()
    assert result.status == "unsupported_type"


@pytest.mark.anyio
async def test_http_error_status_is_reported():
    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else html(status=403)

    f = fetcher(handler)
    result = await f.fetch("https://public.test/private")
    await f.aclose()
    assert result.status == "http_403"


@pytest.mark.anyio
async def test_robots_disallow_is_respected_and_cached():
    calls = {"robots": 0}

    def handler(req):
        if req.url.path == "/robots.txt":
            calls["robots"] += 1
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return html()

    f = fetcher(handler)
    blocked = await f.fetch("https://public.test/private/page")
    allowed = await f.fetch("https://public.test/public/page")
    await f.aclose()
    assert blocked.status == "robots_disallowed"
    assert allowed.status == "ok"
    assert calls["robots"] == 1


@pytest.mark.anyio
async def test_robots_server_error_means_disallow():
    def handler(req):
        return httpx.Response(503) if req.url.path == "/robots.txt" else html()

    f = fetcher(handler)
    result = await f.fetch("https://public.test/page")
    await f.aclose()
    assert result.status == "robots_disallowed"


@pytest.mark.anyio
async def test_robots_can_be_disabled():
    def handler(req):
        return httpx.Response(503) if req.url.path == "/robots.txt" else html()

    f = fetcher(handler, robots=False)
    result = await f.fetch("https://public.test/page")
    await f.aclose()
    assert result.status == "ok"
