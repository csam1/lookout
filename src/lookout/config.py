from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Loaded from environment variables with the LOOKOUT_ prefix."""

    model_config = SettingsConfigDict(env_prefix="LOOKOUT_", env_file=".env", extra="ignore")

    # Storage
    database_url: str = "postgresql+asyncpg://websearch:websearch@localhost:5432/websearch"
    # Create tables on startup. Fine for local/dev; replace with migrations before a shared deploy.
    auto_create_schema: bool = True
    # Empty string disables the result cache.
    redis_url: str = "redis://localhost:6379/0"

    # Search provider: "fake" (tests/dev) or "searxng" (free, self-hosted).
    search_provider: str = "fake"
    searxng_base_url: str = "http://localhost:8080"

    # Deep mode: a request waits at most this long for pages, then answers with what it has.
    # Pages still loading keep going in the background (up to fetch_timeout_seconds) and fill
    # the page cache for later queries.
    deep_deadline_seconds: float = 3.0
    page_cache_ttl_seconds: int = 6 * 3600

    # Deep mode page fetching
    fetch_timeout_seconds: float = 5.0
    fetch_max_bytes: int = 2_000_000
    fetch_concurrency: int = 10
    respect_robots_txt: bool = True
    user_agent: str = "lookout-bot/0.1 (internal; +https://example.invalid/bot)"

    # New API keys get this monthly credit limit unless one is given.
    default_monthly_credit_limit: int = 10_000
