import asyncio
import logging

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import make_key
from lookout.db import Database


@pytest.fixture
def admin(api):
    """(client, admin_headers, agent_headers): an admin key and a normal key on one app."""
    client, admin_auth = api(admin=True)
    return client, admin_auth


def agent_headers(db_url, limit=1000):
    return {"Authorization": f"Bearer {make_key(db_url, limit, name='agent')}"}


# --- access control ----------------------------------------------------------


def test_admin_endpoints_reject_normal_keys(api, db_url):
    client, auth = api()
    for method, path in (
        ("get", "/v1/admin/keys"),
        ("post", "/v1/admin/keys"),
        ("patch", "/v1/admin/keys/1"),
        ("post", "/v1/admin/keys/1/revoke"),
        ("get", "/v1/admin/usage"),
    ):
        kwargs = {} if method == "get" else {"json": {}}
        r = getattr(client, method)(path, headers=auth, **kwargs)
        assert r.status_code == 403, (method, path)
        assert r.json()["error"]["code"] == "forbidden"


def test_admin_endpoints_require_a_key(admin):
    client, _ = admin
    assert client.get("/v1/admin/keys").status_code == 401


def test_admin_key_can_also_search(admin):
    client, auth = admin
    assert client.post("/v1/search", json={"query": "x"}, headers=auth).status_code == 200


# --- keys -------------------------------------------------------------------------


def test_list_keys_includes_month_usage(admin, db_url):
    client, auth = admin
    agent = agent_headers(db_url)
    client.post("/v1/search", json={"query": "a"}, headers=agent)
    client.post("/v1/search", json={"query": "b", "mode": "deep"}, headers=agent)

    keys = {k["name"]: k for k in client.get("/v1/admin/keys", headers=auth).json()}
    assert keys["agent"]["calls_this_month"] == 2
    assert keys["agent"]["credits_this_month"] == 3
    assert keys["agent"]["last_used_at"].endswith(("Z", "+00:00"))  # explicit UTC for browsers
    assert keys["admin"]["is_admin"] is True
    assert keys["admin"]["last_used_at"] is None
    assert "key_hash" not in keys["agent"]


def test_create_key_returns_plaintext_once_and_it_works(admin):
    client, auth = admin
    r = client.post("/v1/admin/keys", json={"name": "  billing-agent ", "monthly_credit_limit": 50}, headers=auth)
    assert r.status_code == 201
    created = r.json()
    assert created["key"]["name"] == "billing-agent"
    assert created["key"]["monthly_credit_limit"] == 50
    assert created["key"]["is_admin"] is False
    new_key = created["api_key"]
    assert new_key.startswith(created["key"]["key_prefix"])

    # The new key searches, but is not an admin.
    new_auth = {"Authorization": f"Bearer {new_key}"}
    assert client.post("/v1/search", json={"query": "x"}, headers=new_auth).status_code == 200
    assert client.get("/v1/admin/keys", headers=new_auth).status_code == 403
    # The plaintext never appears in listings.
    assert new_key not in client.get("/v1/admin/keys", headers=auth).text


def test_create_key_uses_default_limit(admin):
    client, auth = admin
    r = client.post("/v1/admin/keys", json={"name": "x"}, headers=auth)
    assert r.json()["key"]["monthly_credit_limit"] == 10_000


@pytest.mark.parametrize("body", [{"name": ""}, {"name": "   "}, {"name": "x", "monthly_credit_limit": 0}])
def test_create_key_validates(admin, body):
    client, auth = admin
    r = client.post("/v1/admin/keys", json=body, headers=auth)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_update_name_and_limit(admin, db_url):
    client, auth = admin
    make_key(db_url, 100, name="agent")
    agent = next(k for k in client.get("/v1/admin/keys", headers=auth).json() if k["name"] == "agent")

    r = client.patch(f"/v1/admin/keys/{agent['id']}", json={"name": "agent-v2", "monthly_credit_limit": 999}, headers=auth)
    assert r.status_code == 200
    assert r.json()["name"] == "agent-v2"
    assert r.json()["monthly_credit_limit"] == 999


def test_raising_the_limit_lifts_a_quota_block(admin, db_url):
    client, auth = admin
    agent = agent_headers(db_url, limit=1)
    assert client.post("/v1/search", json={"query": "a"}, headers=agent).status_code == 200
    assert client.post("/v1/search", json={"query": "b"}, headers=agent).status_code == 429

    agent_id = next(k["id"] for k in client.get("/v1/admin/keys", headers=auth).json() if k["name"] == "agent")
    client.patch(f"/v1/admin/keys/{agent_id}", json={"monthly_credit_limit": 10}, headers=auth)
    assert client.post("/v1/search", json={"query": "b"}, headers=agent).status_code == 200


def test_update_unknown_key_is_404(admin):
    client, auth = admin
    r = client.patch("/v1/admin/keys/9999", json={"name": "x"}, headers=auth)
    assert r.status_code == 404


