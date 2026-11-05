"""Deterministic provider for local development and tests. No network calls."""

from lookout.providers.base import ProviderHit


class FakeProvider:
    name = "fake"

    def __init__(self, hits: list[ProviderHit] | None = None) -> None:
        self._hits = hits

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        time_range: str,
        include_domains: list[str],
        exclude_domains: list[str],
    ) -> list[ProviderHit]:
        if self._hits is not None:
            return self._hits[:max_results]
        slug = "-".join(query.lower().split())[:60] or "query"
        return [
            ProviderHit(
                title=f"Fake result {i} for {query}",
                url=f"https://example{i}.com/{slug}",
                snippet=f"Fake snippet {i} about {query}. " * 3,
            )
            for i in range(1, max_results + 1)
        ]
