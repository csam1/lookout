import pytest
from sqlalchemy import func, select, update

from conftest import CountingProvider, run_db
from lookout.app import create_app
from lookout.cache import MemoryCache
from lookout.config import Settings
from lookout.db import ApiKey, UsageEvent
from lookout.providers.base import ProviderError, ProviderHit
from lookout.providers.fake import FakeProvider


def post(client, body, headers):
    return client.post("/v1/search", json=body, headers=headers)


# --- auth -------------------------------------------------------------------


def test_healthz_needs_no_key(api):
    client, _ = api()
    assert client.get("/healthz").status_code == 200


def test_missing_key_returns_error_body_with_request_id(api):
    client, _ = api()
    r = post(client, {"query": "x"}, {})
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "unauthorized"
    assert err["request_id"].startswith("req_")
    assert r.headers["X-Request-ID"] == err["request_id"]


def test_wrong_key_rejected(api):
    client, _ = api()
    assert post(client, {"query": "x"}, {"X-API-Key": "lo_nope"}).status_code == 401


def test_x_api_key_header_accepted(api):
    client, auth = api()
    key = auth["Authorization"].removeprefix("Bearer ")
    assert post(client, {"query": "x"}, {"X-API-Key": key}).status_code == 200


def test_revoked_key_rejected(api, db_url):
    client, auth = api()

    async def revoke(session):
        await session.execute(update(ApiKey).values(active=False))
        await session.commit()

    run_db(db_url, revoke)
    assert post(client, {"query": "x"}, auth).status_code == 401


def test_auth_runs_before_body_validation(api):
    client, _ = api()
    r = post(client, {"query": ""}, {})
    assert r.status_code == 401


# --- fast mode contract -----------------------------------------------------


def test_fast_search_returns_contract_shape(api):
    client, auth = api()
    r = post(client, {"query": "fda ai devices", "max_results": 3}, auth)
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "fda ai devices"
    assert len(body["results"]) == 3
    assert body["usage"] == {"credits": 1}
    assert body["cached"] is False
    first = body["results"][0]
    assert first["content"] is None
    assert first["fetched_at"] is None
    assert first["fetch_status"] is None


def test_excerpts_respect_max_chars_per_result(api):
    client, auth = api()
    r = post(client, {"query": "x", "max_chars_per_result": 100}, auth)
    for result in r.json()["results"]:
        assert sum(len(e) for e in result["excerpts"]) <= 100


def test_include_and_exclude_domains(api):
    hits = [
        ProviderHit(title="a", url="https://www.fda.gov/a", snippet="s"),
        ProviderHit(title="b", url="https://sub.fda.gov/b", snippet="s"),
        ProviderHit(title="c", url="https://news.example.com/c", snippet="s"),
        ProviderHit(title="d", url="https://fda.gov.evil.com/d", snippet="s"),
    ]
    client, auth = api(provider=FakeProvider(hits))

    r = post(client, {"query": "x", "include_domains": ["fda.gov"]}, auth)
    assert [x["domain"] for x in r.json()["results"]] == ["fda.gov", "sub.fda.gov"]

    r = post(client, {"query": "x", "exclude_domains": ["example.com"]}, auth)
    assert "news.example.com" not in [x["domain"] for x in r.json()["results"]]


def test_duplicate_urls_are_removed(api):
    hits = [
        ProviderHit(title="a", url="https://www.example.com/page/", snippet="s"),
        ProviderHit(title="a2", url="https://example.com/page", snippet="s"),
        ProviderHit(title="b", url="https://example.com/other", snippet="s"),
    ]
    client, auth = api(provider=FakeProvider(hits))
    assert len(post(client, {"query": "x"}, auth).json()["results"]) == 2


def test_validation_error_uses_error_format(api):
    client, auth = api()
    r = post(client, {"query": ""}, auth)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_request"
    assert "query" in err["message"]


def test_max_results_bounds_enforced(api):
    client, auth = api()
    assert post(client, {"query": "x", "max_results": 21}, auth).status_code == 422


class BrokenProvider:
    name = "broken"

    async def search(self, query, **kwargs):
        raise ProviderError("upstream timeout")


def test_provider_failure_is_502_and_not_billed(api, db_url):
    client, auth = api(provider=BrokenProvider())
    r = post(client, {"query": "x"}, auth)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_search_failed"

    async def count(session):
        return (await session.execute(select(func.count(UsageEvent.id)))).scalar_one()

    assert run_db(db_url, count) == 0


def test_unknown_provider_fails_at_startup():
    with pytest.raises(RuntimeError):
        create_app(Settings(search_provider="nope", redis_url=""))


# --- usage and quota --------------------------------------------------------


def test_usage_is_recorded_per_call(api, db_url):
    client, auth = api()
    post(client, {"query": "one"}, auth)
    post(client, {"query": "two", "mode": "deep"}, auth)

    async def events(session):
        rows = (await session.execute(select(UsageEvent).order_by(UsageEvent.id))).scalars()
        return [(e.mode, e.credits, e.endpoint) for e in rows]

    assert run_db(db_url, events) == [("fast", 1, "search"), ("deep", 2, "search")]


def test_quota_exceeded_returns_429(api):
    client, auth = api(limit=2)
    assert post(client, {"query": "a"}, auth).status_code == 200
    assert post(client, {"query": "b"}, auth).status_code == 200
    r = post(client, {"query": "c"}, auth)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "quota_exceeded"


def test_quota_accounts_for_deep_cost(api):
    client, auth = api(limit=1)
    r = post(client, {"query": "a", "mode": "deep"}, auth)
    assert r.status_code == 429


# --- cache ------------------------------------------------------------------


def test_repeat_query_is_served_from_cache_and_still_billed(api, db_url):
    provider = CountingProvider()
    client, auth = api(provider=provider, cache=MemoryCache())

    first = post(client, {"query": "Same  Query"}, auth).json()
    second = post(client, {"query": "same query"}, auth).json()  # normalised to the same key

    assert provider.calls == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["results"] == first["results"]
    assert second["request_id"] != first["request_id"]

    async def credits(session):
        return (await session.execute(select(func.sum(UsageEvent.credits)))).scalar_one()

    assert run_db(db_url, credits) == 2


def test_different_params_do_not_share_cache(api):
    provider = CountingProvider()
    client, auth = api(provider=provider, cache=MemoryCache())
    post(client, {"query": "q", "max_results": 3}, auth)
    post(client, {"query": "q", "max_results": 4}, auth)
    assert provider.calls == 2


def test_empty_results_are_not_cached(api):
    provider = CountingProvider(hits=[])
    cache = MemoryCache()
    client, auth = api(provider=provider, cache=cache)
    post(client, {"query": "nothing"}, auth)
    post(client, {"query": "nothing"}, auth)
    assert provider.calls == 2
    assert cache.store == {}
