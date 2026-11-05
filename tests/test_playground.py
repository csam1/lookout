def test_usage_reports_this_months_calls_and_credits(api):
    client, auth = api(limit=500)
    client.post("/v1/search", json={"query": "a"}, headers=auth)
    client.post("/v1/search", json={"query": "b", "mode": "deep"}, headers=auth)

    r = client.get("/v1/usage", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["calls"] == 2
    assert body["credits_used"] == 3
    assert body["credit_limit"] == 500
    assert body["key_name"] == "test"
    assert body["key_prefix"].startswith("lo_")


def test_usage_requires_a_key(api):
    client, _ = api()
    r = client.get("/v1/usage")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_playground_page_is_served_with_strict_csp(api):
    client, _ = api()
    r = client.get("/playground")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Lookout playground" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "<script>" not in r.text  # no inline scripts; CSP would block them anyway


def test_playground_assets_are_served(api):
    client, _ = api()
    for path, kind in (("/static/playground.js", "javascript"), ("/static/ui.css", "text/css")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert kind in r.headers["content-type"]


def test_playground_is_not_in_the_api_schema(api):
    client, _ = api()
    paths = client.get("/openapi.json").json()["paths"]
    assert "/playground" not in paths
    assert "/v1/usage" in paths


def test_home_page_is_served_with_strict_csp(api):
    client, _ = api()
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'href="/playground"' in r.text
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    assert "<script>" not in r.text
    assert "/" not in client.get("/openapi.json").json()["paths"]
