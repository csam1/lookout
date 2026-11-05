<div align="center">

<h1>▲ lookout</h1>

<p><strong>OpenRouter for web search APIs.</strong><br />
One endpoint in front of any search backend. Give an agent a query and an objective; get back clean, ranked passages inside a budget you control.</p>

<p>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white">
  <img alt="Redis" src="https://img.shields.io/badge/Redis-DC382D?style=flat-square&logo=redis&logoColor=white">
  <img alt="SearXNG" src="https://img.shields.io/badge/SearXNG-free%20by%20default-3050FF?style=flat-square">
  <img alt="Self-hosted" src="https://img.shields.io/badge/self--hosted-111111?style=flat-square">
</p>

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="#api">API</a> ·
  <a href="#the-openrouter-idea-applied-to-search">Why “OpenRouter for search”</a> ·
  <a href="PLAN.md">Design notes</a>
</p>

</div>

---

> **The short version.** Search APIs are where LLM APIs were before OpenRouter: every provider has its own key, request shape, response schema, and price. Lookout gives agents one contract and hides the provider behind it.

Point your agent at Lookout instead of a specific search vendor. It sends a query and an optional `objective` — what the agent is trying to accomplish. Lookout returns ranked pages with the passages that matter, not whole pages or raw HTML. `fast` mode is one call to the search backend; `deep` mode reads the pages and selects excerpts against the objective.

Start free with self-hosted SearXNG. Add paid backends later without changing agent code.

## Why Lookout

| The usual problem | The Lookout way |
|---|---|
| Every search vendor has a different auth model, payload, response shape, and price. | One bearer key, one `POST /v1/search` contract, one response shape. |
| Switching search backends means rewriting every agent tool. | Implement one `SearchProvider` adapter; the public API stays identical. |
| Search results are snippets and HTML, not text an LLM can use. | Clean excerpts ranked against the agent's objective, inside a character budget. |
| Cheap lookups and expensive reads cost the same. | `fast` returns snippets for 1 credit; `deep` reads pages for 2. |
| You pay per query from day one. | Self-hosted SearXNG is free; paid backends are optional. |

## The OpenRouter idea, applied to search

OpenRouter gives applications one API for many LLM providers. Lookout does the same for web search.

| | OpenRouter | Lookout |
|---|---|---|
| **Sits in front of** | Many LLM providers | Many web search backends |
| **Public contract** | One chat-completions API | One `/v1/search` API |
| **Lets you** | Swap models without rewriting clients | Swap search providers without rewriting agents |
| **Adds** | Routing, fallback, pricing | Fetch/extract, objective ranking, caching, quotas |

**In one sentence:** Lookout is the OpenRouter layer for web search — one stable contract in front of interchangeable search backends.

The adapter layer is already live. The free, self-hosted **SearXNG** adapter ships as the default; a deterministic fake provider powers the tests. Paid backends such as Brave, Tavily, Exa, and Parallel are planned adapters behind the same interface — the request and response models do not change.

Today the backend is selected with `LOOKOUT_SEARCH_PROVIDER`. Multi-provider fallback and result merging are the next step, deliberately kept behind the same contract.

### Adding a provider

The whole integration surface is one small protocol in [`src/lookout/providers/base.py`](src/lookout/providers/base.py):

```python
class MyProvider:
    name = "my-provider"

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        time_range: str,
        include_domains: list[str],
        exclude_domains: list[str],
    ) -> list[ProviderHit]:
        ...
```

1. Implement `SearchProvider` under `src/lookout/providers/`.
2. Return `ProviderHit`s. Filtering, dedupe, caching, and deep-mode page reads are handled for you.
3. Register it in `build_provider` and set `LOOKOUT_SEARCH_PROVIDER=my-provider`.

## What a call gives you

- **Titles, URLs, domains, and dates** for every result.
- **Excerpts** selected against the query and objective, not random snippets.
- **A hard character budget** per result (`max_chars_per_result`), so context windows stay predictable.
- **`fetch_status` per result**, so the agent knows whether a page was read or fell back to a snippet.
- **Optional raw markdown** (`include_raw_content`) for pipelines that want the whole page.
- **Credit accounting**: `usage.credits`, monthly limits, and `GET /v1/usage`.

## How it works

```mermaid
flowchart LR
    agent["Agent<br/>one key · one schema"] --> api["Lookout<br/>POST /v1/search"]
    api --> gate["auth · quotas · cache"]
    gate --> orch["search orchestrator"]
    orch --> adapter["provider adapter"]
    adapter --> searxng["SearXNG<br/>free · default"]
    adapter -.-> paid["Brave · Tavily · Exa<br/>planned"]
    orch --> deep["fetch → extract → rank<br/>deep mode"]
    deep --> out["ranked excerpts<br/>within budget"]
    searxng --> out
```

