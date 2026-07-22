"""
The "teach the system" learning loop:

  1. Engineer sets up a .tat filter the way they want (log_viewer) — this
     never triggers chat on its own; a tool pass just computes the
     *operation pattern* — per-keyword hit counts, which keywords co-fire,
     the time span — WITHOUT sending the raw log to the LLM.
  2. Engineer clicks "Log Round & Analyze" when THEY decide this filter state
     is worth submitting. One LLM call (analyze_round) does three things at
     once: analyzes what the round captures, asks 1-3 targeted questions
     grounded in the delta, and self-rates 0-100 how ready this is to export.
  3. Engineer answers in the chat (typed or picking a question's option).
     They can tweak the filter and click "Log Round" again for another round
     — round_count keeps incrementing, each round's delta is relative to the
     loaded baseline skill, same as before.
  4. When ready (readiness score is a hint, not a gate), the engineer clicks
     one of the two Export buttons. Both synthesize the whole conversation
     into ONE OR MORE BRAND-NEW skill entries matching wireless_ce_avatar/
     IntelAvatar's Skill schema (name / description / keywords / exclusive /
     expert_rules), aiming at three things the engineer's expertise supplies
     but the LLM lacks:
       a. key domain knowledge & hard rules (-> expert_rules),
       b. a description that is mutually exclusive with the other skills,
       c. the most minimal "key" filter set that still isolates the scenario.
     The two buttons differ only in whether the prior-knowledge base is
     consulted (see synthesize_skill_draft's `use_prior_knowledge`):
       • FRESH — no existing skill is looked at; the conversation is split
         into skills purely by the distinct knowledge domains it covers.
       • PRIOR — the SAME-domain (WiFi vs BT — see _other_skill_descriptions'
         `domain` param) existing skills are shown ONLY so the new skills stay
         mutually exclusive from them.
     Neither mode ever edits/extends an existing skill or annotates "add rule
     X to skill Y" — every draft is a fresh entry. Both still split into
     multiple entries when the conversation genuinely covers more than one
     distinct, mutually-exclusive scenario.
  5. Engineer reviews/edits each draft in the Edit Skill popup, then saves it.
"""
import json
import re
from typing import Dict, List, Optional

from utils.json_utils import parse_json_loose

# The three goals every skill-building question / synthesis is steering toward.
# Kept in one place so the interview and the synthesis prompts stay aligned.
_SKILL_GOALS = """\
A good reusable "skill" (the same concept as IntelAvatar's log-analysis agent
skills) needs three things the engineer's expertise supplies but a generic LLM
lacks:
 1. KEY KNOWLEDGE & RULES — the domain facts, thresholds, decode tables, and
    hard if/then diagnostic rules that let someone conclude a root cause from
    these log lines. (goes into `expert_rules`)
 2. A MUTUALLY-EXCLUSIVE DESCRIPTION — one sentence that says exactly what this
    skill covers, phrased so it does NOT overlap the other existing skills'
    scope. (goes into `description`)
 3. THE MINIMAL KEY FILTER SET — the smallest set of include-keywords that
    still reliably isolates this scenario, plus the noise terms worth
    excluding. (goes into `keywords` / `exclusive`)"""

# The assessment half of the interview — readiness + per-goal coverage + the
# still-missing gaps + a claim-by-claim 防呆/sanity-check. Shared verbatim
# between the Log-Round prompt (which also analyzes + asks) and the standalone
# per-answer assess prompt, so both produce the SAME shape and the readiness
# badge / details panel never drift depending on which path updated them.
#
# The validation step is the important one: an engineer's stated threshold
# ("roam needs +20% grade delta") is domain knowledge, NOT something this
# session's log proved — those must be flagged "asserted" so they can't be
# exported as if verified. That distinction is exactly what keeps a taught
# skill trustworthy.
_ASSESS_TASKS = """\
ASSESS how close this is to a good, trustworthy, reusable skill. Score
readiness 0-100 AND three coverage sub-scores (each 0-100), mapped to the
three goals:
 - knowledge: are the domain facts / thresholds / if-then rules captured well
   enough that a newcomer could reach the same root cause? (-> expert_rules)
 - scope: is the one-sentence scope clear AND mutually exclusive from the
   existing skills, and is extend-vs-new decided? (-> description)
 - keywords: is the minimal include-keyword set + noise excludes confirmed?
   (-> keywords/exclusive)
Readiness must RISE as gaps get answered; only lower it when a NEW
contradiction appears.

LIST GAPS: the specific still-missing pieces, each a short actionable phrase
(what to ask/answer next). Treat every material filter edit the engineer has
NOT explained (see the operation journal / unexplained-edits list) as a gap —
the reasoning behind an add/exclude is exactly the skill knowledge to capture.
Empty list only if nothing material is missing.

VALIDATE every substantive claim the engineer has made (防呆 / sanity-check).
Classify each:
 - "verified": directly supported by a log line / stat present THIS session
   (name the evidence).
 - "asserted": plausible domain knowledge the engineer stated but NOT confirmed
   by any log line this session (e.g. a threshold or bonus magnitude with no
   matching log). These must never be exported as if proven — flag them.
 - "contradiction": conflicts with the stats, with another answer, or is an
   open item the engineer left unresolved while wanting to export."""

