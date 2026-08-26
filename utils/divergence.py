"""
Divergence detection — what the engineer did that the LLM's baseline read
(services.learning_service.analyze_baseline) did NOT expect.

This is the gate that decides whether an interruption is worth an LLM call at
all, and it is deliberately pure and deterministic: no model is consulted
here. Two reasons that matters.

1. Cost/latency. It runs after every filter edit. Asking a model "was that
   surprising?" on each toggle is the auto-fire pattern this app already
   removed once.
2. Trustworthiness of the trigger. The baseline read is a language-model
   opinion, and in testing its key/noise split moved noticeably with nothing
   but prompt wording. So the baseline is NOT allowed to decide whether to
   interrupt — only to characterise an interruption that an OBJECTIVE,
   measured signal already justified. The order is always:

     materiality (measured: unique hits / dropped lines / a removal)
       -> then classification (baseline's stance on that keyword)

   `operation_journal.unreasoned_material_ops` IS the materiality gate — it
   already encodes "this edit had a real effect and the engineer hasn't
   explained it", and reusing it (rather than inventing a second threshold)
   is what keeps the Steps panel's own unexplained-edit hints and this module
   agreeing about which edits matter.

The classification splits material edits two ways, and the split is the whole
point — they deserve different treatment, per different papers:

  CONTRADICTION — the baseline committed to a stance and the engineer went
    the other way (it called a keyword load-bearing and they cut it; it
    called one noise and they promoted it). Two concrete interpretations now
    disagree about which lines survive, which is exactly ClarifyGPT's bar for
    asking: alternate readings with different observable behaviour. Worth one
    discriminating question.

  OMISSION — the baseline had no committed view on this keyword at all. This
    is not ambiguity, it is missing provenance: the engineer knows something
    that was never in contention. AutoManual's concern, not ClarifyGPT's, so
    it belongs in the Steps panel's own "explain this edit" affordance rather
    than in a clarifying question. Asking here would be low-information-gain
    noise (GATE), because there is no competing hypothesis to discriminate.

  FOCUS — setting an issue-time focus window is neither of the above. It does
    not change what a keyword means, so it must never be folded into the
    keyword comparison (see also operation_journal.annotate_effects, which
    refuses to diff survivor counts across a window change). But "how did you
    know the problem was at 09:41?" is itself reusable knowledge — often the
    single most transferable thing in a triage session — so it is surfaced as
    its own signal, once, with whatever locating hint the baseline offered so
    the question can be posed as a comparison rather than a blank prompt.
"""
from typing import Dict, List, Optional

from utils import operation_journal

# Edits that take a keyword OUT of the surviving set (or push it to the noise
# side), vs. edits that bring one IN. A contradiction is a stance/direction
# mismatch, so both halves are needed.
_DEMOTING = ("remove", "toggle_off", "add_exclude")
_PROMOTING = ("add_include", "toggle_on")

# How many omissions get a visible Steps-panel hint at once (see detect()).
_MAX_HINTS = 2


def _stance_map(baseline: Dict) -> Dict[str, Dict]:
    """keyword (lowercased) -> {"stance": "key"|"noise", "why": str}.

    Lowercased because the comparison must survive the engineer retyping a
    keyword with different casing; analyze_baseline already snapped these to
    the filter's own spelling, so this is belt-and-braces rather than the
    primary defence.
    """
    out: Dict[str, Dict] = {}
    for stance, field in (("key", "expected_key_keywords"), ("noise", "expected_noise_keywords")):
        for entry in (baseline.get(field) or []):
            text = str(entry.get("text") or "").strip()
            if text:
                out.setdefault(text.lower(), {"stance": stance, "why": str(entry.get("why") or "").strip()})
    return out


