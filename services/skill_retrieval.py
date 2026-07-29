"""
Agent C — lightweight retrieval that narrows a freshly synthesized candidate
draft down to the handful of EXISTING skills most likely to be the same
capability, so Agent B's judge (learning_service.judge_candidate) only has to
reason over a short, focused list instead of the entire domain pool.

Token overlap over name + description + keywords is deliberately used INSTEAD
of dense embeddings — a WiFi/BT domain's skill count here is tens, not
thousands, so a vector index would be pure overhead for the precision it buys,
and it would put a model download and a network round-trip in the path of an
otherwise offline tool. Revisit only if a single domain's skill count grows
large enough that lexical scoring starts missing genuine semantic matches.

Overlap is IDF-WEIGHTED rather than plain Jaccard, which is the cheap half of
AutoSkill's hybrid (dense + BM25) retrieval. Plain Jaccard counts every token
equally, so in a pool where every skill says "wifi", "log", "check" and
"analysis", those shared tokens dominate the score and two genuinely unrelated
skills look similar. Weighting each token by how RARE it is in the pool means
the tokens that actually identify a skill (`BT_COEX_DENY`, `EAPOL`) drive the
match. Same intuition BM25 encodes; no corpus statistics beyond the pool
itself, no model, no network.
"""
import math
import re
from typing import Dict, List, Optional, Tuple

from .skill_service import Skill

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(*texts: str) -> set:
    out: set = set()
    for t in texts:
        out |= {m.group(0) for m in _TOKEN_RE.finditer(str(t or "").lower())}
    return out


def build_idf(pool: Dict[str, Skill]) -> Dict[str, float]:
    """Inverse document frequency of each token across the skill pool.

    A token in every skill scores ~0 (it separates nothing); a token in one
    skill scores highest. Smoothed so a token absent from the pool entirely —
    which is the common case for a NEW draft's most distinctive keywords —
    still gets the maximum weight rather than a division by zero.
    """
    docs = [_skill_tokens(sk) for sk in (pool or {}).values()]
    n = len(docs)
    if not n:
        return {}
    df: Dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    return {t: math.log(1.0 + n / (1.0 + c)) for t, c in df.items()}


def _default_idf(idf: Dict[str, float]) -> float:
    """Weight for a token the pool has never seen. It appears in zero skills,
    so by the same formula it is maximally distinctive — treating it as 0
    would throw away exactly the tokens that make a new draft new."""
    return math.log(1.0 + len(idf)) if idf else 1.0


def _weighted_overlap(a: set, b: set, idf: Optional[Dict[str, float]]) -> float:
    """Jaccard, but each token contributes its IDF instead of 1. With no IDF
    map supplied this is plain Jaccard, so callers without a pool still work."""
    if not a or not b:
        return 0.0
    if not idf:
        union = len(a | b)
        return len(a & b) / union if union else 0.0
    fallback = _default_idf(idf)
    w = lambda t: idf.get(t, fallback)  # noqa: E731
    inter = sum(w(t) for t in (a & b))
    union = sum(w(t) for t in (a | b))
    return inter / union if union else 0.0


def _draft_tokens(draft: Dict) -> set:
    return _tokens(
        draft.get("name", ""),
        draft.get("description", ""),
        " ".join(draft.get("keywords") or []),
    )


def _skill_tokens(skill: Skill) -> set:
    return _tokens(skill.name, skill.description, " ".join(skill.keywords or []))


def score_against(draft: Dict, skill: Skill, idf: Optional[Dict[str, float]] = None) -> float:
    """0..1 similarity between a candidate draft and one existing skill.

    Blends whole-signal overlap (name+description+keywords together) with a
    name-only overlap, so two skills sharing an almost-identical name count
    for more than two sharing a few generic keywords.

    `idf` (from build_idf over the pool being searched) makes rare tokens
    carry the match. Omitting it falls back to plain unweighted overlap, which
    is what a caller comparing against a single skill with no pool in hand
    can do — see route_draft's continuity fast path.
    """
    text_sim = _weighted_overlap(_draft_tokens(draft), _skill_tokens(skill), idf)
    name_sim = _weighted_overlap(_tokens(draft.get("name", "")), _tokens(skill.name), idf)
    return round(0.7 * text_sim + 0.3 * name_sim, 4)


def retrieve_top_m(draft: Dict, pool: Dict[str, Skill], top_m: int = 3,
                    exclude_keys: Optional[set] = None) -> List[Tuple[str, Skill, float]]:
    """Ranks every skill in `pool` against `draft` and returns the top_m
    (key, skill, score) triples, highest score first (ties broken by
    insertion order via Python's stable sort). `exclude_keys` skips a skill a
    caller already ruled out via a separate check (e.g. the continuity fast
    path in learning_service.route_draft) so it isn't redundantly re-judged
    here with the exact same evidence."""
    exclude_keys = exclude_keys or set()
    # Built once over the WHOLE pool, including any excluded skills: token
    # rarity is a property of the library, not of this particular search, and
    # recomputing it per candidate would change every score depending on which
    # skill happened to be ruled out first.
    idf = build_idf(pool)
    scored = [
        (key, sk, score_against(draft, sk, idf))
        for key, sk in (pool or {}).items()
        if key not in exclude_keys
    ]
    scored.sort(key=lambda t: t[2], reverse=True)
    return scored[:max(0, top_m)]