# The JSON fields the assessment contributes — appended into whichever
# response object each prompt emits (Log-Round adds analysis+questions on top).
_ASSESS_JSON = """\
  "readiness": {"score": <0-100 integer>, "note": "<one short sentence>"},
  "coverage": {"knowledge": <0-100>, "scope": <0-100>, "keywords": <0-100>},
  "gaps": ["<short actionable missing piece>", "..."],
  "validation": [{"claim": "<what the engineer asserted>", "status": "verified|asserted|contradiction", "note": "<why / cite evidence>"}],
  "ready_to_export": <true|false>"""

ROUND_SYS_PROMPT = """\
You are a senior Wi-Fi/BT debug expert. The engineer just LOGGED A ROUND —
they set the filter the way they want and are submitting it for analysis. Do
these things in one response:

1. ANALYZE: 2-4 sentences on what this round's filter/delta actually
   captures, citing evidence from the operation pattern (hit counts, which
   keywords co-fire). This is analysis, not a question.
2. ASK: 1-3 short, specific questions that extract exactly the knowledge
   you're missing for the goals below. Prefer asking WHY the engineer made a
   specific still-unexplained edit from the operation journal (e.g. "you
   excluded 'Mcc' which dropped 300 lines — is that always noise, or only in
   this scenario?") — that reasoning is the tacit skill worth capturing.
   Ground every question in the round delta or a journal edit and, if you can
   identify the closest existing skill from the "Existing skills" list, name
   it explicitly.
3. """ + _ASSESS_TASKS + """

""" + _SKILL_GOALS + """

Each question is EITHER:
 - "open": genuinely open-ended — no small fixed set of sensible answers.
 - "choice": you can already enumerate the realistic answers (e.g.
   extend-vs-new, which keyword is load-bearing, which of two thresholds
   applies) — give 2-4 concrete options in the engineer's own domain
   vocabulary. Do NOT add an "Other" option yourself, the UI already offers a
   free-text fallback on every choice question.

Output ONLY this JSON object, no markdown fences, no extra prose:
{
  "analysis": "<2-4 sentences>",
  "questions": [{"question": "...", "type": "open"}, {"question": "...", "type": "choice", "options": ["...", "..."]}],
""" + _ASSESS_JSON + """
}
"""

ASSESS_SYS_PROMPT = """\
You are a senior Wi-Fi/BT debug expert acting as a QUALITY GATE for a skill
being taught interactively. The engineer just answered in the chat. Re-assess
where things stand given the WHOLE conversation so far.

""" + _ASSESS_TASKS + """

""" + _SKILL_GOALS + """

Output ONLY this JSON object, no markdown fences, no extra prose:
{
""" + _ASSESS_JSON + """
}
"""

# The interview runs in one of two CONVERSATION MODES, chosen by the two Log
# Round buttons and sticky for the whole session. The mode changes WHAT the
# LLM probes for — appended to both the Log-Round and per-answer-assess system
# prompts so questioning, gap-listing, and scoring all respect it:
#   • FRESH — no existing-skill base is shown at all; teach from scratch.
#   • PRIOR — the same-domain existing skills ARE shown, and the LLM must
#     treat what they already cover as given and only probe the NEW delta, so
#     the interview never re-asks knowledge the engineer already has captured.
_INTERVIEW_FRESH_CLAUSE = """

CONVERSATION MODE — NO PRIOR KNOWLEDGE: teach this skill from scratch. There is
no existing-skill base to lean on (none is shown to you). Ask whatever is
needed to fully capture this scenario's knowledge and assume nothing is already
covered. Treat every material, unexplained filter edit as a gap to probe."""

_INTERVIEW_PRIOR_CLAUSE = """

CONVERSATION MODE — WITH PRIOR KNOWLEDGE: the "Existing skills" list below is
already-captured team knowledge for THIS SAME domain — treat everything those
skills already cover as a GIVEN. Do NOT ask questions whose answer is already
encoded in one of them; re-teaching known knowledge is exactly the overlap to
avoid. Ask ONLY about what is genuinely NEW beyond them: the delta
keywords/edits and the tacit judgment not yet in any existing skill. When you
list gaps or score coverage, only count knowledge that is NEW relative to the
existing skills — knowledge they already hold is not a gap."""


def _interview_system_prompt(base: str, use_prior_knowledge: bool) -> str:
    """The interview/assess system prompt with the conversation-mode clause
    appended — one string per (base, mode) pair, so prompt-caching still gets a
    stable prefix within each mode."""
    return base + (_INTERVIEW_PRIOR_CLAUSE if use_prior_knowledge else _INTERVIEW_FRESH_CLAUSE)

