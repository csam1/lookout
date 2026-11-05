# Lookout (web search for AI agents) — Plan

Goal: build a web search service for AI agents, in the same category as Tavily, Exa and Parallel. An agent sends a query (or a natural-language objective) and gets back clean, ranked, LLM-ready results: titles, URLs, dates, and short relevant excerpts or full cleaned content, with a token budget the caller controls.

Status: Phase 1 core built, plus page cache and request deadline (see README.md and section 10). Deep p50 is ~2.7 s cold and ~1.8 s with warm page cache. Decisions so far: internal agents first, public developer API later; starting from scratch, so the stack is Python + FastAPI, Postgres, Redis.

---

## 1. What "like Tavily / Parallel" means

The difference from a normal search API (Google/Bing/Brave) is the output contract. Agent-oriented search APIs typically:

| Need | What the API does |
|---|---|
| Results an LLM can use directly | Returns cleaned text or excerpts, not just snippets and HTML |
| Relevance to the agent's goal | Accepts an `objective` string in addition to the `query`, and ranks/extracts against it |
| Token control | `max_results`, `max_chars_per_result` / `max_tokens`, so the agent does not blow its context |
| Freshness | `time_range` / `start_date` filters, and published dates on every result |
| Source control | `include_domains` / `exclude_domains` |
| Grounding | Stable URLs, source metadata and optional raw content so the agent can cite |
| Safety | Output is treated as untrusted; the service strips scripts and hidden text, and offers a flag for injection-like content |
| Multiple modes | Fast snippet search for cheap lookups; deep mode that fetches and reads pages |
| Extra endpoints | `extract` (URL → clean markdown), later `crawl` / `map` and async deep research |

Proposed v1 request shape (subject to review):

```json
POST /v1/search
{
  "query": "latest FDA guidance on AI-enabled medical devices",
  "objective": "Summarise what changed in 2025-2026 for a compliance team",
  "mode": "fast | deep",
  "max_results": 5,
  "max_chars_per_result": 2000,
  "time_range": "month | year | any",
  "include_domains": ["fda.gov"],
  "exclude_domains": [],
  "include_raw_content": false
}
```

Proposed response:

```json
{
  "request_id": "req_...",
  "query": "...",
  "results": [
    {
      "title": "...",
      "url": "https://...",
      "domain": "fda.gov",
      "published_at": "2026-03-14",
      "score": 0.91,
      "excerpts": ["...relevant passage...", "..."],
      "content": null,
      "fetched_at": "2026-11-02T10:00:00Z"
    }
  ],
  "usage": { "credits": 2 },
  "latency_ms": 840
}
```

---

## 2. Architecture

```
            ┌──────────────┐
 agents ──► │ API gateway  │  auth (API keys), rate limits, quotas, request logs
            └──────┬───────┘
                   ▼
            ┌──────────────┐     ┌─────────────┐
            │ Search       │────►│ Cache       │  Redis: normalised query+params → results
            │ orchestrator │     └─────────────┘
            └──┬────────┬──┘
               │        │
   ┌───────────▼─┐   ┌──▼───────────────┐
   │ Search      │   │ Fetch & extract  │  fetch pool (SSRF-safe), robots.txt,
   │ providers   │   │ service          │  headless browser for JS pages,
   │ (adapters)  │   │                  │  readability → clean markdown
   └─────────────┘   └──┬───────────────┘
                        ▼
                 ┌─────────────────┐
                 │ Rank & excerpt  │  chunk → embed → rerank vs objective,
                 │                 │  pick passages within token budget, dedupe
                 └─────────────────┘
                        │
                        ▼
              Postgres (keys, usage, billing events) · object store (raw HTML, optional)
              Metrics/traces (OpenTelemetry) · eval harness (offline)
```

Components:

