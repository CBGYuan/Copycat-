"""Structured, session-only decisions captured while refining a skill.

The ledger is deliberately a sidecar to the Avatar skill. Nothing here is
written into ``skills*.yaml``: Avatar continues to receive its established
name/description/keywords/exclusive/expert_rules shape.
"""
import re
from typing import Dict, Optional


VALID_MODES = {"quiet", "smart", "grill"}


def normalize_mode(value) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in VALID_MODES else "smart"


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
        "blocking": bool(state.interview_mode == "grill") if blocking is None else bool(blocking),
    }
    state.decision_ledger.append(item)
    return item


def resolve(state, decision_id: str, answer: str) -> Optional[Dict]:
    decision_id = str(decision_id or "").strip()
    answer = str(answer or "").strip()
    if not decision_id or not answer:
        return None
    for item in state.decision_ledger:
        if item.get("id") == decision_id:
            item["status"] = "resolved"
            item["answer"] = answer
            return item
    return None


def defer(state, decision_id: str) -> Optional[Dict]:
    decision_id = str(decision_id or "").strip()
    for item in state.decision_ledger:
        if item.get("id") == decision_id:
            item["status"] = "deferred"
            item["blocking"] = False
            return item
    return None


def payload(state) -> Dict:
    items = [dict(item) for item in state.decision_ledger]
    return {
        "mode": normalize_mode(state.interview_mode),
        "items": items,
        "open": sum(item.get("status") == "open" for item in items),
        "resolved": sum(item.get("status") == "resolved" for item in items),
        "deferred": sum(item.get("status") == "deferred" for item in items),
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
