# Lookout

A self-hosted web search API for AI agents. Send a query, get back ranked results with the passages that matter, not whole pages.

- **Fast mode** (~1 s): search results with snippets. Costs 1 credit.
- **Deep mode** (~3 s): reads the pages and picks the passages that answer your objective. Costs 2 credits.
- Free search through self-hosted SearXNG. No paid search API needed.
- API keys, monthly limits and usage tracking built in.
- Web pages for trying searches (playground) and managing keys (admin).

## Quick start

You need Python 3.12+ and Docker.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
docker compose up -d                              # Postgres, Redis, SearXNG
.venv/bin/lookout create-key --name me --admin    # prints your key once
.venv/bin/uvicorn lookout.main:app --port 8765
```

Then open:

| Page | What it's for |
|---|---|
| http://localhost:8765/ | Homepage |
| http://localhost:8765/playground | Try searches with your key |
| http://localhost:8765/admin | Manage keys and see usage (admin key) |
| http://localhost:8765/docs | API reference |

## Make a search

```bash
export KEY=lo_...

# fast
curl -s localhost:8765/v1/search -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query": "FDA predetermined change control plan", "max_results": 3}'

# deep
curl -s localhost:8765/v1/search -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query": "FDA predetermined change control plan",
       "objective": "What can manufacturers change without a new submission?",
       "mode": "deep", "max_results": 4}'
```

In deep mode, each result has a `fetch_status`. If a page couldn't be read (`timeout`, `http_403`, `robots_disallowed`, ...), you get the search snippet instead. Slow pages keep loading in the background and are cached, so asking again is instant.

## Manage keys

```bash
.venv/bin/lookout create-key --name my-agent --limit 20000   # monthly credit limit
.venv/bin/lookout list-keys
.venv/bin/lookout revoke-key --id 3
.venv/bin/lookout usage
```

Keys are stored as hashes, so a lost key can't be recovered. Revoke it and make a new one. A caller can check its own usage with `GET /v1/usage`.

## Run the tests

```bash
.venv/bin/python -m pytest -q
```

## Learn more

- [PLAN.md](PLAN.md): design, phases and measured latency
- [docs/openapi.json](docs/openapi.json): full HTTP contract, including errors and the admin API
- `src/lookout/`: the code (`service.py` is the search flow, `fetch.py` the safe page fetcher, `excerpt.py` the passage picker)