1. **API gateway.** Hashed API keys, per-key rate limits, monthly credit quotas, request IDs. Plain JSON over HTTPS. OpenAPI spec is the source of truth for SDKs.
2. **Search orchestrator.** Normalises the query, checks cache, fans out to one or more search providers, merges and de-duplicates by canonical URL, applies domain and date filters, and hands the candidate set to the fetch stage when mode is `deep`.
3. **Search providers (adapters).** One interface, several implementations. v1 uses one upstream web-search API. Candidates for upstream: Brave Search API, a self-hosted SearXNG instance (metasearch, open source), or a commercial SERP API. Check each provider's current terms, pricing and whether caching or redisplay of results is allowed before committing.
4. **Fetch & extract.** Fetches candidate pages with an SSRF-safe HTTP client (block private IP ranges, limit redirects, size and time caps, respect robots.txt). A headless-browser pool handles JavaScript-heavy pages and runs only when the plain fetch returns thin content. Extraction uses a readability-style extractor and converts to markdown.
5. **Rank & excerpt.** Splits documents into chunks, embeds chunks and the objective, reranks (embedding similarity first, cross-encoder reranker later), and returns the top passages up to the caller's character or token budget. This is the main quality lever and what separates an agent API from a snippet API.
6. **Cache.** Results cached by normalised (query, filters, mode). TTL depends on freshness: minutes for news, days for reference pages. Page content cached by URL with a TTL.
7. **Billing and metering.** Credits per call, weighted by mode (fast = 1, deep = more, extract = per URL). Usage events written asynchronously to Postgres; dashboard and quota checks read from there.
8. **Observability.** Per-stage latency (search, fetch, rank), provider error rates, cache hit ratio, fetch failure reasons. Trace IDs returned to the caller as `request_id`.

---

## 3. Build vs buy (the key decision)

Building a crawler and web-scale index is a multi-year, multi-team effort. Tavily and Parallel run their own infrastructure; most smaller teams do not. Options:

| Option | What you build | Pros | Cons |
|---|---|---|---|
| A. Wrap an upstream search API | Gateway, fetch/extract, rank/excerpt, cache, billing | Fast to ship (weeks), predictable quality of candidate URLs | Dependent on upstream terms, cost and rate limits |
| B. Self-hosted metasearch (SearXNG) + own fetch | Same as A plus operating SearXNG | No per-query search fee, no vendor lock-in | Result quality and engine coverage vary; you run and tune it |
| C. Own index / crawler | Everything above plus crawler, index, freshness pipeline | Full control, best long-term margin | Very large scope; not a v1 |

**Recommendation:** start with A and design the provider interface so B or C can be added later without changing the public API. The value to customers is in the output contract (excerpts, ranking against objective, token budgets, safety), not in owning the index on day one.

---

## 4. Phased plan

### Phase 0 — Decisions and skeleton (about 1 week)

- Confirm the open decisions in section 7.
- Set up the repo: language, framework, lint/test, CI, Docker Compose for Postgres and Redis.
- Write the OpenAPI spec for `/v1/search` and `/v1/extract` and review it against two or three real agent use cases.
- Pick the first search provider and check its terms.

### Phase 1 — MVP (about 3 weeks)

- `POST /v1/search` in `fast` mode: query → provider → normalised results with snippets.
- API key auth, per-key rate limit, monthly credits, usage table.
- Redis cache for search results.
- `deep` mode: fetch top N pages, extract markdown, return `content` truncated to `max_chars_per_result`.
- Basic excerpting: keyword/BM25 match against the objective, no embeddings yet.
- Logging with request IDs, latency metrics, error responses in one consistent format.
- Python and TypeScript SDK skeletons generated from the OpenAPI spec.
- Docs page with a quickstart and one example agent loop.

Exit criteria: a sample agent answers 20 hand-written questions end-to-end, with p50 under about 2s in `deep` mode and no fetch of private or local addresses (tested).

### Phase 2 — Quality (about 3–4 weeks)

- Embedding-based chunking and reranking for excerpts; objective-aware selection.
- Domain include/exclude and time-range filters, enforced at provider level where supported and post-filtered otherwise.
- Dedupe by canonical URL and near-duplicate content.
- Headless-browser fallback for JS-rendered pages, with a concurrency cap.
- Prompt-injection hygiene: strip hidden text, scripts and off-screen elements; flag suspicious passages in the response (`flags: ["possible_injection"]`).
- Eval harness: a golden set of 100–200 queries with expected sources; measure recall@5, excerpt relevance (LLM-judged with spot human checks), latency and cost per query. Run it on every change to ranking.
- Head-to-head comparison against Tavily and Exa on the same golden set, so claims about quality are measured, not assumed.

### Phase 3 — Production readiness (about 2–3 weeks)

- Multi-provider fallback and merge (if one provider errors or is rate-limited, use the next).
- Load tests at target QPS; autoscaling for fetch workers; circuit breakers per provider and per domain.
- Billing: credits to Stripe (or your billing system), usage dashboard, quota emails.
- Security review: key storage, SSRF tests, abuse controls (per-key and per-IP limits, blocked domains list), PII handling in logs (do not log query bodies by default).
- SLOs and alerts; runbook for provider outages.

### Phase 4 — Agent integrations and extra endpoints