# A short excerpt lifted verbatim from this project's own data/skills/skills.yaml
# (the connection_flow skill's ownership-check rule) — used purely to anchor the
# LLM's WRITING REGISTER for expert_rules, not as content to copy. Without this,
# synthesis drifts toward generic AI prose ("leverage the log data to identify...")
# that doesn't match the rest of the file and reads oddly next to it.
_STYLE_EXEMPLAR = """\
Match this exact voice — an excerpt from this project's own skills.yaml:

  expert_rules: |
    1. Ownership check (ownership lines: host does not have the NIC ownership
       semaphore, NIC ownership is at, [msysAcquireOwnershipApi]).
    - If the log contains: "HOST owns NIC"
      For example: 01/12/2026-07:59:03.398 [22] [MMACSYS] [SPECIAL]
      [msysAcquireOwnershipApi]:<< :(3429) - HOST owns NIC. skip
      acquireOwnership flow
      Then conclude:
        - Ownership = Host
        - Host Owns NIC = Yes
      Include the matching log line as evidence.
    - If neither condition is met:
      - Ownership = Unknown
      Include any ownership-related log lines found as evidence.

Conventions to follow, exactly:
 - Numbered top-level rules ("1.", "2.", ...); "-" sub-bullets for conditions.
 - Pattern per rule: state the condition ("If the log contains X..."), give ONE
   concrete example line — pulled from "Sample surviving log lines" below, a
   REAL line from this session, never invented — then "Then conclude: <result>".
 - Multi-branch rules end with an explicit block naming the fields to report.
 - Plain engineer register: short imperative sentences. NO marketing language
   ("leverage", "seamless", "robust", "streamline") and no filler ("It's
   important to note that", "In order to").
 - `keywords`/`exclusive` entries: EXACT substrings as they literally appear in
   the log (preserve original casing/spacing/brackets) — never paraphrased,
   never title-cased, never reworded for readability."""

# The skill-building synthesis always emits BRAND-NEW skills — it never
# "extends"/rewrites an existing one (no is_new / target_skill_key routing).
# The engineer chooses the mode with one of two Export buttons:
#   • FRESH   — no prior-knowledge base is consulted at all. Split the
#               conversation into skills purely by the distinct knowledge
#               domains it covers.
#   • PRIOR   — the existing WiFi (or BT) skills are shown ONLY so the new
#               skills' scope/description do not overlap them; the LLM still
#               produces new skills, never edits an existing one.
# Both share the schema + goals + split/field rules below; only the
# mutual-exclusion clause and the closing existing-skills instruction differ.
_SYNTHESIS_SCHEMA = """\
Produce one or more BRAND-NEW skill entries matching this exact JSON schema
(same shape as IntelAvatar's skills.yaml Skill):

{
  "skills": [
    {
      "name": "<short skill/category name>",
      "description": "<ONE sentence, scoped so it does NOT overlap any OTHER entry in this same skills array>",
      "keywords": ["<minimal essential include-keyword>", "..."],
      "exclusive": ["<noise term to exclude>", "..."],
      "expert_rules": "<numbered, actionable diagnostic rules as one string, using \\n between rules>",
      "note": "<one short sentence on what distinct scenario this skill isolates>"
    }
  ]
}

Every entry is a NEW, standalone skill. Do NOT try to edit, merge into, or
add rules onto any pre-existing skill — this flow only ever creates fresh
skills."""

_SYNTHESIS_SPLIT_AND_FIELDS = """\
Rules:
 - `skills` is USUALLY a single-element array. Emit MORE than one entry only
   when the operation pattern + interview genuinely cover two or more
   distinct, non-overlapping root-cause scenarios that cannot honestly be
   described by one mutually-exclusive description (e.g. the filter/chat
   covers both an auth/EAPOL failure loop AND an unrelated DHCP lease failure
   with disjoint keywords and a different root cause). Do NOT split just to
   separate minor variations, sub-steps, or severities of the SAME root cause
   — those belong in one entry's `expert_rules` as separate numbered rules.
   This knowledge-domain split is REQUIRED whenever the conversation really
   does span two domains, in BOTH modes. When you do split, every entry's
   `description` must be mutually exclusive from every OTHER entry in `skills`.
 - `keywords`: prefer the MINIMAL set that still isolates the scenario. Use
   the operation journal's marginal stats: drop any include-keyword with ~0
   UNIQUE hits (everything it caught, another keyword already caught — it's
   redundant), keep the ones carrying unique hits. Move genuine noise the
   engineer excluded into "exclusive".
 - `expert_rules`: fold in the engineer's stated REASONS from the operation
   journal — an edit's reason ("excluded Mcc because it's periodic housekeeping
   unrelated to scoring") often IS a diagnostic rule. Mark any threshold/claim
   the engineer asserted that the session's log did not actually demonstrate as
   engineer-provided rather than stating it as proven.
 - `exclusive`: cap around 8-10 terms — the highest-hit-count noise patterns
   only (check per-keyword hit counts), not every conceivable false positive.
   This is what keeps a future analysis call under IntelAvatar's own ~15KB
   per-skill evidence budget.
 - `expert_rules`: encode the engineer's key knowledge — concrete log patterns,
   thresholds, decode tables, and if/then conclusions — so a newcomer who has
   never seen this case can reach the same root cause. Cite representative log
   lines.
 - Output ONLY the JSON object, no markdown fences, no extra prose."""

