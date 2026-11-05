"""SearXNG provider: free, self-hosted metasearch. No per-query fee or API key.

Requires the instance to have the JSON output format enabled (see docker/searxng/settings.yml).
"""

import httpx

from lookout.providers.base import ProviderError, ProviderHit

# SearXNG accepts day | week | month | year. "any" means no filter.
_TIME_RANGES = {"day": "day", "week": "week", "month": "month", "year": "year"}


class SearxngProvider:
    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        time_range: str,
        include_domains: list[str],
        exclude_domains: list[str],
    ) -> list[ProviderHit]:
        params = {"q": query, "format": "json", "categories": "general", "safesearch": "1"}
        if time_range in _TIME_RANGES:
            params["time_range"] = _TIME_RANGES[time_range]

        try:
            resp = await self._client.get("/search", params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"SearXNG request failed: {exc}") from exc

        if resp.status_code != 200:
            raise ProviderError(f"SearXNG returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("SearXNG returned non-JSON; is format=json enabled?") from exc

        hits: list[ProviderHit] = []
        for item in data.get("results", []):
            url = item.get("url")
            if not url:
                continue
            hits.append(
                ProviderHit(
                    title=item.get("title") or url,
                    url=url,
                    snippet=item.get("content") or "",
                    published_at=item.get("publishedDate") or None,
                )
            )
        # Domain filters run in SearchService, so return extra hits to let filtering still fill max_results.
        return hits[: max_results * 2]