- `POST /v1/extract` (URL list → markdown), then `map` / `crawl` for site-scoped work.
- MCP server exposing `search` and `extract` to Claude, Cursor and other MCP clients.
- Drop-in tool definitions for common frameworks (LangChain, LlamaIndex, OpenAI/Anthropic tool-calling schemas).
- Async deep research endpoint: submit an objective, poll or webhook for a structured report with citations.

### Later (only if usage justifies it)

- Own crawl and index for a chosen set of domains or topics.
- Dedicated freshness pipeline for news.
- On-prem or VPC deployment for regulated customers.

---

## 5. Non-functional requirements (initial targets, to be validated)

| Area | Target |
|---|---|
| Latency, fast mode | p50 < 800 ms, p95 < 2 s |
| Latency, deep mode (5 results) | p50 < 3 s, p95 < 8 s (dominated by page fetch) |
| Availability | 99.5% for v1, 99.9% after Phase 3 |
| Result freshness | Results reflect pages within the cache TTL for the query's time range |
| Security | No requests to private, loopback or link-local addresses; keys hashed at rest; TLS only |
| Cost | Target cost per deep query under a set amount (to be set; drives credit pricing) |
| Compliance | Respect robots.txt by default; honour takedown requests; honour upstream provider terms |

---

## 6. Risks

- **Upstream dependency.** Provider changes pricing, limits or terms. Mitigation: adapter interface, second provider by Phase 3, cache within allowed terms.
- **Extraction quality.** Some sites block bots or render only in JavaScript. Mitigation: browser fallback, clear `fetch_status` per result, do not return empty content silently.
- **Prompt injection through web content.** Agents read whatever the page says. Mitigation: sanitisation, flags, and a clear "treat as untrusted data" note in the docs.
- **Cost blow-up from deep mode.** Mitigation: per-key caps, credit weights, max URLs per call.
- **Legal.** Copyright and terms around storing and redisplaying content. Get a legal review before public launch, especially for cached full text.
- **Quality claims.** Without the eval harness, "better than Tavily" is unsupported. Mitigation: Phase 2 golden set.

---

## 7. Decisions and open questions

Decided:

1. **Audience:** internal agents first. A public developer API (self-serve keys, billing, abuse controls) comes later. Phase 3's hashed-key and billing work is what makes that switch possible.
2. **Stack:** starting fresh, so Python + FastAPI, Postgres, Redis, Docker.

Still open (needed before Phase 1):

3. **Upstream search provider** for v1: which one, and do your terms allow caching and returning result text to end users?
4. **Pricing model** (internal chargeback can be deferred): credits per call (Tavily-style) or usage tiers?
5. **Scope of v1:** is `deep` mode (fetch and extract) required in v1, or is `fast` search with snippets enough to start?
6. **Target volume:** expected queries per day in 6 and 12 months. This sets cache sizing, worker count and provider cost.
3. **Upstream search provider** for v1: which one, and do your terms allow caching and returning result text to end users?
4. **Pricing model:** credits per call (Tavily-style) or per-token/usage tiers?
5. **Scope of v1:** is `deep` mode (fetch and extract) required in v1, or is `fast` search with snippets enough to start?
6. **Target volume:** expected queries per day in 6 and 12 months. This sets cache sizing, worker count and provider cost.

---

## 8. Search providers and pricing

Prices are from secondary sources checked on 2026-11-02 and conflict in places. Confirm each on the vendor's pricing page before budgeting or signing up.

| Provider | Type | Approx. price per 1,000 searches | Free tier | Notes |
|---|---|---|---|---|
| Parallel | Agent search API | ~$5 Basic/Advanced; ~$1 Turbo/Fast | Sources conflict (5,000 to 16,000 requests/month) | Returns compressed excerpts; extract $0.001/request. Built for agent tool calls. |
| Brave Search API | Web index API | ~$5 standard; ~$4 Answers endpoint | $5/month credit reported; old free tier appears retired | Own index. A May 2026 changelog reports a price cut to $5/1K. |
| Exa | Neural (embedding) search | $7 auto; $4 lite; $12 deep; contents $1/1K pages | 1,000 requests/month | Retrieval is embedding-based, not a generative LLM. |
| Tavily | Agent search API | ~$8 pay-as-you-go; ~$30/month for 4,000 credits | 1,000 credits/month | Basic = 1 credit, advanced = 2 (sources conflict). |
| Perplexity Search API | Search API | Not checked | Not checked | Named in Parallel's comparison articles; pricing not verified. |
| DeepSeek Search API | Search API | Not checked | Not checked | Named in comparison articles; pricing not verified. |
| You.com, Linkup, Kagi | Search APIs | Not checked | Not checked | Compared in CostBench listings; not verified. |
| Serper / SerpAPI | Google SERP scrapers | Not checked | Not checked | Return Google results rather than an agent-ready format. Need checking for terms. |
| SearXNG (self-hosted) | Open-source metasearch | No per-query fee; hosting cost only | n/a | Aggregates other engines. Lower and less consistent quality; we run and tune it. |