# FRESH mode: no existing skills consulted at all.
SYNTHESIS_SYS_PROMPT_FRESH = """\
You are a senior Wi-Fi/BT debug expert. Using ONLY the operation pattern and
the engineer's answers from THIS session, """ + _SYNTHESIS_SCHEMA + """

""" + _SKILL_GOALS + """

There is NO prior-knowledge base to compare against in this mode — do not
assume, reference, or try to avoid any pre-existing skill. Scope and split the
skills you emit purely by the distinct knowledge domains the conversation
itself covers.

""" + _SYNTHESIS_SPLIT_AND_FIELDS + """

""" + _STYLE_EXEMPLAR + """
"""

# PRIOR mode: existing same-domain skills shown for mutual-exclusion only.
SYNTHESIS_SYS_PROMPT_PRIOR = """\
You are a senior Wi-Fi/BT debug expert. Using the operation pattern, the
engineer's answers, and the OTHER existing skills of THIS SAME domain (name +
description + keywords), """ + _SYNTHESIS_SCHEMA + """

""" + _SKILL_GOALS + """

You are shown the existing skills of this domain ONLY so the NEW skills you
emit stay mutually exclusive from them — carve the new skill's scope around
what the existing skills already cover so it is genuinely additive knowledge,
never a duplicate. Do NOT propose editing or extending any of them; if the
whole conversation only re-covers ground an existing skill already owns, say
so in that entry's `note` and still emit it as a clearly-distinct new skill (or
omit it). Each `description` must be mutually exclusive from every skill in
"Existing skills" AND from every other entry in `skills`.

""" + _SYNTHESIS_SPLIT_AND_FIELDS + """

""" + _STYLE_EXEMPLAR + """
"""


def _build_context_block(context: Dict) -> str:
    lines = [
        f"Domain: {(context.get('domain') or 'wifi').upper()} — only route/compare against "
        "the 'Existing skills' list below, which is already scoped to this same domain.",
        f"Filter file: {context.get('tat_path') or '(none)'}",
        f"Enabled include-keywords: {', '.join(context.get('keywords') or []) or '(none)'}",
        f"Enabled exclude/noise terms: {', '.join(context.get('excluding_terms') or []) or '(none)'}",
    ]

    # The operation delta vs. whatever baseline skill is loaded — this is the
    # single most token-efficient signal available, and it's completely
    # generic (works for any skill/tat, not just one scenario): "what did the
    # engineer just add/remove" IS "what they're teaching", full stop. When
    # it's non-empty we lean on it hard and skip re-dumping the baseline
    # skill's full keyword-by-keyword hit counts (already vetted when that
    # skill was first written) — that's what keeps a 20+-keyword skill like
    # connection_flow cheap to iterate on instead of re-paying for its whole
    # keyword list on every single question round.
    delta = context.get("operation_delta") or {}
    has_baseline = bool(delta.get("baseline_skill_key"))
    has_delta = bool(delta.get("added_keywords") or delta.get("added_exclusive") or delta.get("removed_keywords"))
    if has_baseline:
        lines.append(
            f"Baseline skill already loaded: \"{delta['baseline_skill_name']}\" "
            f"(key: {delta['baseline_skill_key']}) — you are most likely REFINING this skill, not "
            "scoping a new one from scratch. Only the delta below is new/changed relative to it."
        )
    if has_delta:
        parts = []
        added = delta.get("added_keywords") or []
        if added:
            rows = "\n".join(f"  - \"{a['text']}\": {a['hits']} hits" for a in added)
            parts.append(f"NEW include-keywords not in the baseline skill:\n{rows}")
        if delta.get("added_exclusive"):
            parts.append("NEW exclude/noise terms not in the baseline skill:\n  - " + "\n  - ".join(delta["added_exclusive"]))
        if delta.get("removed_keywords"):
            parts.append("Baseline keywords now DISABLED/removed:\n  - " + "\n  - ".join(delta["removed_keywords"]))
        lines.append("Operation delta (what actually changed — focus here):\n" + "\n".join(parts))

    # Compact operation-pattern stats instead of dumping the raw log (which can
    # be 100k+ lines) — this is what keeps every LLM call cheap regardless of
    # source log size.
    stats = context.get("filter_stats") or {}
    if stats:
        total = stats.get("total_lines")
        survived = stats.get("surviving_count")
        overlap = stats.get("overlap_count")
        span = stats.get("time_span") or {}
        span_txt = ""
        if span.get("first") or span.get("last"):
            span_txt = f" Time span: {span.get('first')} → {span.get('last')}."
        lines.append(
            f"Operation pattern: {survived}/{total} lines survived the filter; "
            f"{overlap} lines matched 2+ include-keywords (the intersection).{span_txt}"
        )
        # Full per-keyword breakdown only when there's no baseline+delta to
        # lean on instead — i.e. this really is a from-scratch scenario and
        # every keyword's hit count is still live information.
        per_filter = stats.get("per_filter") or []
        if per_filter and not (has_baseline and has_delta):
            rows = "\n".join(
                f"  - {'[EXCLUDING] ' if pf['excluding'] else ''}\"{pf['text']}\": {pf['hits']} hits"
                for pf in per_filter
            )
            lines.append(f"Per-keyword hit counts:\n{rows}")
        co = stats.get("co_occurrence") or []
        if has_baseline and has_delta:
            # Only pairs touching a NEW keyword are informative here — pairs
            # among unchanged baseline keywords were already true when that
            # skill was written and add nothing to this round.
            added_texts = {a["text"] for a in (delta.get("added_keywords") or [])}
            co = [c for c in co if c["a"] in added_texts or c["b"] in added_texts]
        if co:
            rows = "\n".join(
                f"  - \"{c['a']}\" + \"{c['b']}\": co-fire on {c['count']} lines"
                for c in co
            )
            lines.append(f"Keyword co-occurrence (the operation pattern):\n{rows}")
    # The operation journal: the engineer's edit-by-edit reasoning journey.
    # This is the richest token-cheap signal for drawing out tacit skill —
    # each edit's measured effect is one line, and any edit still tagged
    # "reason: (not given)" is a concrete thing to ask WHY about.
    journal = context.get("operation_journal")
    if journal:
        lines.append(
            "Operation journal (the engineer's filter edits, their measured effect, and their "
            "stated reason — edits marked \"reason: (not given)\" are judgment NOT yet captured):\n"
            + journal
        )
    unreasoned = context.get("unreasoned_ops") or []
    if unreasoned:
        rows = "\n".join(f"  - #{o['seq']} {o['action']} \"{o['text']}\" ({o['effect_phrase']})" for o in unreasoned)
        lines.append(
            "Edits that had a real effect but the engineer has NOT explained yet — prioritise a "
            "\"why did you do this?\" question for the load-bearing ones:\n" + rows
        )

    existing = context.get("existing_skills") or []
    if existing:
        # Same "- name: description" bullet format IntelAvatar's own agent is
        # shown when it picks which skill to call — proven format for this
        # exact judgment call. Keywords appended (capped) so literal overlap
        # is visible, not just prose-level similarity.
        rows = "\n".join(
            f"  - {s['name']} (key: {s.get('key', s['name'])}): {s['description']}"
            + (f" | keywords: {', '.join(s['keywords'][:10])}" if s.get('keywords') else "")
            for s in existing
        )
        lines.append(
            "Existing skills, same domain as this log (decide overlap/extend-vs-new "
            "against these; keep any new description mutually exclusive from them):\n" + rows
        )
    sample = context.get("sample_lines") or []
    if sample:
        # Caller (learning_routes._sample_lines) already picked the right
        # subset — a HEAD + TAIL split, not just the chronological prefix —
        # so the LLM sees both how the scenario starts AND how it ends
        # (root-cause/failure lines typically cluster near the end of a
        # survivor set). Don't re-slice here, that would silently cut the
        # tail back off.
        preview = "\n".join(sample)
        lines.append(f"Sample surviving log lines (head + tail, chronological order within each half):\n{preview}")
    chat_history = context.get("chat_history") or []
    if chat_history:
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in chat_history[-12:])
        lines.append(f"Interview so far:\n{convo}")
    return "\n\n".join(lines)


