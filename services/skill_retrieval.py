"""
Agent C — lightweight retrieval that narrows a freshly synthesized candidate
draft down to the handful of EXISTING skills most likely to be the same
capability, so Agent B's judge (learning_service.judge_candidate) only has to
reason over a short, focused list instead of the entire domain pool.

Token-set (Jaccard) overlap over name + description + keywords is deliberately
used INSTEAD of dense embeddings — a WiFi/BT domain's skill count here is
tens, not thousands, so a vector index would be pure overhead for the
precision it buys. Revisit only if a single domain's skill count grows large
enough that lexical scoring starts missing genuine semantic matches (see the
AutoSkill-integration design notes).
"""
import re
from typing import Dict, List, Optional, Tuple

from .skill_service import Skill

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(*texts: str) -> set:
    out: set = set()
    for t in texts:
        out |= {m.group(0) for m in _TOKEN_RE.finditer(str(t or "").lower())}
    return out


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _draft_tokens(draft: Dict) -> set:
    return _tokens(
        draft.get("name", ""),
        draft.get("description", ""),
        " ".join(draft.get("keywords") or []),
    )


def _skill_tokens(skill: Skill) -> set:
    return _tokens(skill.name, skill.description, " ".join(skill.keywords or []))


def score_against(draft: Dict, skill: Skill) -> float:
    """0..1 similarity between a candidate draft and one existing skill.
    Blends whole-signal Jaccard (name+description+keywords together) with a
    name-only Jaccard so two skills that share an almost-identical name count
    for more than two skills that merely share a few generic keywords."""
    text_sim = _jaccard(_draft_tokens(draft), _skill_tokens(skill))
    name_sim = _jaccard(_tokens(draft.get("name", "")), _tokens(skill.name))
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
    scored = [
        (key, sk, score_against(draft, sk))
        for key, sk in (pool or {}).items()
        if key not in exclude_keys
    ]
    scored.sort(key=lambda t: t[2], reverse=True)
    return scored[:max(0, top_m)]
