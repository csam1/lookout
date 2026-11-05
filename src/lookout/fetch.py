"""SSRF-safe page fetcher for deep mode.

Guards, applied to the first URL and to every redirect hop:
- scheme must be http or https, port must be 80 or 443, no credentials in the URL
- every address the hostname resolves to must be public (no private, loopback,
  link-local, CGNAT, multicast or reserved ranges, including IPv4-mapped IPv6)
- redirects are followed manually, at most MAX_REDIRECTS
- only HTML / plain-text responses, capped at max_bytes, within a total time budget
- robots.txt is honoured (RFC 9309: 4xx = allow; 5xx or unreachable = disallow), cached per
  origin in process memory and, when a shared store is given, in Redis

Known gap: httpx resolves the hostname again when connecting, so a DNS-rebinding
attacker could pass the check and then point at an internal address. Close this
before exposing the service publicly (egress proxy or pinned-IP transport; see PLAN.md).
"""

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from lookout.cache import ResultCache

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
ALLOWED_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
MAX_REDIRECTS = 5
ROBOTS_TTL_SECONDS = 6 * 3600  # RFC 9309: should not be reused for more than 24 h
ROBOTS_ERROR_TTL_SECONDS = 300  # unreachable robots.txt: disallow, but retry soon
ROBOTS_MAX_BYTES = 500_000
ROBOTS_CACHE_MAX = 10_000
ROBOTS_KEY = "lo:robots:v1:"

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchBlocked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class FetchResult:
    url: str
    final_url: str
    # ok | blocked:<reason> | robots_disallowed | timeout | http_<code> | too_large
    # | unsupported_type | too_many_redirects | error
    status: str
    body: bytes | None = None
    charset: str | None = None


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...
    async def aclose(self) -> None: ...


def ip_is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def check_url(url: str, resolver: Resolver) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise FetchBlocked("scheme")
    if parts.username or parts.password:
        raise FetchBlocked("credentials_in_url")
    host = parts.hostname
    if not host:
        raise FetchBlocked("no_host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise FetchBlocked("bad_port") from None
    if port not in ALLOWED_PORTS:
        raise FetchBlocked("port")
    try:
        addresses = await resolver(host, port)
    except (socket.gaierror, OSError):
        raise FetchBlocked("dns_failed") from None
    if not addresses:
        raise FetchBlocked("dns_failed")
    # Reject if ANY address is internal: an attacker-controlled name can mix public and private records.
    if not all(ip_is_public(a) for a in addresses):
        raise FetchBlocked("non_public_address")


class HttpFetcher:
    def __init__(
        self,
        *,
        timeout: float,
        max_bytes: int,
        concurrency: int,
        user_agent: str,
        respect_robots: bool = True,
        resolver: Resolver = system_resolve,
        transport: httpx.AsyncBaseTransport | None = None,
        store: ResultCache | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._user_agent = user_agent
        self._respect_robots = respect_robots
        self._resolver = resolver
        self._store = store
        self._sem = asyncio.Semaphore(concurrency)
        self._robots: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            # Never route through an env-configured proxy: the IP checks above would not apply to it.
            trust_env=False,
            transport=transport,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        async with self._sem:
            try:
                return await asyncio.wait_for(self._fetch(url), timeout=self._timeout)
            except TimeoutError:
                return FetchResult(url, url, "timeout")
            except httpx.TimeoutException:
                return FetchResult(url, url, "timeout")
            except httpx.HTTPError:
                return FetchResult(url, url, "error")

    async def _fetch(self, url: str) -> FetchResult:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                await check_url(current, self._resolver)
            except FetchBlocked as exc:
                return FetchResult(url, current, f"blocked:{exc.reason}")
            if self._respect_robots and not await self._robots_allowed(current):
                return FetchResult(url, current, "robots_disallowed")

            async with self._client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return FetchResult(url, current, f"http_{resp.status_code}")
                    current = urljoin(current, location)
                    continue
                if resp.status_code != 200:
                    return FetchResult(url, current, f"http_{resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if ctype not in ALLOWED_TYPES:
                    return FetchResult(url, current, "unsupported_type")
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self._max_bytes:
                    return FetchResult(url, current, "too_large")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self._max_bytes:
                        return FetchResult(url, current, "too_large")
                return FetchResult(url, current, "ok", bytes(buf), resp.charset_encoding)
        return FetchResult(url, current, "too_many_redirects")

    async def _robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        now = time.monotonic()
        cached = self._robots.get(origin)
        if cached is None or cached[0] < now:
            if len(self._robots) >= ROBOTS_CACHE_MAX:
                self._robots.clear()
            rules, ttl = await self._robots_rules(origin)
            cached = (now + ttl, _parser_from(rules))
            self._robots[origin] = cached
        parser = cached[1]
        return True if parser is None else parser.can_fetch(self._user_agent, url)

    async def _robots_rules(self, origin: str) -> tuple[dict, int]:
        """Rules for an origin from the shared store, or fetched and stored. Returns (rules, ttl)."""
        key = ROBOTS_KEY + origin
        if self._store is not None:
            stored = await self._store.get(key)
            if stored is not None:
                return stored, stored.get("ttl", ROBOTS_TTL_SECONDS)
        rules = await self._load_robots(origin)
        ttl = ROBOTS_ERROR_TTL_SECONDS if rules.get("error") else ROBOTS_TTL_SECONDS
        rules["ttl"] = ttl
        if self._store is not None:
            await self._store.set(key, rules, ttl)
        return rules, ttl

    async def _load_robots(self, origin: str) -> dict:
        """Fetch robots.txt. Returns {"mode": "allow_all" | "disallow_all" | "rules", ...}."""
        robots_url = origin + "/robots.txt"
        try:
            await check_url(robots_url, self._resolver)
        except FetchBlocked:
            return {"mode": "allow_all"}  # the page itself fails the same check
        try:
            resp = await self._client.get(robots_url, timeout=min(2.0, self._timeout))
        except httpx.HTTPError:
            return {"mode": "disallow_all", "error": True}  # RFC 9309: unreachable = disallow
        if resp.status_code >= 500:
            return {"mode": "disallow_all", "error": True}
        if resp.status_code != 200:
            return {"mode": "allow_all"}  # 3xx not followed; 4xx means unrestricted
        text = resp.content[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace")
        return {"mode": "rules", "text": text}


def _parser_from(rules: dict) -> RobotFileParser | None:
    """None means no restrictions."""
    mode = rules.get("mode")
    if mode == "allow_all":
        return None
    parser = RobotFileParser()
    if mode == "rules":
        parser.parse(rules.get("text", "").splitlines())
    else:
        parser.parse(["User-agent: *", "Disallow: /"])
    return parser