def _normalize_question(q) -> Optional[Dict]:
    """Coerce one LLM-emitted question into {question, type, options} —
    tolerant of the model returning a bare string (old-style) or an object
    missing/mistyping a field, since a malformed single question shouldn't
    sink the whole batch."""
    if isinstance(q, str):
        text = q.strip()
        return {"question": text, "type": "open", "options": []} if text else None
    if not isinstance(q, dict):
        return None
    text = str(q.get("question", "")).strip()
    if not text:
        return None
    options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
    qtype = "choice" if (q.get("type") == "choice" and len(options) >= 2) else "open"
    return {"question": text, "type": qtype, "options": options if qtype == "choice" else []}


def _clamp_score(value, default: int = 0) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


_VALID_STATUSES = ("verified", "asserted", "contradiction")


def _parse_assessment(parsed: Dict) -> Dict:
    """Pull the readiness / coverage / gaps / validation block out of a parsed
    LLM object — shared by analyze_round (Log Round) and assess_readiness
    (per chat answer) so both surface the exact same shape to the UI."""
    readiness = parsed.get("readiness") or {}
    coverage = parsed.get("coverage") or {}
    gaps = [str(g).strip() for g in (parsed.get("gaps") or []) if str(g).strip()]

    validation = []
    for v in parsed.get("validation") or []:
        if not isinstance(v, dict):
            continue
        claim = str(v.get("claim", "")).strip()
        if not claim:
            continue
        status = str(v.get("status", "")).strip().lower()
        if status not in _VALID_STATUSES:
            status = "asserted"  # safest default: treat unknown as not-yet-proven
        validation.append({"claim": claim, "status": status, "note": str(v.get("note") or "").strip()})

    return {
        "readiness": {"score": _clamp_score(readiness.get("score")), "note": str(readiness.get("note") or "").strip()},
        "coverage": {
            "knowledge": _clamp_score(coverage.get("knowledge")),
            "scope": _clamp_score(coverage.get("scope")),
            "keywords": _clamp_score(coverage.get("keywords")),
        },
        "gaps": gaps,
        "validation": validation,
        "ready_to_export": bool(parsed.get("ready_to_export")),
    }


