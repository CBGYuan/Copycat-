"""
Operation journal — an ordered record of the filter edits an engineer makes
on the left panel, each annotated with its *marginal effect* on the result.

Why this exists: the final filter state alone loses the engineer's reasoning.
"They added `BG_SCAN`, then excluded `Mcc` as noise" is exactly the tacit
skill we want to capture. Recording each edit — plus a cheap, token-light
stat of what it actually did (how many unique lines it contributed, how much
noise it dropped, what it co-fires with) — lets the skill-building interview
ask a *grounded* "why did you make this edit?" and store the answer, so the
learned skill inherits the engineer's judgment, not just their keyword list.

Nothing here reads the raw log. Effects are diffed from the stats snapshot
that compute_filter_stats already produces, so this adds ~no token or compute
cost to the existing filter run.
"""
from typing import Dict, List, Optional


# Human-readable verb for each action, used in the compact LLM rendering and
# the UI journal line.
_ACTION_VERB = {
    "add_include": "added include",
    "add_exclude": "added exclude",
    "remove": "removed",
    "toggle_on": "re-enabled",
    "toggle_off": "disabled",
    "load_skill": "loaded skill",
    "load_tat": "loaded .tat",
}


def record(state, action: str, text: str = "", excluding: bool = False,
           label: str = "") -> Dict:
    """Append one edit to the journal. `text` is the keyword (for filter
    edits) or the skill/file name (for loads). `reason` starts empty and is
    filled later — either by the engineer annotating it directly, or drawn out
    by the interview's "why" question and recorded via annotate_reason().
    `pending` marks that this edit's effect hasn't been measured by a filter
    run yet. `chat_index` is how many chat_history messages existed at this
    exact moment — the step viewer uses it to show "what was said around this
    edit" (chat_history[this.chat_index : next.chat_index]) without needing a
    separate timestamp/event-log mechanism."""
    event = {
        "seq": len(state.operations) + 1,
        "action": action,
        "text": text,
        "excluding": excluding,
        "label": label,          # e.g. the skill's display name for load_skill
        "reason": "",            # the engineer's WHY — the knowledge to capture
        "scope": "",             # optional applicability boundary learned from the explanation
        "evidence_status": "unexplained",  # unexplained | asserted | measured
        "effect": None,          # filled by annotate_effects() after the next run
        "pending": True,
        "chat_index": len(state.chat_history),
        "red_flags": [],         # filled by annotate_effects() — see detect_red_flags()
    }
    state.operations.append(event)
    return event


# Thresholds for the deterministic (NO LLM) red-flag detector below — tuned
# to only fire on edits worth interrupting for, not every minor tweak.
_REDUNDANT_HITS_MIN = 3
_BIG_EXPANSION_MIN = 15
_BIG_DROP_ABS_MIN = 10
_BIG_DROP_FRAC_MIN = 0.3
_LOAD_BEARING_UNIQUE_MIN = 5


def _last_known_unique(state, text: str) -> Optional[int]:
    """The most recent unique_hits this exact keyword was measured to
    contribute, from whenever it was last added/toggled — used to judge
    whether removing/disabling it now is throwing away something load-bearing."""
    for op in reversed(state.operations):
        if op["text"] == text:
            eff = op.get("effect") or {}
            if eff.get("unique_hits") is not None:
                return eff["unique_hits"]
    return None


def detect_red_flags(state, op: Dict, prev_survivors: Optional[int]) -> List[Dict]:
    """Cheap, deterministic (NO LLM call) checks over one just-measured
    operation's effect — the "passive" half of the interview: instead of
    waiting for the engineer to click Log Round, a handful of statistically
    obvious patterns (a keyword that added nothing, an exclude that silently
    removed a third of the results, disabling something load-bearing) get
    flagged the instant they happen, for free. Each flag is a ready-to-ask
    question the frontend can pop into chat without spending a token.
    """
    flags = []
    action = op["action"]
    text = op["text"]
    effect = op.get("effect") or {}

    if action == "add_include":
        hits = effect.get("hits") or 0
        uniq = effect.get("unique_hits")
        if hits >= _REDUNDANT_HITS_MIN and uniq == 0:
            flags.append({
                "type": "redundant_keyword",
                "question": f"\"{text}\" matched {hits} line(s) but contributed 0 unique ones — "
                             "every hit was already caught by another keyword. Keep it anyway, or is it redundant here?",
            })
        delta = effect.get("survivor_delta")
        if isinstance(delta, int) and delta >= _BIG_EXPANSION_MIN:
            flags.append({
                "type": "big_expansion",
                "question": f"Adding \"{text}\" pulled in {delta} more surviving lines. "
                             "Was that expansion expected, or did it catch something unrelated?",
            })
    elif action == "add_exclude":
        dropped = effect.get("dropped") or 0
        if dropped == 0:
            flags.append({
                "type": "noop_exclude",
                "question": f"The exclude \"{text}\" didn't drop anything this run — is that noise "
                             "term just absent from this log, or did you mean to match something else?",
            })
        elif dropped >= _BIG_DROP_ABS_MIN and isinstance(prev_survivors, int) and prev_survivors > 0 \
                and dropped / prev_survivors >= _BIG_DROP_FRAC_MIN:
            pct = round(100 * dropped / prev_survivors)
            flags.append({
                "type": "big_drop",
                "question": f"Excluding \"{text}\" removed {dropped} lines (~{pct}% of the prior result). "
                             "Was that scope of noise expected, or could it be hiding something relevant?",
            })
    elif action in ("remove", "toggle_off"):
        prior_uniq = _last_known_unique(state, text)
        if prior_uniq and prior_uniq >= _LOAD_BEARING_UNIQUE_MIN:
            verb = "removing" if action == "remove" else "disabling"
            flags.append({
                "type": "losing_load_bearing",
                "question": f"You're {verb} \"{text}\", which was uniquely responsible for {prior_uniq} "
                             "line(s) earlier. Still fine to drop it?",
            })
    return flags


