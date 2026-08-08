"""Structured/lexical retrieval over episodes and reducer-derived state.
"Structured and lexical retrieval should come first" per the plan's own
design principles; embeddings are optional and NOT built here — nothing
yet needs semantic matching that keyword overlap can't handle at this
project's current scale.
"""

import re

from context.budget import estimate_tokens
from memory.episodes import list_episodes
from memory.reducers import reduce_state

_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")


def _tokenize(text) -> set:
    return set(w.lower() for w in _WORD_RE.findall(text or ""))


def _score(query_terms: set, text) -> int:
    return len(query_terms & _tokenize(text))


def retrieve(run_dir: str, query: str, max_tokens: int = 1000) -> list:
    """Ranked list of {"kind", "text", "score", "ref"}, trimmed to
    max_tokens total. Stale entities (memory/reducers.py's staleness
    flag) are excluded outright, not just deprioritized — a query about a
    file must not resurrect a pre-edit fact about it (Phase 5's own
    acceptance test). `ref` is an event_id (or subgoal_id for episodes) —
    memory/tools.py's memory_expand accepts an event_id directly."""
    query_terms = _tokenize(query)
    candidates = []

    for ep in list_episodes(run_dir):
        score = _score(query_terms, f"{ep['goal']} {ep['summary']}")
        if score > 0:
            candidates.append({"kind": "episode", "text": f"[{ep['subgoal_id']}] {ep['summary']}",
                                "score": score, "ref": ep["subgoal_id"]})

    state = reduce_state(run_dir)
    for e in state.get("changed_entities", []) + state.get("inspected_entities", []):
        if e.get("stale"):
            continue
        score = _score(query_terms, e["path"])
        if score > 0:
            ref = e.get("changed_at_event_id") or e.get("observed_at_event_id")
            candidates.append({"kind": "entity", "text": f"{e['path']} (turn {e.get('iteration')})",
                                "score": score, "ref": ref})

    for f in state.get("failures", []):
        text = f"{f.get('path') or f.get('command') or ''} {f['taxonomy']} {f['detail']}"
        score = _score(query_terms, text)
        if score > 0:
            candidates.append({"kind": "failure", "text": f"[{f['taxonomy']}] {f['detail'][:150]}",
                                "score": score, "ref": f["event_id"]})

    candidates.sort(key=lambda c: -c["score"])

    results, used_tokens = [], 0
    for c in candidates:
        cost = estimate_tokens(c["text"])
        if used_tokens + cost > max_tokens:
            break
        results.append(c)
        used_tokens += cost
    return results
