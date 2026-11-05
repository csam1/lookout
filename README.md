# Lookout: web search for AI agents

Lookout is an internal web search API for agents. Returns ranked results with excerpts chosen for the agent's query and objective, plus usage metering. Design and phases are in [PLAN.md](PLAN.md). The HTTP contract is in [docs/openapi.json](docs/openapi.json).

Status: Phase 1 core done. Fast and deep modes, Postgres API keys and usage, Redis cache, SSRF-safe fetching, BM25 excerpts. See PLAN.md section 10 for measured latency and what is still open.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
docker compose up -d                     # Postgres, Redis, SearXNG
.venv/bin/lookout create-key --name my-agent   # prints the key once
```

## Run

```bash
.venv/bin/uvicorn lookout.main:app --port 8765
```

## Pages

http://localhost:8765/ is a homepage: what the API does, an example request and response, the two modes, and how to get started. It links to the playground, the admin page and the API reference at `/docs`.

## Playground UI

Open http://localhost:8765/playground, paste an API key into the top bar, and run a search. It shows what an agent receives: a strip of results, latency, credits, pages read and cache hit or miss; then one card per result with the excerpts, your query's words highlighted, whether the page was read (or why not) and characters used against the budget. The top bar shows the key's usage this month. "Request and raw response" has the exact JSON, a copyable curl command (with `$KEY`, not your key) and a Copy JSON button.

Site filters take one domain at a time as chips (Enter, comma or space adds one). The pages are dark by default; the sun/moon button switches to light and the choice is kept in the browser.

The key is kept in session storage for that tab only. The page renders web text as plain text and is served with a strict Content-Security-Policy.

## Admin UI

Open http://localhost:8765/admin and sign in with an admin key. Create the first one on the server:

```bash
.venv/bin/lookout create-key --name your-name --admin
```

The page shows this month's credits, searches (fast and deep), cache share and keys used, a chart of credits per day for the last 7, 30 or 90 days with deep and fast stacked (and a table view), and every key with its usage against its limit, with a filter box. From there you can create keys (the new key is shown once), rename them or change limits, and revoke them.

- Revoking is permanent, because a revoked key may have leaked.
- The last active admin key can't be revoked, so you can't lock yourself out.
- Every admin change is written to the `lookout.audit` log. The plaintext key is never logged.

The same actions are available as an API under `/v1/admin` (see `docs/openapi.json`).

## Try it

```bash
export KEY=lo_...   # from create-key

# fast: search snippets only (~1 s)
curl -s localhost:8765/v1/search -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query": "FDA predetermined change control plan", "max_results": 3}'

# deep: fetch pages and return passages relevant to the objective (~3-4 s cold, ~0 s cached)
curl -s localhost:8765/v1/search -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query": "FDA predetermined change control plan",
       "objective": "What can manufacturers change without a new submission?",
       "mode": "deep", "max_results": 4, "max_chars_per_result": 800}'
```

Each deep result has `fetch_status`: `ok`, or the reason the page could not be read (`timeout`, `http_403`, `robots_disallowed`, `blocked:non_public_address`, `empty_content`, ...). When it is not `ok`, excerpts fall back to the search snippet.

Deep mode waits at most `LOOKOUT_DEEP_DEADLINE_SECONDS` (default 3 s) for pages. Pages not ready by then return `fetch_status: "timeout"`, keep loading in the background, and are cached by URL, so the next query that returns them reads them instantly. Fetched pages and robots.txt rules are cached in Redis and shared across workers.

Credits: fast = 1, deep = 2. Cached responses are still billed. Failed searches (4xx/5xx) are not.

## Key management

```bash
.venv/bin/lookout create-key --name billing-agent --limit 20000   # monthly credit limit
.venv/bin/lookout list-keys
.venv/bin/lookout revoke-key --id 3
.venv/bin/lookout usage                                          # this month's calls and credits per key
```

A caller can check its own usage with `GET /v1/usage`.

Only a SHA-256 hash of each key is stored. A lost key cannot be recovered; revoke it and create a new one.

## Test

```bash
.venv/bin/python -m pytest -q                       # unit tests, no Docker needed
LOOKOUT_KEY=$KEY .venv/bin/python scripts/smoke_eval.py --mode deep   # 20 live questions, latency + fetch stats
```

## Free search provider (SearXNG)

Self-hosted SearXNG, no API key and no per-query fee. Results come from upstream engines, so quality and coverage vary. Config: `docker/searxng/settings.yml` (change `server.secret_key` before any shared deployment; JSON output must stay enabled).

## Layout

```
src/lookout/
  app.py          FastAPI app, lifespan, auth dependency, quota, usage, access log
  service.py      Response cache, provider call, filters, dedupe, deep-mode deadline + excerpts
  pages.py        Page cache by URL -> fetch -> extract
  fetch.py        SSRF-safe fetcher: public IPs only, redirect re-checks, size/time caps, robots.txt
  extract.py      HTML -> markdown blocks; drops scripts, chrome and hidden text
  excerpt.py      BM25 passage selection within the character budget
  cache.py        Redis result cache (fails open), TTL by time_range
  db.py           API keys and usage events (SQLAlchemy async)
  admin.py        Admin API: keys and usage across all callers (admin keys only)
  keys.py, auth.py, cli.py
  static/         Homepage (/), playground (/playground) and admin (/admin) pages; shared ui.css and theme.js
  providers/      SearchProvider interface; fake.py (tests); searxng.py (free, self-hosted)
scripts/smoke_eval.py   Live latency / fetch-rate check
docker/searxng/         SearXNG settings
```

## Errors

All errors use one shape:

```json
{"error": {"code": "quota_exceeded", "message": "...", "request_id": "req_..."}}
```

Codes: `unauthorized` (401), `forbidden` (403, not an admin key), `not_found` (404), `last_admin` (409), `invalid_request` (422), `quota_exceeded` (429), `upstream_search_failed` (502).
