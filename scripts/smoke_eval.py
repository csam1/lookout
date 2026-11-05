"""Phase 1 smoke evaluation against a running API.

Runs a fixed set of questions, then reports latency percentiles, fetch success and
empty-result counts. This is a health check, not a quality eval; the Phase 2 golden
set with expected sources replaces it for quality.

    LOOKOUT_KEY=lo_... python scripts/smoke_eval.py [--mode deep] [--base http://localhost:8765]
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
import uuid
from collections import Counter

QUESTIONS = [
    "What is the FDA predetermined change control plan for AI devices?",
    "How does PostgreSQL MVCC work?",
    "What are the side effects of metformin?",
    "Kubernetes horizontal pod autoscaler custom metrics",
    "What is retrieval augmented generation?",
    "Python asyncio TaskGroup vs gather differences",
    "How do HbA1c targets differ for elderly patients?",
    "Redis eviction policies explained",
    "What is the RFC 9309 robots exclusion protocol?",
    "ABDM Health ID registration process India",
    "How does BM25 ranking work?",
    "Symptoms of vitamin D deficiency",
    "What is server-side request forgery and how to prevent it?",
    "FastAPI dependency injection lifespan events",
    "How do mRNA vaccines work?",
    "Difference between TCP and QUIC",
    "What is a cross-encoder reranker?",
    "WHO guidelines on hypertension treatment",
    "How does SearXNG metasearch work?",
    "Explain OAuth 2.0 PKCE flow",
]


def call(base: str, key: str, body: dict) -> tuple[int, dict, float]:
    req = urllib.request.Request(
        f"{base}/v1/search",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
            status = resp.status
    except urllib.error.HTTPError as exc:
        payload = json.load(exc)
        status = exc.code
    return status, payload, (time.perf_counter() - started) * 1000


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8765")
    parser.add_argument("--mode", choices=["fast", "deep"], default="deep")
    parser.add_argument("--max-results", type=int, default=5)
    args = parser.parse_args()
    key = os.environ.get("LOOKOUT_KEY")
    if not key:
        print("Set LOOKOUT_KEY to an API key (see `lookout create-key`).", file=sys.stderr)
        return 2

    # A per-run nonce in the objective keeps results out of the cache, so latency is real.
    nonce = uuid.uuid4().hex[:8]
    latencies, statuses, fetch = [], Counter(), Counter()
    empty = 0
    for q in QUESTIONS:
        body = {"query": q, "objective": f"answer the question ({nonce})", "mode": args.mode,
                "max_results": args.max_results, "max_chars_per_result": 1000}
        status, payload, ms = call(args.base, key, body)
        statuses[status] += 1
        latencies.append(ms)
        results = payload.get("results", [])
        if status == 200 and not results:
            empty += 1
        for r in results:
            fetch[(r.get("fetch_status") or "n/a").split(":")[0]] += 1
        print(f"{status}  {ms:7.0f} ms  {len(results)} results  {q[:60]}")

    total_fetch = sum(fetch.values())
    print("\n--- summary ---")
    print(f"mode={args.mode} questions={len(QUESTIONS)} http={dict(statuses)} empty={empty}")
    print(f"latency ms: p50={pct(latencies, 50):.0f} p90={pct(latencies, 90):.0f} "
          f"p95={pct(latencies, 95):.0f} max={max(latencies):.0f} mean={statistics.mean(latencies):.0f}")
    if args.mode == "deep" and total_fetch:
        print(f"fetch_status: {dict(fetch.most_common())}  ok_rate={fetch['ok'] / total_fetch:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