Cost at 10,000 searches/month: about $50 (Parallel Basic, Brave), $70 (Exa), $80 (Tavily pay-as-you-go). Page fetching and extraction are extra.

Decision (2026-11-02): use the free option for now, self-hosted SearXNG. It is the first provider adapter (`src/lookout/providers/searxng.py`), runs in Docker, and has no key or quota. Quality is lower than the paid APIs and depends on upstream engines, so the eval harness in Phase 2 should measure it against a paid provider before any claims about quality. Paid options (Parallel, Brave, Exa, Tavily) stay behind the same interface for later, and the free tiers they offer (Exa 1,000/month, Tavily 1,000 credits/month) are a fallback for quality comparisons, not production.

## 9. Model choices

We don't need the underlying models of Tavily, Parallel or Exa. None of them publicly names its model, and the open components below cover what makes the output useful.

| Component | Use | Candidates (open weights) | License (per source) | How to run |
|---|---|---|---|---|
| LLM | Summaries and answer synthesis (optional; not needed for search) | Qwen3 (small and mid sizes); gpt-oss-20b / 120b; Gemma 3 12B / 27B | Qwen3 and gpt-oss: Apache 2.0. Gemma: check its license | Dev: Ollama in Docker (`ollama run qwen3:8b`). Prod: vLLM (OpenAI-compatible API) on a GPU host. |
| Embeddings | Chunk and query embeddings for excerpt ranking (Phase 2) | Qwen3-Embedding 0.6B / 4B / 8B; BGE-M3 | Apache 2.0 (per source) | Hugging Face Text Embeddings Inference (TEI) Docker image. 0.6B should run on CPU; not tested. |
| Reranker | Re-score passages against the objective (Phase 2) | BGE Reranker v2-m3 (default); Jina Reranker v2; Qwen3-Reranker | Check each model card | TEI. Qwen3-Reranker support in TEI not confirmed. |
| Llama 4 | Considered | Llama 4 Scout / Maverick | Meta community license (not OSI-approved) | Avoid for now unless licensing is acceptable. |

Hardware: not verified. Check each model card for VRAM or CPU needs before sizing. Start with the small Qwen3 and 0.6B embedding models so the pipeline runs on a laptop or one small GPU.

Phase mapping:
- Phase 1: no models. Provider results only, with BM25 excerpting.
- Phase 2: TEI embeddings and reranker, plus the eval harness. Optional LLM only if summaries are needed.
- Phase 3: evaluate larger open models against the golden set before choosing a production LLM.

## 10. Next steps

### Phase 0 (done)

Repo skeleton, `/v1/search` contract, provider interface, error format, OpenAPI export, free SearXNG provider in Docker.

### Phase 1 (core done, 2026-11-02)

Built:
- API keys in Postgres (SHA-256 hashed), `lookout` CLI to create, list and revoke keys and show usage.
- Monthly credit quotas (429 `quota_exceeded`) and a usage event per successful call. Fast = 1 credit, deep = 2.
- Redis result cache keyed by provider and normalised params, TTL by `time_range`, fails open. Empty results are not cached.
- Deep mode: SSRF-safe fetcher (public IPs only, every redirect re-checked, ports 80/443, size and time caps, robots.txt per RFC 9309), HTML extraction that drops chrome and hidden text, BM25 excerpts against query + objective, `fetch_status` per result with snippet fallback.
- Structured access log per request (no query text).
- `scripts/smoke_eval.py`: 20 live questions with latency and fetch stats.
- 77 unit tests, no Docker required.

Measured on 2026-11-02 (local laptop, SearXNG, cold caches, 20 questions, 5 results each; one run per setting, so treat as rough):

| Setting | p50 | p95 | Pages read OK |
|---|---|---|---|
| Fast mode (search only) | 0.95 s | 2.1 s | n/a |
| Deep, 5 s page budget (default) | 3.8 s | 6.2 s | 72% |
| Deep, 2.5 s page budget | 3.3 s | 3.7 s | 63% |
| Deep, repeat query (Redis hit) | ~1 ms | | |