| | `fast` | `deep` |
|---|---|---|
| **Credits** | 1 | 2 |
| **What happens** | One search call. Titles, URLs, and snippets. | Fetches each page, extracts clean text, ranks passages against the objective. |
| **Returns** | Snippets | Excerpts plus `fetch_status`; optional raw markdown. |
| **Typical latency** | ~1 s | ~2.7 s cold · ~1.8 s warm |
| **Best for** | Finding pages, cheap lookups | Answering, extracting, citing |

<sub>Latency measured locally against SearXNG on 20 questions. Real numbers depend on the backend and the pages. See [PLAN.md](PLAN.md) for methodology.</sub>

## Quick start

You need **Python 3.12+** and **Docker**.

```bash
git clone https://github.com/csam1/lookout.git
cd lookout

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env
docker compose up -d                              # Postgres, Redis, SearXNG

.venv/bin/lookout create-key --name me --admin    # printed once
.venv/bin/uvicorn lookout.main:app --port 8765
```

SearXNG takes a few seconds to warm up on its first start. The API is ready immediately.

Then open:

| Page | What it's for |
|---|---|
| [http://localhost:8765/](http://localhost:8765/) | Homepage and example request/response |
| [http://localhost:8765/playground](http://localhost:8765/playground) | Try searches, inspect excerpts, copy a cURL |
| [http://localhost:8765/admin](http://localhost:8765/admin) | Keys, limits, and usage charts (admin key) |
| [http://localhost:8765/docs](http://localhost:8765/docs) | Interactive API reference |

The key is shown once; only its hash is stored.

## API

### Make a search

```bash
export KEY=lo_...

curl -s http://localhost:8765/v1/search \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "FDA predetermined change control plan",
    "objective": "What can manufacturers change without a new submission?",
    "mode": "deep",
    "max_results": 4,
    "max_chars_per_result": 800
  }'
```

A trimmed response:

```json
{
  "request_id": "req_9f3c1a7e2b4d6081",
  "query": "FDA predetermined change control plan",
  "results": [
    {
      "title": "Predetermined Change Control Plans for AI-Enabled Device Software Functions",
      "url": "https://www.fda.gov/media/166704/download",
      "domain": "fda.gov",
      "published_at": "2024-12-04",
      "score": 1.0,
      "excerpts": [
        "A PCCP describes the modifications a manufacturer intends to make to the device, and how those modifications will be assessed, so that they can be implemented without a new marketing submission."
      ],
      "fetched_at": "2026-11-04T09:12:41Z",
      "fetch_status": "ok"
    }
  ],
  "usage": { "credits": 2 },
  "latency_ms": 3120,
  "cached": false
}
```

### Request fields

| Field | Default | Notes |
|---|---|---|
| `query` | — | Required, 1–500 characters. |
| `objective` | `null` | What the agent is trying to do. Used to rank passages. |
| `mode` | `"fast"` | `fast` or `deep`. |
| `max_results` | `5` | 1–20. |
| `max_chars_per_result` | `2000` | 100–10,000. Total excerpt budget per result. |
| `time_range` | `"any"` | `day`, `week`, `month`, `year`, or `any`. |
| `include_domains` | `[]` | Up to 50. Subdomains are included. |
| `exclude_domains` | `[]` | Up to 50. |
| `include_raw_content` | `false` | Deep mode only. Adds cleaned markdown to `content`. |

### Response notes

- `request_id` is also returned in the `X-Request-ID` header.
- `score` currently reflects provider order; the embedding reranker is a Phase 2 upgrade.
- `excerpts` total at most `max_chars_per_result` characters.
- `content` is only present in deep mode with `include_raw_content: true`.
- `cached: true` means the result came from Redis.
- `fetch_status` is deep-mode only: `ok`, or why a page could not be read (`timeout`, `http_403`, `robots_disallowed`, `blocked:non_public_address`, `empty_content`, …). When it is not `ok`, `excerpts` fall back to the search snippet.

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/search` | API key | Run a search |
| `GET` | `/v1/usage` | API key | Current month's usage |
| `GET` | `/healthz` | none | Liveness |
| `GET` | `/v1/admin/keys` | admin | List keys with usage |
| `POST` | `/v1/admin/keys` | admin | Create a key |
| `PATCH` | `/v1/admin/keys/{id}` | admin | Rename or change a limit |
| `POST` | `/v1/admin/keys/{id}/revoke` | admin | Revoke a key |
| `GET` | `/v1/admin/usage` | admin | Month totals and a daily series |

Errors share one shape, with the request ID for tracing:

```json
{
  "error": {
    "code": "quota_exceeded",
    "message": "Monthly credit limit reached (10000/10000).",
    "request_id": "req_9f3c1a7e2b4d6081"
  }
}
```

Common codes: `unauthorized` (401), `forbidden` (403), `not_found` (404), `last_admin` (409), `invalid_request` (422), `quota_exceeded` (429), `upstream_search_failed` (502).

## Under the hood

### Deep mode: from URL to excerpt

1. **Search** — the provider adapter returns candidates.
2. **Filter** — apply include/exclude domains, dedupe by canonical URL, trim to `max_results`.
3. **Fetch** — read pages concurrently with a request deadline.
4. **Extract** — turn HTML into clean text blocks; drop scripts, navigation, forms, and hidden text.
5. **Rank** — score chunks with BM25 against the query and objective.
6. **Budget** — return the best passages up to `max_chars_per_result`. If a page fails, fall back to its snippet.

Pages that miss the deadline keep loading in the background and fill the page cache. That makes repeat queries and later queries against the same URLs much faster.

### Safety and freshness

- **SSRF-safe fetcher**: public addresses only, every redirect re-checked, ports 80/443, size and time caps.
- **`robots.txt` respected** per RFC 9309 (4xx allows, 5xx or unreachable disallows), cached per origin.
- **Prompt-injection hygiene**: hidden text, scripts, and chrome are removed. Web content is treated as data, not instructions.
- **Keys**: stored as SHA-256 hashes; plaintext is shown once. Query text is deliberately not logged.
- **Caching**: result cache keyed by provider and normalised parameters; TTL depends on `time_range`. Pages are cached by URL for 6 hours; transient failures are not cached.
- **Known gap**: the fetcher re-resolves DNS at connect time, so close the DNS-rebinding gap documented in [PLAN.md](PLAN.md) before a public deployment.

## Operations

### CLI

```bash
lookout create-key --name my-agent --limit 20000   # monthly credit limit
lookout create-key --name ops --admin              # admin key
lookout list-keys
lookout revoke-key --id 3
lookout usage
```

Revocation is permanent. The last active admin key cannot be revoked. Every admin change is written to the `lookout.audit` log without the plaintext key.

### Admin UI

`/admin` shows month totals, credits per day, every key against its limit, inline editing, and two-step revoke. The same operations are available under `/v1/admin`.

### Configuration

Settings are environment variables with the `LOOKOUT_` prefix, loaded from `.env` if present. Defaults point at the Docker Compose services.

| Variable | Default | Purpose |
|---|---|---|
| `LOOKOUT_DATABASE_URL` | `postgresql+asyncpg://websearch:websearch@localhost:5432/websearch` | Postgres connection |
| `LOOKOUT_REDIS_URL` | `redis://localhost:6379/0` | Cache; empty string disables it |
| `LOOKOUT_SEARCH_PROVIDER` | `fake` | `searxng` for real search |
| `LOOKOUT_SEARXNG_BASE_URL` | `http://localhost:8080` | SearXNG instance |
| `LOOKOUT_DEEP_DEADLINE_SECONDS` | `3.0` | How long a deep request waits for pages |
| `LOOKOUT_FETCH_TIMEOUT_SECONDS` | `5.0` | Per-page limit, including background reads |
| `LOOKOUT_RESPECT_ROBOTS_TXT` | `true` | Honour `robots.txt` |
| `LOOKOUT_DEFAULT_MONTHLY_CREDIT_LIMIT` | `10000` | Limit for new keys |

The full list lives in [`src/lookout/config.py`](src/lookout/config.py).

## Project layout

```text
src/lookout/
  app.py           FastAPI routes, auth wiring, UI pages
  service.py       search orchestration: cache, filters, dedupe, modes
  providers/       provider adapters behind one SearchProvider protocol
  fetch.py         SSRF-safe page fetcher
  extract.py       HTML → clean markdown-style blocks
  excerpt.py       BM25 passage selection
  pages.py         page cache and read pipeline
  admin.py         key management and usage API
  static/          homepage, playground, admin (no build step)
tests/             unit tests with SQLite and fakes, no Docker required
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Status

**Shipped:** fast and deep search, SearXNG adapter, Redis result and page caches, deep-mode deadline with background reads, API keys and quotas, usage metering, CLI, playground, admin UI, and homepage.

**Next:** multi-provider fallback and merging, Alembic migrations, SDKs, embedding-based reranking with an eval harness, a `/v1/extract` endpoint, and an MCP server.

The full design, measurements, and roadmap are in [PLAN.md](PLAN.md).

## Further reading

- [PLAN.md](PLAN.md) — architecture, phases, measured latency, known gaps
- [docs/openapi.json](docs/openapi.json) — the full HTTP contract
- [src/lookout/providers/base.py](src/lookout/providers/base.py) — the provider interface
- [src/lookout/config.py](src/lookout/config.py) — every setting

<p align="center"><sub>▲ One lookout. Every search backend.</sub></p>