def annotate_effects(state, stats: Dict) -> None:
    """After a filter run, attach the marginal effect to every operation that
    hasn't been measured yet, pulling the numbers straight out of `stats`
    (compute_filter_stats result) — no log rescan.

    For a keyword edit we record that keyword's own hits + unique_hits (for
    includes) or dropped-noise count (for excludes), plus its top co-fire
    partner, and the change in total survivors vs. the previous run. For a
    load we just record the resulting survivor count."""
    survivors = stats.get("surviving_count")
    prev = state.prev_survivors
    survivor_delta = (survivors - prev) if (isinstance(survivors, int) and isinstance(prev, int)) else None

    per_filter = {pf["text"]: pf for pf in stats.get("per_filter", [])}
    # Best co-fire partner for each keyword (highest-count pair touching it).
    co_by_text: Dict[str, Dict] = {}
    for c in stats.get("co_occurrence", []):
        for me, other in ((c["a"], c["b"]), (c["b"], c["a"])):
            if me not in co_by_text or c["count"] > co_by_text[me]["count"]:
                co_by_text[me] = {"text": other, "count": c["count"]}

    for op in state.operations:
        if not op.get("pending"):
            continue
        effect: Dict = {"survivor_delta": survivor_delta, "survivors_after": survivors}
        pf = per_filter.get(op["text"])
        if pf:
            effect["hits"] = pf.get("hits")
            if pf.get("unique_hits") is not None:
                effect["unique_hits"] = pf["unique_hits"]
            if pf.get("dropped") is not None:
                effect["dropped"] = pf["dropped"]
            co = co_by_text.get(op["text"])
            if co:
                effect["co_fire"] = co
        op["effect"] = effect
        op["pending"] = False
        op["red_flags"] = detect_red_flags(state, op, prev)

    if isinstance(survivors, int):
        state.prev_survivors = survivors


def annotate_reason(state, seq: int, reason: str) -> bool:
    """Store the engineer's stated reason for operation #seq (from the inline
    "why?" box or an interview answer). Returns False if seq is unknown."""
    reason = (reason or "").strip()
    for op in state.operations:
        if op["seq"] == seq:
            op["reason"] = reason
            op["evidence_status"] = "measured" if op.get("effect") else "asserted"
            return True
    return False


def _effect_phrase(op: Dict) -> str:
    eff = op.get("effect") or {}
    bits = []
    if eff.get("unique_hits") is not None:
        bits.append(f"{eff.get('hits', 0)} hits ({eff['unique_hits']} unique)")
    elif eff.get("dropped") is not None:
        bits.append(f"dropped {eff['dropped']} noise lines")
    elif eff.get("hits") is not None:
        bits.append(f"{eff['hits']} hits")
    co = eff.get("co_fire")
    if co:
        bits.append(f"co-fires \"{co['text']}\"×{co['count']}")
    if eff.get("survivor_delta") is not None:
        d = eff["survivor_delta"]
        bits.append(f"survivors {'+' if d >= 0 else ''}{d}")
    return ", ".join(bits)


def unreasoned_material_ops(state) -> List[Dict]:
    """Operations that had a real effect but the engineer hasn't explained —
    these are the highest-value "why did you do this?" targets for the
    interview (each one is a piece of judgment not yet captured)."""
    out = []
    for op in state.operations:
        if op["reason"]:
            continue
        if op["action"] in ("load_skill", "load_tat"):
            continue
        eff = op.get("effect") or {}
        material = bool(eff.get("unique_hits") or eff.get("dropped") or op["action"] == "remove")
        if material:
            out.append(op)
    return out


def compact(state, limit: int = 20) -> str:
    """Token-cheap rendering of the journal for the LLM context — one line per
    edit: what was done, its measured effect, and the engineer's reason (or a
    clear "(reason not given)" flag so the model knows to ask)."""
    ops = state.operations[-limit:]
    if not ops:
        return ""
    lines = []
    for op in ops:
        verb = _ACTION_VERB.get(op["action"], op["action"])
        target = op.get("label") or op["text"]
        head = f"#{op['seq']} {verb} \"{target}\""
        phrase = _effect_phrase(op)
        if phrase:
            head += f" — {phrase}"
        head += f" — reason: {op['reason']}" if op["reason"] else " — reason: (not given)"
        lines.append(head)
    return "\n".join(lines)


def payload(state) -> List[Dict]:
    """The journal shape the frontend renders (one row per edit)."""
    return [{
        "seq": op["seq"],
        "action": op["action"],
        "verb": _ACTION_VERB.get(op["action"], op["action"]),
        "text": op["text"],
        "label": op.get("label") or op["text"],
        "excluding": op["excluding"],
        "reason": op["reason"],
        "scope": op.get("scope", ""),
        "evidence_status": op.get("evidence_status", "unexplained"),
        "effect": op.get("effect") or {},
        "effect_phrase": _effect_phrase(op),
        "chat_index": op.get("chat_index", 0),
        "red_flags": op.get("red_flags") or [],
    } for op in state.operations]
