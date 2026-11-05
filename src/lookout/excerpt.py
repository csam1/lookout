"""Pick the passages of a page that best match the query and objective (BM25).

Phase 1 ranks chunks within a single document. Phase 2 replaces this with embeddings
plus a reranker; keep the select_excerpts signature so the service does not change.
"""

import math
import re

from lookout.extract import split_text

TOKEN = re.compile(r"\w+", re.UNICODE)
STOPWORDS = frozenset(
    "a an and are as at be by for from has have how in is it its of on or that the this "
    "these those to was were what when which who why will with about into than then there".split()
)
CHUNK_TARGET = 600
MAX_EXCERPTS = 5
MIN_TAIL = 100  # don't append a truncated excerpt shorter than this
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if len(t) > 1 and t not in STOPWORDS]


def make_chunks(blocks: list[str], target: int = CHUNK_TARGET) -> list[str]:
    """Merge short blocks and split long ones so chunks are roughly `target` chars."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if len(block) > target * 1.5:
            if current:
                chunks.append("\n\n".join(current))
                current, size = [], 0
            chunks.extend(split_text(block, target))
            continue
        if current and size + len(block) > target:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(block)
        size += len(block) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def bm25_scores(docs: list[list[str]], query: list[str]) -> list[float]:
    n = len(docs)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in docs) / n or 1.0
    terms = set(query)
    df = {t: sum(1 for d in docs if t in d) for t in terms}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in terms}
    scores = []
    for doc in docs:
        dl = len(doc)
        counts: dict[str, int] = {}
        for tok in doc:
            if tok in terms:
                counts[tok] = counts.get(tok, 0) + 1
        score = 0.0
        for t, tf in counts.items():
            score += idf[t] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * dl / avgdl))
        scores.append(score)
    return scores


def select_excerpts(blocks: list[str], query_text: str, max_chars: int) -> list[str]:
    """Return up to MAX_EXCERPTS passages, best first, whose total length is <= max_chars."""
    chunks = make_chunks(blocks)
    if not chunks:
        return []
    scores = bm25_scores([tokenize(c) for c in chunks], tokenize(query_text))
    ranked = sorted(range(len(chunks)), key=lambda i: (-scores[i], i))
    picked = [i for i in ranked if scores[i] > 0][:MAX_EXCERPTS]
    if not picked:
        picked = [0]  # nothing matched: fall back to the opening of the page

    out: list[str] = []
    budget = max_chars
    for i in picked:
        text = chunks[i]
        if len(text) > budget:
            if out and budget < MIN_TAIL:
                break
            text = text[: budget - 1].rstrip() + "…"
        out.append(text)
        budget -= len(text)
        if budget <= 0:
            break
    return out
