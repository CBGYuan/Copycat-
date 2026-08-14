"""Structured, session-only decisions captured while refining a skill.

The ledger is deliberately a sidecar to the Avatar skill. Nothing here is
written into ``skills*.yaml``: Avatar continues to receive its established
name/description/keywords/exclusive/expert_rules shape.
"""
import re
from typing import Dict, Optional

from utils import question_match


VALID_MODES = {"quiet", "ask"}

# "smart" and "grill" were two separate modes until they were merged into
# "ask". They differed by one prompt paragraph and by whether an unresolved
# decision warned before Export -- but those are independent axes, and tying
# them together made "ask conservatively, yet still warn me before I export
# with open questions" unreachable. Warning before Export is now
# unconditional (see log_viewer.js's exportSkill) and blocking became a
# property of the QUESTION rather than of the mode (see record_question), so
# the axis the mode still controls is only "ask, or don't".
_LEGACY_MODES = {"smart": "ask", "grill": "ask"}


def normalize_mode(value) -> str:
    mode = str(value or "").strip().lower()
    mode = _LEGACY_MODES.get(mode, mode)
    return mode if mode in VALID_MODES else "ask"


def record_question(
    state,
    *,
    source: str,
    question: str,
    qtype: str = "open",
    options=None,
    basis: str = "",
    summary: str = "",
    recommended_answer: str = "",
    recommendation_reason: str = "",
    step="all",
    source_key: str = "",
    blocking: Optional[bool] = None,
) -> Optional[Dict]:
    """Add one open decision, de-duplicating a repeated source/question."""
    question = str(question or "").strip()
    if not question:
        return None
    source_key = str(source_key or "").strip()
    for item in state.decision_ledger:
        if source_key and item.get("source_key") == source_key:
            return item
        if item.get("status") == "open" and item.get("question") == question:
            return item

    state.decision_next_id += 1
    item = {
        "id": f"D{state.decision_next_id}",
        "source": str(source or "chat"),
        "source_key": source_key,
        "question": question,
        "type": "choice" if qtype == "choice" else "open",
        "options": [str(value).strip() for value in (options or []) if str(value).strip()][:4],
        "basis": str(basis or ""),
        "summary": str(summary or ""),
        "recommended_answer": str(recommended_answer or ""),
        "recommendation_reason": str(recommendation_reason or ""),
        "step": step if isinstance(step, int) else "all",
        "status": "open",
        "answer": "",
        # A property of THIS question, not of the interview mode: a genuine
        # specification decision (the default) is worth warning about before
        # Export; an optional follow-up explicitly passes blocking=False (see
        # learning_routes' teach-step follow-ups) so it never nags. Making
        # this mode-dependent is what previously let the default mode export
        # with unresolved decisions and no warning at all.
        "blocking": True if blocking is None else bool(blocking),
    }
    state.decision_ledger.append(item)
    return item


def resolve(state, decision_id: str, answer: str, *, covers=question_match.same_topic) -> Optional[Dict]:
    """Resolve one decision, and close the ones it made moot.

    `covers(a, b) -> bool` defaults to the same matcher the gap strip uses, so
    question-similarity has ONE definition rather than two that drift apart.
    Every caller gets the sweep by default — the symptom this fixes showed up
    through the plain-chat-reply path as readily as through a question card,
    and a resolve() that only sometimes swept would have fixed it in one place
    and left it broken in the other. Pass covers=None to opt out.

    Why supersede rather than resolve: the interview reliably circles back on
    a question in its own words a few turns later, and answering that later,
    better-phrased version left the ORIGINAL sitting open forever — it kept
    nagging on Export as an "unresolved specification decision" even though
    the engineer had plainly answered it. But the two questions are only
    FUZZILY the same, so copying the answer onto the older item would put
    words in the engineer's mouth. `superseded` says exactly what happened:
    covered by another decision, no longer blocking, never claimed to have
    been answered directly.
    """
    decision_id = str(decision_id or "").strip()
    answer = str(answer or "").strip()
    if not decision_id or not answer:
        return None
    for item in state.decision_ledger:
        if item.get("id") == decision_id:
            item["status"] = "resolved"
            item["answer"] = answer
            if covers:
                _supersede_covered_by(state, item, covers)
            return item
    return None


