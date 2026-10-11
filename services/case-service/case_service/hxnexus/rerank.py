"""HxNexus — rerank retrieved chunks before they become context.

Cosine similarity over embeddings finds text that is *about* the question and
is weak on the exact thing asked for: a policy number, a clause reference, a
surname. The rerank takes a wider candidate set and re-orders it by cosine
blended with a keyword (BM25) score.

**Local by construction.** This runs on chunk text *before* the egress layers
(`ai_egress_top_k`, `ai_egress_min_score`, pseudonymisation), so it must never
hand that text to a provider. There is no model here and no network call —
arithmetic over strings already in memory — and that is the reason a
cross-encoder was not used: the configured LLM backend may be an external one.

**`score` is left alone.** Each chunk keeps its cosine in `score`, because the
egress relevance floor is defined against it; the blend goes in `rerank_score`
and decides only the order.

**Off unless switched on.** `resolve_mode` maps unset, empty and unrecognised
to off, and off means the caller's cosine top-k exactly as before.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter

log = logging.getLogger(__name__)

OFF = "off"
LEXICAL = "lexical"

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_K1, _B = 1.5, 0.75


def resolve_mode(value: str | None) -> str:
    name = (value or "").strip().lower()
    if name in ("", OFF, "none", "false", "0"):
        return OFF
    if name == LEXICAL:
        return LEXICAL
    log.warning("HxNexus: unknown rerank mode %r — rerank stays off", value)
    return OFF


def candidate_count(top_k: int, configured: int) -> int:
    """How many to retrieve so there is something to re-order."""
    try:
        configured = int(configured)
    except (TypeError, ValueError):
        configured = 0
    return max(top_k, configured)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def _bm25(question: str, texts: list[str]) -> list[float]:
    docs = [_tokens(t) for t in texts]
    terms = set(_tokens(question))
    if not terms or not docs:
        return [0.0] * len(texts)
    n = len(docs)
    avg_len = (sum(len(d) for d in docs) / n) or 1.0
    counts = [Counter(d) for d in docs]
    scores = [0.0] * n
    for term in terms:
        df = sum(1 for c in counts if term in c)
        if not df:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, c in enumerate(counts):
            tf = c.get(term, 0)
            if tf:
                norm = tf + _K1 * (1 - _B + _B * len(docs[i]) / avg_len)
                scores[i] += idf * tf * (_K1 + 1) / norm
    return scores


def rerank_lexical(
    question: str,
    chunks: list[dict],
    top_k: int,
    weight: float = 0.3,
) -> list[dict]:
    """Re-order `chunks` by cosine blended with BM25 and keep `top_k`.

    `weight` is the keyword share of the blend, clamped to 0..1. When the
    question has no term in any candidate the keyword score is flat zero and
    the cosine order comes back unchanged.
    """
    if not chunks:
        return []
    try:
        weight = min(1.0, max(0.0, float(weight)))
    except (TypeError, ValueError):
        weight = 0.0
    lexical = _bm25(question, [c.get("text") or "" for c in chunks])
    top = max(lexical)
    if top <= 0 or weight == 0:
        return chunks[:top_k]
    ranked = []
    for chunk, lex in zip(chunks, lexical):
        blended = (1 - weight) * float(chunk.get("score") or 0.0) + weight * (lex / top)
        ranked.append({**chunk, "rerank_score": blended})
    ranked.sort(key=lambda c: c["rerank_score"], reverse=True)
    return ranked[:top_k]
