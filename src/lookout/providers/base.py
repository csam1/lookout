"""Search provider interface. Every upstream search source implements this."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderHit:
    title: str
    url: str
    snippet: str
    published_at: str | None = None


class SearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        time_range: str,
        include_domains: list[str],
        exclude_domains: list[str],
    ) -> list[ProviderHit]:
        """Return up to max_results hits. Raise ProviderError on upstream failure."""
        ...


class ProviderError(Exception):
    """Upstream provider failed (network, quota, bad response)."""