def _supersede_covered_by(state, answered: Dict, covers) -> None:
    """Close open items the just-answered one plainly also answers.

    Only ever downgrades OPEN items, and never the one just resolved — a
    deferred or already-resolved item has had its outcome decided and must not
    be rewritten by a later, loosely-related answer.
    """
    question = answered.get("question") or ""
    for other in state.decision_ledger:
        if other is answered or other.get("status") != "open":
            continue
        if covers(other.get("question") or "", question):
            other["status"] = "superseded"
            other["blocking"] = False
            other["superseded_by"] = answered.get("id", "")


def defer(state, decision_id: str) -> Optional[Dict]:
    decision_id = str(decision_id or "").strip()
    for item in state.decision_ledger:
        if item.get("id") == decision_id:
            item["status"] = "deferred"
            item["blocking"] = False
            return item
    return None


def latest_open(state, step="all") -> Optional[Dict]:
    """Best-effort fallback target for a plain chat reply that carries no
    explicit decision_id (the engineer typed straight into the main chat box
    instead of using a question card's own answer box). Without this, that
    answer is never recorded against the ledger even though it plainly
    addressed the pending question — the item stays "open" forever and keeps
    nagging on Export despite having been answered in conversation. Picks the
    MOST RECENTLY asked open item matching this step (or "all"), since that's
    the one the engineer is most likely replying to; an unrelated open item
    from several turns ago is left alone rather than guessed at."""
    candidates = [
        item for item in state.decision_ledger
        if item.get("status") == "open" and (
            item.get("step") == step or item.get("step") == "all" or step == "all"
        )
    ]
    return candidates[-1] if candidates else None


def payload(state) -> Dict:
    items = [dict(item) for item in state.decision_ledger]
    return {
        "mode": normalize_mode(state.interview_mode),
        "items": items,
        "open": sum(item.get("status") == "open" for item in items),
        "resolved": sum(item.get("status") == "resolved" for item in items),
        "deferred": sum(item.get("status") == "deferred" for item in items),
        "superseded": sum(item.get("status") == "superseded" for item in items),
        "blocking": sum(
            item.get("status") == "open" and item.get("blocking") for item in items
        ),
    }


def build_skill_spec(draft: Dict, state) -> Dict:
    """Build a review-only spec from a draft and the engineer's decisions."""
    ledger = payload(state)
    evidence = draft.get("teaching_evidence") or {}
    rules = str(draft.get("expert_rules") or "")
    rule_count = len(re.findall(r"(?m)^\s*\d+\.\s+", rules))
    resolved = [
        {
            "question": item.get("question", ""),
            "answer": item.get("answer", ""),
            "source": item.get("source", ""),
        }
        for item in ledger["items"]
        if item.get("status") == "resolved"
    ]
    # `superseded` is deliberately absent: it was covered by another decision
    # the engineer DID answer, so listing it here would report the same open
    # question twice — once as itself and once as the answer that closed it.
    unresolved = [
        item.get("question", "")
        for item in ledger["items"]
        if item.get("status") in {"open", "deferred"}
    ]
    return {
        "scope": str(draft.get("description") or ""),
        "triggers": list(draft.get("triggers") or []),
        "required_evidence": list(evidence.get("positive_keywords") or draft.get("keywords") or []),
        "exclusions": list(draft.get("exclusive") or []),
        "rule_count": rule_count,
        "resolved_decisions": resolved,
        "unresolved_decisions": unresolved,
        "labeled_examples": int(evidence.get("labeled_examples") or 0),
        "counterexamples": int(evidence.get("counterexample_count") or 0),
        "avatar_fields": ["name", "description", "keywords", "exclusive", "expert_rules"],
    }
