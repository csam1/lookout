import asyncio

import pytest
from fastapi.testclient import TestClient

from lookout.app import create_app
from lookout.cache import NullCache
from lookout.config import Settings
from lookout.db import Database
from lookout.fetch import FetchResult
from lookout.keys import create_key
from lookout.providers.fake import FakeProvider


class FakeFetcher:
    """Serves canned pages by URL. Unknown URLs return http_404."""

    def __init__(self, pages: dict[str, bytes | str | FetchResult] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        page = self.pages.get(url)
        if page is None:
            return FetchResult(url, url, "http_404")
        if isinstance(page, FetchResult):
            return page
        body = page.encode() if isinstance(page, str) else page
        return FetchResult(url, url, "ok", body, "utf-8")

    async def aclose(self) -> None:
        pass


class CountingProvider(FakeProvider):
    def __init__(self, hits=None) -> None:
        super().__init__(hits)
        self.calls = 0

    async def search(self, query, **kwargs):
        self.calls += 1
        return await super().search(query, **kwargs)


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/test.db"


def make_key(db_url: str, limit: int = 1000, name: str = "test", admin: bool = False) -> str:
    async def go() -> str:
        db = Database(db_url)
        await db.create_schema()
        async with db.sessions() as session:
            key, _ = await create_key(session, name, limit, is_admin=admin)
        await db.close()
        return key

    return asyncio.run(go())


def run_db(db_url: str, fn):
    """Run an async function(session) against the test database and return its result."""

    async def go():
        db = Database(db_url)
        async with db.sessions() as session:
            result = await fn(session)
        await db.close()
        return result

    return asyncio.run(go())


@pytest.fixture
def api(db_url):
    """Factory returning (client, auth_headers) for an app wired to test doubles."""
    clients: list[TestClient] = []

    def factory(provider=None, fetcher=None, cache=None, limit=1000, settings=None, admin=False):
        key = make_key(db_url, limit, name="admin" if admin else "test", admin=admin)
        settings = settings or Settings(database_url=db_url, redis_url="", search_provider="fake")
        app = create_app(
            settings,
            provider=provider or FakeProvider(),
            db=Database(db_url),
            cache=cache or NullCache(),
            fetcher=fetcher or FakeFetcher(),
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, {"Authorization": f"Bearer {key}"}

    yield factory
    for client in clients:
        client.__exit__(None, None, None)