Latency follow-up (2026-11-02):
- Page cache by URL in Redis (6 h for read pages; 1 h for robots, 4xx, blocked, unsupported or empty pages; transient failures not cached). A page read for one query is reused by later queries regardless of objective.
- robots.txt rules in Redis, shared across workers and restarts (6 h; 5 min when unreachable).
- Request deadline for deep mode (default 3 s). Pages not ready report `fetch_status: "timeout"` with snippet fallback, keep loading in the background, and fill the page cache. A response with timed-out pages is cached for only 2 min.
- Fix: robots.txt unreachable by network error now means disallow (RFC 9309); it previously meant allow.

| Setting (deep, 20 questions, one run each) | p50 | p95 | Pages read OK |
|---|---|---|---|
| Before: 5 s page budget, no page cache | 3.8 s | 6.2 s | 72% |
| After, cold (Redis flushed) | 2.7 s | 4.2 s | 72% |
| After, pages and robots warm | 1.8 s | 3.5 s | 75% |

Exit criteria status:
- 20 questions end-to-end: done, all 200, none empty.
- p50 under ~2 s in deep mode: **met with a warm page cache (1.8 s), not cold (2.7 s).** Search alone is ~1 s, so cold reads of new hosts cannot get much lower without a faster search provider. Section 5's NFR (deep p50 < 3 s, p95 < 8 s) is now met cold and warm.
- No fetches of private or local addresses: covered by tests (address ranges, redirects to internal hosts, mixed DNS answers, metadata IP).

Not done from the Phase 1 list:
- Python and TypeScript SDKs and a docs quickstart with an example agent loop.

Known gaps to close before any shared or public deployment:
- **DNS rebinding:** the fetcher checks resolved IPs, but httpx resolves again when connecting. Fix with an egress proxy or a pinned-IP transport.
- **Schema migrations:** tables are created with `create_all` on startup. Add Alembic before a shared deploy.
- **Quota race:** concurrent requests can overshoot a key's limit slightly (check, then record). Acceptable for internal use.
- **Background page reads** are per process and are cancelled on shutdown; a restart mid-read just means that page is fetched again later.

### Playground UI (done, 2026-11-02)

`/playground`: one page served by the API (plain HTML, CSS and JS; no build step). Shows excerpts with query terms highlighted, per-result read status in plain words, character budget used, latency, credits, cache hits, the key's monthly usage (`GET /v1/usage`), raw JSON and a copyable curl. Untrusted web text is rendered as text only, links are limited to http/https, and the page has a strict CSP. Checked in headless Chrome in light, dark and mobile layouts.

### Admin UI (done, 2026-11-02)

- Admin role: `is_admin` on API keys. The first admin key comes from `lookout create-key --admin`.
- `/v1/admin` API: list keys with this month's usage and last use, create (plaintext returned once), rename or change limit, revoke (permanent; the last active admin can't be revoked), and usage totals with a zero-filled daily series. Non-admin keys get 403. Every change goes to the `lookout.audit` log, without the plaintext key.
- `/admin` page: month totals, credits per day chart (hover tooltip, table view, colours validated for light and dark), keys table with inline edit and two-step revoke. On phones the keys show as stacked blocks. Checked in headless Chrome in light, dark and mobile layouts.
- Schema: the existing `api_keys` table gets the `is_admin` column through a small idempotent upgrade step in `Database.create_schema`. This is a stopgap; Alembic is still needed before a shared deploy.

### UI redesign (done, 2026-11-04)

- Homepage at `/` (home.html, home.css): what the API does, example request and response, fast vs deep, get-started steps. Brand mark on every page links to it.

Both pages restyled in a Vercel-like look: near-monochrome, true-black dark theme by default with a light toggle (`theme.js`, kept in localStorage), Geist and Geist Mono, 1px borders, one white primary button per page. Shared top bar with page tabs, the key as a pill, and the key's usage as a pill. Playground: segmented Fast/Deep switch, options always visible, domain chips, a five-cell stat strip (results, latency, credits, pages read, cache), result cards with a read-status badge, Copy JSON. Admin: four stat tiles, stacked deep/fast credits chart with a 7/30/90-day switch, key filter, inline revoke confirmation. The usage API gained `fast_calls`, `deep_calls` and per-day `deep_credits` for this. Checked in headless Chromium in dark, light, desktop and phone.

### Next

1. Alembic migrations, now that the schema has started changing (replaces the stopgap above).
2. SDKs and quickstart, if internal agents need them now.
3. Answer open questions 4–6 in section 7.
4. Phase 2: golden query set with expected sources, compare SearXNG against one paid provider's free tier, then embeddings + reranker.