def analyze_round(llm_helper, context: Dict, round_num: int,
                   use_prior_knowledge: bool = False) -> Dict:
    """One LLM call that does analysis + interview questions + a full
    assessment (readiness, per-goal coverage, gaps, and a claim-by-claim
    validation) together. Cheaper than separate calls, and it lets the
    model's questions be grounded in its own just-written analysis of this
    specific round.

    `use_prior_knowledge` sets the conversation mode (see
    _interview_system_prompt): PRIOR makes the questions/gaps skip anything the
    same-domain existing skills already cover, FRESH teaches from scratch.

    Returns {analysis, questions, assessment}, where `assessment` is either
    the dict from _parse_assessment() or None on total parse failure. None is
    a deliberate signal, not a zeroed-out result — the caller (learning_routes
    .log_round) must NOT overwrite the session's last known-good readiness/
    coverage/gaps/validation when this happens, only visibly regressing the
    readiness badge to 0% on a transient truncation would be actively
    misleading (readiness is supposed to only move when real evidence
    changes it).
    """
    user_prompt = f"Round {round_num}.\n\n" + _build_context_block(context)
    raw = llm_helper.chat(
        messages=[{"role": "user", "content": user_prompt}],
        system_content=_interview_system_prompt(ROUND_SYS_PROMPT, use_prior_knowledge),
        temperature=0.3,
        # A long-running session (many rounds, a growing validation list) can
        # push the assessment's own JSON past 1800 tokens, truncating it —
        # same failure shape as the earlier synthesize_skill_draft bug. 3000
        # gives headroom; returning assessment=None on any remaining failure
        # (below) also stops that from ever being displayable garbage.
        max_tokens=3000,
    )
    parsed = parse_json_loose(raw)
    if not parsed:
        print(f"⚠️  analyze_round: could not parse LLM output as JSON ({len(raw)} chars). "
              f"Raw tail: ...{raw[-200:]!r}")
    questions = [n for n in (_normalize_question(q) for q in ((parsed or {}).get("questions") or [])) if n]
    analysis = str((parsed or {}).get("analysis") or "").strip()
    if not analysis:
        analysis = "⚠️ This round's response couldn't be parsed — try Log Round & Analyze again."
    return {
        "analysis": analysis,
        "questions": questions,
        "assessment": _parse_assessment(parsed) if parsed else None,
    }


def assess_readiness(llm_helper, context: Dict, use_prior_knowledge: bool = False) -> Optional[Dict]:
    """Lightweight, standalone re-assessment used after EACH chat answer (no
    analysis, no new questions) so the readiness badge + claim-check panel
    update live as the engineer teaches, instead of only jumping on Log Round.

    `use_prior_knowledge` mirrors analyze_round's mode so per-answer scoring
    counts only NEW-vs-existing knowledge as coverage in PRIOR mode.

    Returns the dict from _parse_assessment(), or None on total parse
    failure — None means "couldn't tell this time," not "readiness is 0."
    The caller must keep serving the last known-good assessment rather than
    overwrite it with zeroed defaults.
    """
    user_prompt = _build_context_block(context)
    raw = llm_helper.chat(
        messages=[{"role": "user", "content": user_prompt}],
        system_content=_interview_system_prompt(ASSESS_SYS_PROMPT, use_prior_knowledge),
        temperature=0.2,
        max_tokens=2000,  # same truncation risk as analyze_round on a long session
    )
    parsed = parse_json_loose(raw)
    if not parsed:
        print(f"⚠️  assess_readiness: could not parse LLM output as JSON ({len(raw)} chars). "
              f"Raw tail: ...{raw[-200:]!r}")
        return None
    return _parse_assessment(parsed)


# "Teach this step" — USER-LED, scoped to ONE specific filter edit, triggered
# by clicking a step's own teach icon in the Steps panel (the panel's default
# click stays purely navigational — see toggleStepPanel's docstring in
# log_viewer.html). The engineer leads: they write, in their own words, what
# key thing they noticed and what the problem/reasoning was — NOT the LLM
# firing a question first. The LLM's job is then to (a) condense that into one
# crisp "knowledge core" statement handed back for the engineer to CONFIRM
# (never invent facts they didn't say), and (b) add its own expert
# perspective — a genuine second opinion, not a restatement. A follow-up
# question is optional and secondary, only when something is materially
# unclear — the interaction is not built around it.
CONFIRM_STEP_SYS_PROMPT = """\
You are a senior Wi-Fi/BT debug expert. The engineer just explained, IN THEIR
OWN WORDS, the key thing they noticed and the problem/reasoning behind ONE
specific filter edit they made. Your job:

1. CONDENSE their explanation into ONE crisp knowledge-core sentence — the
   distilled rule/fact, phrased so it's ready to be handed back to them for
   confirmation. Stay strictly faithful to what they actually said; do NOT
   invent thresholds, mechanisms, or claims they didn't state.
2. Add your OWN expert perspective as a genuine second opinion — is this
   consistent with the measured effect? Any related risk, edge case, or
   connection worth flagging? This must add something, not just restate their
   words back.
3. ONLY if something material is genuinely unclear or seems to conflict with
   the measured effect, add ONE optional follow-up question — omit the field
   entirely (empty string) when nothing is missing. This is secondary; do not
   default to asking one just to fill the slot.

Output ONLY this JSON object, no markdown fences, no extra prose:
{
  "knowledge_core": "<one crisp sentence — the distilled rule/knowledge, for the engineer to confirm>",
  "expert_note": "<your own analysis/perspective as a second opinion — 1-2 sentences>",
  "follow_up_question": "<optional — omit or leave empty if nothing genuinely unclear>"
}
"""