def test_revoke_stops_the_key_and_is_permanent(admin, db_url):
    client, auth = admin
    agent = agent_headers(db_url)
    agent_id = next(k["id"] for k in client.get("/v1/admin/keys", headers=auth).json() if k["name"] == "agent")

    r = client.post(f"/v1/admin/keys/{agent_id}/revoke", headers=auth)
    assert r.status_code == 200
    assert r.json()["active"] is False
    assert client.post("/v1/search", json={"query": "x"}, headers=agent).status_code == 401
    # Revoking again is harmless; there is no way to re-activate.
    assert client.post(f"/v1/admin/keys/{agent_id}/revoke", headers=auth).json()["active"] is False


def test_last_active_admin_cannot_be_revoked(admin):
    client, auth = admin
    admin_id = next(k["id"] for k in client.get("/v1/admin/keys", headers=auth).json() if k["is_admin"])
    r = client.post(f"/v1/admin/keys/{admin_id}/revoke", headers=auth)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "last_admin"


def test_admin_can_be_revoked_when_another_admin_exists(admin):
    client, auth = admin
    second = client.post("/v1/admin/keys", json={"name": "second-admin", "is_admin": True}, headers=auth).json()
    r = client.post(f"/v1/admin/keys/{second['key']['id']}/revoke", headers=auth)
    assert r.status_code == 200


def test_admin_actions_are_audited(admin, caplog):
    client, auth = admin
    with caplog.at_level(logging.INFO, logger="lookout.audit"):
        created = client.post("/v1/admin/keys", json={"name": "audited"}, headers=auth).json()
        client.patch(f"/v1/admin/keys/{created['key']['id']}", json={"monthly_credit_limit": 5}, headers=auth)
        client.post(f"/v1/admin/keys/{created['key']['id']}/revoke", headers=auth)
    messages = " ".join(r.getMessage() for r in caplog.records if r.name == "lookout.audit")
    for action in ("create_key", "update_key", "revoke_key"):
        assert action in messages
    assert created["api_key"] not in messages  # never log the plaintext key


# --- usage ------------------------------------------------------------------------


def test_usage_totals_and_daily_series(admin, db_url):
    client, auth = admin
    agent = agent_headers(db_url)
    client.post("/v1/search", json={"query": "a"}, headers=agent)
    client.post("/v1/search", json={"query": "b", "mode": "deep"}, headers=agent)

    body = client.get("/v1/admin/usage?days=7", headers=auth).json()
    assert body["calls"] == 2
    assert body["credits"] == 3
    assert body["keys_used"] == 1
    assert body["active_keys"] == 2
    assert body["period_start"].endswith(("Z", "+00:00"))
    assert len(body["daily"]) == 7
    assert [d["date"] for d in body["daily"]] == sorted(d["date"] for d in body["daily"])
    assert body["daily"][-1]["credits"] == 3  # today
    assert sum(d["credits"] for d in body["daily"][:-1]) == 0
    # Split by mode, so the admin chart can stack fast and deep credits.
    assert body["fast_calls"] == 1
    assert body["deep_calls"] == 1
    assert body["daily"][-1]["deep_credits"] == 2
    assert sum(d["deep_credits"] for d in body["daily"][:-1]) == 0


def test_usage_days_bounds(admin):
    client, auth = admin
    assert client.get("/v1/admin/usage?days=0", headers=auth).status_code == 422
    assert client.get("/v1/admin/usage?days=91", headers=auth).status_code == 422


# --- page and schema upgrade --------------------------------------------------


def test_admin_page_is_served_with_strict_csp(admin):
    client, _ = admin
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Lookout admin" in r.text
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    for path in ("/static/admin.js", "/static/admin.css"):
        assert client.get(path).status_code == 200


def test_schema_upgrade_adds_is_admin_to_an_old_table(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/old.db"

    async def go():
        engine = create_async_engine(url)
        async with engine.begin() as conn:  # the table as Phase 1 created it
            await conn.execute(text(
                "CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name VARCHAR(100), key_prefix VARCHAR(16), "
                "key_hash VARCHAR(64) UNIQUE, monthly_credit_limit INTEGER, active BOOLEAN, created_at DATETIME)"
            ))
            await conn.execute(text(
                "INSERT INTO api_keys VALUES (1, 'old', 'lo_old', 'h', 10, 1, '2026-11-01 00:00:00')"
            ))
        await engine.dispose()

        db = Database(url)
        await db.create_schema()
        await db.create_schema()  # idempotent
        async with db.engine.connect() as conn:
            columns = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("api_keys")})
            is_admin = (await conn.execute(text("SELECT is_admin FROM api_keys WHERE id = 1"))).scalar_one()
        await db.close()
        return columns, is_admin

    columns, is_admin = asyncio.run(go())
    assert "is_admin" in columns
    assert not is_admin