def _classify(op: Dict, stance_map: Dict[str, Dict]) -> Optional[Dict]:
    """CONTRADICTION when the baseline's stance and the edit's direction
    disagree; None when they agree (the engineer confirmed the read — nothing
    to ask) or when the baseline never had a stance (caller treats that as an
    omission)."""
    entry = stance_map.get(str(op.get("text") or "").strip().lower())
    if not entry:
        return None
    action = op.get("action")
    contradicts = (
        (entry["stance"] == "key" and action in _DEMOTING)
        or (entry["stance"] == "noise" and action in _PROMOTING)
    )
    if not contradicts:
        return None
    return {"baseline_stance": entry["stance"], "baseline_why": entry["why"]}


def _row(op: Dict) -> Dict:
    return {
        "seq": op["seq"],
        "text": op.get("text", ""),
        "action": op.get("action", ""),
        # Carried so the interview can ask at the right LEVEL: an include is a
        # log-content keyword for this scenario, an exclude is message-noise
        # policy that mostly generalizes across captures. Asking "why doesn't
        # this matter here?" about a noise term is the wrong question.
        "excluding": bool(op.get("excluding")),
        "effect_phrase": operation_journal._effect_phrase(op),
    }


def detect(state) -> Dict:
    """Classify every material, still-unexplained edit against the baseline.

    Returns:
      {
        "contradictions": [{seq, text, action, effect_phrase, baseline_stance, baseline_why}],
        "omissions":      [{seq, text, action, effect_phrase}],
        "focus":          {"center", "window_min", "baseline_hint"} | None,
        "has_baseline":   bool,
      }

    With no baseline yet (LLM unavailable at load, or a filter set that was
    never baselined) every material edit is reported as an omission: without a
    committed prior read nothing can be contradicted, and silently reporting
    nothing would make the Steps panel's hints vanish exactly when the
    engineer has least support.
    """
    baseline = state.baseline or {}
    stance_map = _stance_map(baseline)
    material_ops = operation_journal.unreasoned_material_ops(state)
    # Only edits made AFTER the read was committed can deviate from it. The
    # edits that assembled the filter set (loading a .tat, or typing the
    # keywords in by hand before baselining) are what the baseline looked AT —
    # judging them against it would report a .tat's own keywords as
    # contradicting the read of those same keywords. With no baseline the
    # cutoff is 0, so nothing is filtered out and everything falls through to
    # the omission path below.
    if baseline:
        material_ops = [op for op in material_ops if op["seq"] > state.baseline_op_seq]

    contradictions: List[Dict] = []
    omissions: List[Dict] = []
    for op in material_ops:
        verdict = _classify(op, stance_map) if stance_map else None
        if verdict:
            contradictions.append({**_row(op), **verdict})
        elif str(op.get("text") or "").strip().lower() not in stance_map:
            # No committed stance either way -> provenance gap, not ambiguity.
            omissions.append(_row(op))
        # else: the engineer's edit AGREES with the baseline's stance. Not a
        # divergence at all, so it is deliberately absent from both lists —
        # confirming the read is not a thing to interrupt about.

    # Only the most recent few omissions are flagged for a visible hint in the
    # Steps panel. Every omission is still reported (the interview and export
    # use the full list), but lighting up every unexplained edit at once turns
    # the panel into a wall of glowing icons, and a hint that is always on is
    # one nobody reads. Recency is the ranking because the edit an engineer
    # can most easily explain is the one they just made.
    for row in omissions[-_MAX_HINTS:]:
        row["highlight"] = True

    focus = None
    if state.focus_center_iso:
        focus = {
            "center": state.focus_center_iso,
            "window_min": state.focus_window_min,
            "baseline_hint": baseline.get("expected_issue_time_hint") or "",
        }

    return {
        "contradictions": contradictions,
        "omissions": omissions,
        "focus": focus,
        "has_baseline": bool(baseline),
        # Lets the client skip the /learning/clarify round-trip entirely once
        # the focus question has been put (the server enforces this too — this
        # is only to avoid a pointless request on every subsequent filter run).
        "focus_clarified": bool(state.focus_clarified),
    }