def _build_confirm_step_context_block(op_context: Dict, user_explanation: str) -> str:
    lines = [
        f"Domain: {(op_context.get('domain') or 'wifi').upper()}.",
        f"The edit: {op_context['verb']} \"{op_context['target']}\""
        + (" (an EXCLUDE/noise filter)" if op_context.get("excluding") else " (an INCLUDE/keyword filter)"),
    ]
    if op_context.get("effect_phrase"):
        lines.append(f"Measured effect: {op_context['effect_phrase']}")
    existing = op_context.get("existing_skills") or []
    if existing:
        rows = "\n".join(
            f"  - {s['name']}: {s['description']}"
            + (f" | keywords: {', '.join(s['keywords'][:10])}" if s.get('keywords') else "")
            for s in existing
        )
        lines.append(
            "Existing skills, same domain (already-known team knowledge — the "
            "knowledge core should be new/distinct from these, not a restatement "
            "of what they already cover):\n" + rows
        )
    lines.append(f"Engineer's own explanation of this edit:\n\"{user_explanation}\"")
    return "\n".join(lines)


def confirm_step_knowledge(llm_helper, op_context: Dict, user_explanation: str,
                            use_prior_knowledge: bool = False) -> Dict:
    """User-led counterpart to the old probe-first flow: the engineer has
    ALREADY written their own explanation of ONE filter edit (what they
    noticed + the problem) before this is ever called. Condenses it into a
    confirmable `knowledge_core` + the LLM's own `expert_note`, plus an
    OPTIONAL `follow_up_question` (see CONFIRM_STEP_SYS_PROMPT). Cheap and
    scoped to just this one edit, same budget class as the old probe call.

    `op_context` = {domain, verb, target, excluding, effect_phrase,
    existing_skills}. `use_prior_knowledge` mirrors the session's sticky
    conversation mode — when True the caller must have already populated
    `existing_skills` (same-domain, via learning_routes.
    _other_skill_descriptions) so the knowledge core stays distinct from what
    those skills already cover."""
    user_prompt = _build_confirm_step_context_block(op_context, user_explanation)
    raw = llm_helper.chat(
        messages=[{"role": "user", "content": user_prompt}],
        system_content=_interview_system_prompt(CONFIRM_STEP_SYS_PROMPT, use_prior_knowledge),
        temperature=0.3,
        max_tokens=700,  # one edit, a short summary + note — a fraction of a full round's budget
    )
    parsed = parse_json_loose(raw)
    if not parsed:
        print(f"⚠️  confirm_step_knowledge: could not parse LLM output as JSON ({len(raw)} chars). "
              f"Raw tail: ...{raw[-200:]!r}")
    knowledge_core = str((parsed or {}).get("knowledge_core") or "").strip()
    if not knowledge_core:
        knowledge_core = "⚠️ Couldn't condense this step — try again, or just keep your explanation as-is."
    return {
        "knowledge_core": knowledge_core,
        "expert_note": str((parsed or {}).get("expert_note") or "").strip(),
        "follow_up_question": str((parsed or {}).get("follow_up_question") or "").strip(),
    }


# "Ask about this step" — the LLM-LED counterpart to confirm_step_knowledge's
# user-led flow, for engineers who'd rather answer a targeted question than
# write free text. Triggered by a step's own ❓ icon (separate from 🎓's
# user-led explain box — the engineer picks whichever entry point suits them).
# Deliberately ONE question, always skippable client-side, never the primary
# interaction — see log_viewer.html's askStepQuestion/renderStepAskCard.
ASK_STEP_SYS_PROMPT = """\
You are a senior Wi-Fi/BT debug expert. Ask ONE short, specific question about
ONE filter edit the engineer just made — grounded in its measured effect —
that draws out exactly WHY they made it and what domain knowledge justifies
it. Nothing else: no analysis, no assessment, no second question.

The question is EITHER "open" (genuinely open-ended) or "choice" (give 2-4
concrete options in the engineer's own domain vocabulary — never add an
"Other"/"Skip" option yourself, the UI already offers both).

Output ONLY this JSON object, no markdown fences, no extra prose:
{"question": "...", "type": "open"}
or
{"question": "...", "type": "choice", "options": ["...", "..."]}
"""


def _build_ask_step_context_block(op_context: Dict) -> str:
    lines = [
        f"Domain: {(op_context.get('domain') or 'wifi').upper()}.",
        f"The edit: {op_context['verb']} \"{op_context['target']}\""
        + (" (an EXCLUDE/noise filter)" if op_context.get("excluding") else " (an INCLUDE/keyword filter)"),
    ]
    if op_context.get("effect_phrase"):
        lines.append(f"Measured effect: {op_context['effect_phrase']}")
    if op_context.get("reason"):
        lines.append(
            f"Reason already given: \"{op_context['reason']}\" — ask about "
            "something ELSE useful (a specific implication or edge case), not "
            "the same thing again."
        )
    else:
        lines.append("No reason recorded yet for this edit.")
    existing = op_context.get("existing_skills") or []
    if existing:
        rows = "\n".join(
            f"  - {s['name']}: {s['description']}"
            + (f" | keywords: {', '.join(s['keywords'][:10])}" if s.get('keywords') else "")
            for s in existing
        )
        lines.append(
            "Existing skills, same domain (already-known team knowledge — don't "
            "ask about anything these already cover):\n" + rows
        )
    return "\n".join(lines)


def ask_step_question(llm_helper, op_context: Dict, use_prior_knowledge: bool = False) -> Optional[Dict]:
    """Generate ONE targeted question about a single filter edit — see
    ASK_STEP_SYS_PROMPT. Returns {question, type, options} (via
    _normalize_question, same shape every other question card uses), or None
    on total parse failure (caller should surface that as a plain error, there
    is no partial/default question worth showing)."""
    user_prompt = _build_ask_step_context_block(op_context)
    raw = llm_helper.chat(
        messages=[{"role": "user", "content": user_prompt}],
        system_content=_interview_system_prompt(ASK_STEP_SYS_PROMPT, use_prior_knowledge),
        temperature=0.3,
        max_tokens=300,  # one short question — the smallest call in this file
    )
    parsed = parse_json_loose(raw)
    if not parsed:
        print(f"⚠️  ask_step_question: could not parse LLM output as JSON ({len(raw)} chars). "
              f"Raw tail: ...{raw[-200:]!r}")
        return None
    return _normalize_question(parsed)


def _normalize_skill_draft(raw: Dict, context: Dict) -> Dict:
    """Apply the same field defaults to ONE skill entry from the LLM's
    `skills` array — shared so every draft (whether the LLM emitted one or
    split into several) ends up with the same guaranteed shape. Every draft is
    a brand-new skill (no is_new/target_skill_key routing) — the caller files
    it as a fresh entry."""
    raw = dict(raw or {})
    raw.setdefault("name", "New_Skill")
    raw.setdefault("description", "")
    raw.setdefault("keywords", context.get("keywords") or [])
    raw.setdefault("exclusive", context.get("excluding_terms") or [])
    raw.setdefault("expert_rules", "")
    raw.setdefault("note", "")
    return raw


def synthesize_skill_draft(llm_helper, context: Dict, qa_pairs: List[Dict],
                            use_prior_knowledge: bool = False) -> Dict:
    """Returns {"drafts": [<skill dict>, ...]} — USUALLY one entry, but the
    LLM may split the conversation into several when it genuinely covers more
    than one mutually-exclusive scenario (see the split rule). Every entry is
    a BRAND-NEW skill: name/description/keywords/exclusive/expert_rules/note.

    `use_prior_knowledge` picks the mode (mirrors the two Export buttons):
      • False (FRESH)  — the existing-skills context is NOT consulted; skills
        are scoped/split purely by what the conversation covers.
      • True  (PRIOR)  — the same-domain existing skills (already in
        context["existing_skills"], populated by the caller) are shown ONLY so
        the new skills stay mutually exclusive from them; still never edits an
        existing skill.
    """
    context_block = _build_context_block(context)
    qa_block = "\n".join(f"Q: {qa['question']}\nA: {qa['answer']}" for qa in qa_pairs)
    user_prompt = f"{context_block}\n\nEngineer Q&A:\n{qa_block}"
    system_content = SYNTHESIS_SYS_PROMPT_PRIOR if use_prior_knowledge else SYNTHESIS_SYS_PROMPT_FRESH
    raw = llm_helper.chat(
        messages=[{"role": "user", "content": user_prompt}],
        system_content=system_content,
        temperature=0.2,
        # A rich expert_rules block (e.g. extending a skill that already has a
        # multi-step numbered flow, like connection_flow) can alone run well
        # past 1500 tokens — that cap was silently truncating the JSON
        # mid-string, which made parse_json_loose() fail closed and fall back
        # to every field's empty default (is_new/target_skill_key/expert_rules
        # all blank) with no visible error. 4000 gives real headroom, and a
        # multi-skill split needs even more, hence 5000.
        max_tokens=5000,
    )
    parsed = parse_json_loose(raw)
    if not parsed:
        # parse_json_loose() fails closed (empty dict) on truncated/malformed
        # JSON — that used to look identical to "the LLM had nothing to say"
        # since every field then silently took its blank default. Surface it.
        print(f"⚠️  synthesize_skill_draft: could not parse LLM output as JSON "
              f"({len(raw)} chars received). Falling back to defaults. "
              f"Raw tail: ...{raw[-200:]!r}")

    raw_list = parsed.get("skills") if isinstance(parsed, dict) else None
    if not isinstance(raw_list, list) or not raw_list:
        # Tolerate the model returning the old single-object shape (or a
        # totally failed parse) instead of sinking the whole export.
        raw_list = [parsed] if parsed else [{}]
    drafts = [_normalize_skill_draft(d, context) for d in raw_list if isinstance(d, dict)] or \
        [_normalize_skill_draft({}, context)]
    return {"drafts": drafts}

