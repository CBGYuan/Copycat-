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
from typing import Dict, List, Optional, Tuple

from utils.json_utils import parse_json_loose
from . import skill_retrieval
from .skill_service import Skill

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
2. CLARIFY ONLY ON BEHAVIORAL DIVERGENCE. First form 2-3 plausible, concrete
    interpretations of the most important unexplained edit or surviving log
    pattern. For each interpretation, state which lines it would keep/drop or
    which conclusion it would produce. If all interpretations produce the same
    behavior for the available evidence, set requires_clarification=false and
    ask NO question. If they diverge, set requires_clarification=true and ask
    exactly ONE short question whose answer selects between those behaviors.
    The question must be grounded in ONE
   concrete piece of evidence, either:
   (a) an edit from the operation journal — ask WHY the engineer made a
       specific still-unexplained one (e.g. "you excluded 'Mcc' which dropped
       300 lines — is that always noise, or only in this scenario?"), or
   (b) an actual line/token quoted or closely paraphrased from "Sample
       surviving log lines" — ask what it MEANS or what should be concluded
       from it (e.g. "line 3 shows 'stateMachineSetStateNoCurrentFlow' right
       before the disconnect — is that string itself diagnostic, or just
       incidental to whatever precedes it?").
   Do not ask generic questions about hit counts or co-occurrence in isolation
   (e.g. "why do these two keywords co-fire?") — a number alone is not
   evidence of meaning; anchor to what an edit or a real log line actually
   shows. Favor (b) whenever "Sample surviving log lines" contains a line
   relevant to this round's delta — that is the richer, more specific signal.
   If you can identify the closest existing skill from the "Existing skills"
   list, name it explicitly.
3. """ + _ASSESS_TASKS + """

""" + _SKILL_GOALS + """

The single question, when needed, is EITHER:
 - "open": genuinely open-ended — no small fixed set of sensible answers.
 - "choice": you can already enumerate the realistic answers (e.g.
   extend-vs-new, which keyword is load-bearing, which of two thresholds
   applies) — give 2-4 concrete options in the engineer's own domain
   vocabulary. Do NOT add an "Other" option yourself, the UI already offers a
   free-text fallback on every choice question.

Output ONLY this JSON object, no markdown fences, no extra prose:
{
  "analysis": "<2-4 sentences>",
    "ambiguity": {"requires_clarification": <true|false>, "divergent_behaviors": ["<interpretation and observable result>", "..."]},
    "questions": [{"question": "<one discriminating question>", "type": "choice", "options": ["<behavior A>", "<behavior B>"]}],
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

# The BASELINE read: a committed, falsifiable interpretation of the default
# filter set, formed BEFORE the engineer has touched anything.
#
# Why it must be structured rather than prose: this object is the null
# hypothesis that every later engineer action is diffed against (see
# analyze_baseline). Prose can't be diffed — "did the engineer disable
# something this read called load-bearing?" is only answerable if the read
# committed to a NAMED list up front. That diff is what turns the
# ClarifyGPT ambiguity gate from LLM-self-reported ("do I feel uncertain?")
# into an observable divergence between two concrete interpretations.
#
# `open_unknowns` is load-bearing for the same reason, in the opposite
# direction: it's what lets a later engineer action be classified as an
# OMISSION (the read never had a view on this) rather than a CONTRADICTION
# (the read committed, and the engineer went the other way). Only the latter
# deserves a discriminating question; omissions are provenance gaps and
# belong to the Steps panel's own explain path.
#
# Deliberately asks NO questions: nobody has said anything to clarify yet.
BASELINE_SYS_PROMPT = """\
You are a senior Wi-Fi/BT debug expert. A log has just been opened with a
filter set that the engineer has NOT yet modified — it came wholesale from a
.tat file or a saved skill. Nobody has explained anything to you yet.

Give your OWN first read of what this default filter appears to be looking
for. This read is a COMMITTED PREDICTION: the engineer is about to start
adjusting the filter, and their adjustments will be compared against what you
say here. So be specific and falsifiable, and keep the two halves honest:

 - Things you genuinely believe from the evidence in front of you go in
   `expected_key_keywords` / `expected_noise_keywords` / `expected_scenario`.
   Name ACTUAL keyword strings from the filter set shown below, never invented
   ones. Ground each `why` in a hit count, a co-occurrence, or a sample line,
   in ONE short clause.
 - Things the default filter alone genuinely cannot tell you go in
   `open_unknowns`. Do NOT pad the believed lists with guesses to look
   thorough — a wrong "key keyword" here manufactures a fake disagreement
   later, while an honest unknown costs nothing. When in doubt, put it in
   `open_unknowns`.

Be STRICT about what counts as key. List a keyword in `expected_key_keywords`
only if you would actively push back were the engineer to disable it — that
is exactly how this list gets used. Two consequences:
 - A keyword whose hits are almost entirely already caught by other keywords
   (low `unique_hits` relative to `hits`) is REDUNDANT, not load-bearing,
   however structurally important it looks. It does not belong in the key
   list.
 - Listing every include-keyword as key is always wrong. If the filter set
   really is uniformly essential, say so in `open_unknowns` instead and keep
   the key list to the ones you would genuinely defend.
A high hit count alone is not evidence of importance — a very noisy keyword
may equally be the housekeeping chatter the engineer is about to cut.

Also say how you WOULD locate the moment the issue happened in a log like
this (`expected_issue_time_hint`) — e.g. which line pattern or which event
would mark it. If nothing in the evidence supports a guess, use null.

DO NOT ask the engineer any questions. There is no answer to clarify against
yet; your job here is only to commit to a starting interpretation.

ASSESS the starting point honestly: no engineer knowledge has been
contributed yet, so readiness and the knowledge/scope coverage scores must
reflect the default filter ALONE and start LOW. `gaps` = what a reusable
skill would still need beyond what the filter itself shows. `validation` MUST
be an empty array — the engineer has asserted nothing yet, so there is
nothing to sanity-check.

""" + _SKILL_GOALS + """

Output ONLY this JSON object, no markdown fences, no extra prose:
{
  "analysis": "<2-4 sentences, your first read, for the engineer to see>",
  "expected_scenario": "<ONE sentence: the scenario this default filter appears to target>",
  "expected_key_keywords": [{"text": "<exact keyword from the filter set>", "why": "<grounded in a hit count / co-occurrence / sample line>"}],
  "expected_noise_keywords": [{"text": "<exact keyword from the filter set>", "why": "<why you read it as low-signal>"}],
  "expected_issue_time_hint": "<how you would locate the issue moment, or null>",
  "open_unknowns": ["<what the default filter alone cannot tell you>", "..."],
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


# Asked when the engineer's action CONTRADICTS the committed baseline read
# (utils.divergence: the read called a keyword load-bearing and they cut it,
# or called it noise and they promoted it).
#
# This is the one place in the app that genuinely meets ClarifyGPT's bar
# without having to ask a model to introspect about its own uncertainty: two
# concrete readings are already on the table and they keep DIFFERENT lines,
# which is an observable behavioural difference, not a feeling. So the prompt
# doesn't need to establish that a question is warranted — divergence.detect
# already established it — and its only job is to make the one question
# actually discriminate.
#
# The scope axis ("always, or only in this scenario?") is mandatory because it
# is what the skill needs: "always noise" makes the keyword a permanent
# `exclusive` entry, "only here" makes it a conditional expert_rule. A
# question that merely extracts "because it's noise" leaves that undecided and
# the export has to guess.
#
# Tone matters for a non-obvious reason: this system captures teaching, it does
# not adjudicate correctness (correctness is measured externally, by running
# the skill in wireless_ce_avatar). A question implying the engineer must
# justify themselves against the model's read would both misrepresent that
# boundary and, in practice, get shorter and more defensive answers.
CLARIFY_SYS_PROMPT = """\
You are a senior Wi-Fi/BT debug expert. Earlier you committed to a first read
of a filter set. The engineer has since done something your read did not
expect. Both readings are plausible; they simply keep different log lines.

Ask exactly ONE question that DISCRIMINATES between them — its answer must
settle which behaviour is right for a reusable skill. Requirements:

 - Ground it in the specific keyword and its MEASURED effect (the hit counts
   below), not in generalities.
 - Cover the scope axis: does the engineer's treatment hold ALWAYS for this
   kind of log, or ONLY in this scenario? This is the part a reusable skill
   cannot be written without.
 - Options, when you use them, must be BEHAVIOURS ("always exclude it",
   "exclude only when X is present"), never opinions ("it's noise").
 - Do NOT ask the engineer to justify themselves and do NOT defend your
   earlier read. You are not judging who is correct — you are capturing what
   the engineer knows that your read did not. Never imply either side is
   wrong.
 - No preamble, no apology, no restating the situation back to them. One
   question, in an engineer's register.
 - LENGTH: the question itself must be at most 25 words and a single sentence,
   and each option at most 12 words. It renders in a narrow chat column, and a
   paragraph-length question with four clause-heavy options gets skipped
   rather than answered — which costs you the knowledge entirely. Put the
   nuance in the options' distinctions, not in a longer question stem.

Question `type` is either:
 - "choice": you can enumerate the realistic behaviours — give 2-4 concrete
   options. Do NOT add an "Other" option; the UI already offers free text.
 - "open": genuinely open-ended, no small fixed set of sensible answers.

Output ONLY this JSON object, no markdown fences, no extra prose:
{
  "question": {"question": "<one discriminating question>", "type": "choice|open", "options": ["<behaviour A>", "<behaviour B>"]},
  "captures": "<the one short sentence of skill knowledge this answer will pin down>"
}
"""

# Asked once when the engineer sets an issue-time focus window. Not a
# contradiction — a focus doesn't change what any keyword means — but "how did
# you know to look at 09:41?" is often the most transferable thing in a whole
# triage session, and it is invisible in the filter set, so nothing else in
# this pipeline would ever capture it.
CLARIFY_FOCUS_SYS_PROMPT = """\
You are a senior Wi-Fi/BT debug expert. The engineer has narrowed the log to a
window around a specific issue time. How they knew to look THERE is reusable
diagnostic knowledge that the filter set alone does not encode.

Ask exactly ONE short question that pins down the LOCATING RULE — what signal
told them the problem was at that moment, in a form someone else could follow
on a different log of the same kind. If your own read proposed a way to locate
the issue and they picked a different moment, contrast the two concretely
rather than asking a blank "why". Do not ask them to justify the choice; ask
what the tell was.

LENGTH: the question itself must be at most 25 words and a single sentence,
and each option at most 12 words. It renders in a narrow chat column, and a
paragraph-length question with clause-heavy options gets skipped rather than
answered — which costs you the knowledge entirely. Put the nuance in the
options' distinctions, not in a longer question stem.

Output ONLY this JSON object, no markdown fences, no extra prose:
{
  "question": {"question": "<one question>", "type": "choice|open", "options": ["<concrete locating signal A>", "<B>"]},
  "captures": "<the one short sentence of skill knowledge this answer will pin down>"
}
"""


def clarify_divergence(llm_helper, target: Dict, use_prior_knowledge: bool = False) -> Optional[Dict]:
    """One discriminating question about ONE divergence (utils.divergence).

    `target` is either
      {"kind": "contradiction", "text", "action_phrase", "effect_phrase",
       "baseline_stance", "baseline_why", "domain"}
    or
      {"kind": "focus", "center", "window_min", "baseline_hint", "domain"}

    Returns {"question": {...}, "captures": str} or None if the model produced
    nothing usable — None is a normal outcome the caller must handle by simply
    not interrupting, never by falling back to a generic question (a
    non-discriminating question is exactly what the ambiguity gate exists to
    prevent).
    """
    if target.get("kind") == "focus":
        system = CLARIFY_FOCUS_SYS_PROMPT
        lines = [
            f"Domain: {(target.get('domain') or 'wifi').upper()}",
            f"The engineer focused on: {target.get('center')} (±{target.get('window_min')} minutes)",
        ]
        if target.get("baseline_hint"):
            lines.append("Your earlier read proposed locating the issue this way:\n"
                         f"  {target['baseline_hint']}")
        else:
            lines.append("Your earlier read had no proposal for locating the issue moment.")
    else:
        system = CLARIFY_SYS_PROMPT
        stance = target.get("baseline_stance")
        stance_txt = ("load-bearing for this scenario" if stance == "key"
                      else "low-signal noise")
        lines = [
            f"Domain: {(target.get('domain') or 'wifi').upper()}",
            f"Keyword: \"{target.get('text')}\"",
            f"Your earlier read called it {stance_txt}, because: {target.get('baseline_why') or '(no reason recorded)'}",
            f"What the engineer just did: {target.get('action_phrase')}",
            f"Measured effect of that action: {target.get('effect_phrase') or '(no effect measured)'}",
        ]

    raw = llm_helper.chat(
        messages=[{"role": "user", "content": "\n\n".join(lines)}],
        system_content=_interview_system_prompt(system, use_prior_knowledge),
        temperature=0.3,
        max_tokens=700,
    )
    parsed = parse_json_loose(raw)
    if not parsed:
        print(f"⚠️  clarify_divergence: could not parse LLM output as JSON ({len(raw)} chars).")
        return None
    question = _normalize_question(parsed.get("question"))
    if not question:
        return None
    return {"question": question, "captures": str(parsed.get("captures") or "").strip()}


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

    # The LOADED skill, called out separately from the list above. It is not
    # just one more skill to stay clear of: the export will physically copy all
    # of its keywords into the new skill (flat inheritance — see
    # utils.skill_dedup.build_extension_skill), so the two WILL be
    # keyword-identical apart from what this session added, and the
    # description is the only field left that can distinguish them. One extra
    # line of prompt, in exchange for the failure it prevents.
    baseline = context.get("baseline_skill")
    if baseline:
        lines.append(
            f"BASELINE SKILL (currently loaded): {baseline['name']} "
            f"(key: {baseline['key']}) — {baseline['description']}\n"
            "  The skill you emit will INHERIT every keyword of this baseline, so a "
            "downstream agent choosing between them sees two entries with the same "
            "filters and can only go by the description. Write this skill's "
            "`description` so it names the narrower situation THIS session is about, "
            "and so a reader could decide which of the two to reach for without "
            "looking at anything else. Never restate or lightly reword the baseline's "
            "description."
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


def _annotation_coverage(annotations) -> int:
    """Deterministic 0-100 evidence-coverage score from the engineer's own
    Log-line labels (see log_viewer.annotate_line/_step) -- unlike the other
    three coverage dimensions (knowledge/scope/keywords), this one is NEVER
    LLM-judged, so labeling a line always moves this number, predictably,
    regardless of what the LLM does with the rest of its output this round.
    Ramps up with labeled evidence lines, but is capped below full marks
    until at least one counterexample has ALSO been flagged -- nudges toward
    checking for an edge case (the ASI-inspired verification concern) rather
    than just rewarding volume of clicks.
    """
    annotations = annotations or []
    evidence_n = sum(1 for a in annotations if a.get("label") == "evidence")
    counterexample_n = sum(1 for a in annotations if a.get("label") == "counterexample")
    if evidence_n == 0:
        return 0
    base = min(90, evidence_n * 15)
    return min(100, base + 10) if counterexample_n else base


def _parse_assessment(parsed: Dict, annotations=None) -> Dict:
    """Pull the readiness / coverage / gaps / validation block out of a parsed
    LLM object — shared by analyze_round (Log Round) and assess_readiness
    (per chat answer) so both surface the exact same shape to the UI.

    `annotations` (state.log_annotations) feeds coverage.evidence — the ONE
    dimension of the four that is computed here, not asked of the LLM, so
    that labeling Log lines has a guaranteed, visible effect on Readiness
    instead of only maybe being noticed by the model's own judgment.
    """
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
            "evidence": _annotation_coverage(annotations),
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
    ambiguity = (parsed or {}).get("ambiguity") or {}
    divergent = [str(item).strip() for item in ambiguity.get("divergent_behaviors", []) if str(item).strip()]
    requires_clarification = bool(ambiguity.get("requires_clarification")) and len(divergent) >= 2
    questions = [n for n in (_normalize_question(q) for q in ((parsed or {}).get("questions") or [])) if n]
    questions = questions[:1] if requires_clarification else []
    analysis = str((parsed or {}).get("analysis") or "").strip()
    if not analysis:
        analysis = "⚠️ This round's response couldn't be parsed — try Log Round & Analyze again."
    return {
        "analysis": analysis,
        "questions": questions,
        "ambiguity": {
            "requires_clarification": requires_clarification,
            "divergent_behaviors": divergent if requires_clarification else [],
        },
        "assessment": _parse_assessment(parsed, context.get("log_annotations")) if parsed else None,
    }


def _normalize_prediction(raw_list, known_texts: Dict[str, str]) -> List[Dict]:
    """Coerce one of the baseline's predicted keyword lists into
    [{text, why}], keeping ONLY entries whose text actually exists in the
    current filter set.

    The filter is the point, not defensive tidying: this list is the thing
    later engineer actions get diffed against, so an invented or paraphrased
    keyword would manufacture a permanent phantom disagreement that the
    engineer can never resolve (they can't "restore" a keyword that was never
    in their filter). Matching is case-insensitive against the filter's own
    spelling, and the stored `text` is snapped back to that spelling so the
    diff compares like with like.
    """
    out: List[Dict] = []
    seen = set()
    for item in raw_list or []:
        if isinstance(item, str):
            text, why = item.strip(), ""
        elif isinstance(item, dict):
            text, why = str(item.get("text") or "").strip(), str(item.get("why") or "").strip()
        else:
            continue
        canonical = known_texts.get(text.lower())
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        out.append({"text": canonical, "why": why})
    return out


def analyze_baseline(llm_helper, context: Dict, use_prior_knowledge: bool = False) -> Dict:
    """The LLM's own first read of a freshly-loaded default filter set, before
    the engineer has edited anything — a committed, structured prediction plus
    a starting assessment.

    This is the null hypothesis the rest of the session is measured against.
    `expected_key_keywords` / `expected_noise_keywords` are what make the
    later divergence check deterministic (the engineer disabling something
    listed as key is an observable CONTRADICTION, no LLM judgment needed),
    and `open_unknowns` is what keeps an OMISSION — an engineer action on
    something this read had no view about — from being mistaken for one.

    Returns the same {analysis, ..., assessment} envelope shape as
    analyze_round, including assessment=None on a total parse failure, so
    callers can treat both uniformly. Asks no questions by construction.
    """
    user_prompt = _build_context_block(context)
    raw = llm_helper.chat(
        messages=[{"role": "user", "content": user_prompt}],
        system_content=_interview_system_prompt(BASELINE_SYS_PROMPT, use_prior_knowledge),
        temperature=0.3,
        max_tokens=3000,
    )
    parsed = parse_json_loose(raw)
    if not parsed:
        print(f"⚠️  analyze_baseline: could not parse LLM output as JSON ({len(raw)} chars). "
              f"Raw tail: ...{raw[-200:]!r}")
    parsed = parsed or {}

    # Every keyword currently in the filter set, by lowercased text -> the
    # filter's own spelling. Both include- and exclude-side entries count:
    # the baseline is allowed to have a view on a noise term too.
    known_texts = {}
    for pf in ((context.get("filter_stats") or {}).get("per_filter") or []):
        text = str(pf.get("text") or "").strip()
        if text:
            known_texts.setdefault(text.lower(), text)

    analysis = str(parsed.get("analysis") or "").strip()
    if not analysis:
        analysis = "⚠️ The baseline read couldn't be parsed — the filter is loaded, but there's no starting interpretation to compare against."
    hint = parsed.get("expected_issue_time_hint")
    hint = str(hint).strip() if hint and str(hint).strip().lower() not in ("null", "none") else ""

    return {
        "analysis": analysis,
        "expected_scenario": str(parsed.get("expected_scenario") or "").strip(),
        "expected_key_keywords": _normalize_prediction(parsed.get("expected_key_keywords"), known_texts),
        "expected_noise_keywords": _normalize_prediction(parsed.get("expected_noise_keywords"), known_texts),
        "expected_issue_time_hint": hint,
        "open_unknowns": [str(u).strip() for u in (parsed.get("open_unknowns") or []) if str(u).strip()],
        "assessment": _parse_assessment(parsed, context.get("log_annotations")) if parsed else None,
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
    return _parse_assessment(parsed, context.get("log_annotations"))


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


def assess_teaching_evidence(draft: Dict, context: Dict) -> Dict:
    """Summarize the teaching evidence behind a candidate skill draft.

    This deliberately does NOT judge whether the skill is correct — it never
    computes TP/FP/FN or a pass/fail verdict. That judgment belongs to
    wireless_ce_avatar (see build_validation_packet / apply_external_validation
    below), which runs the skill against real issues and logs and is the only
    place "a good skill" can actually be measured. This function only reports
    what evidence THIS teaching session produced: which keywords were
    actually exercised on the log, whether every material edit has an
    engineer explanation (operation reason / source Steps), and whether any
    counterexamples were flagged — pure provenance, for the engineer to review
    before Save, not a correctness score.
    """
    stats = context.get("filter_stats") or {}
    quality = keyword_quality_map(stats)
    quality_ci = {key.casefold(): value for key, value in quality.items()}
    keywords = [str(value).strip() for value in draft.get("keywords", []) if str(value).strip()]
    exclusive = [str(value).strip() for value in draft.get("exclusive", []) if str(value).strip()]
    checks = []

    used = []
    unused = []
    for value in keywords:
        measured = quality_ci.get(value.casefold())
        if measured and ((measured.get("hits") or 0) > 0 or (measured.get("unique_hits") or 0) > 0):
            used.append(value)
        else:
            unused.append(value)
    checks.append({
        "name": "Operation reason: keywords exercised on this log",
        "status": "pass" if used else "info",
        "note": f"Measured matches: {', '.join(used)}" if used else "No candidate include keyword produced a measured match this session.",
    })

    effective_excludes = []
    for value in exclusive:
        measured = quality_ci.get(value.casefold())
        if measured and (measured.get("dropped") or 0) > 0:
            effective_excludes.append(value)
    checks.append({
        "name": "Scope completeness: exclude terms measured",
        "status": "pass" if not unused else "info",
        "note": ("All include keywords were measured." if not unused else
                 f"No measured contribution here: {', '.join(unused)}"),
    })

    journal = context.get("operation_journal") or ""
    unexplained = context.get("unreasoned_ops") or []
    checks.append({
        "name": "Source Steps: teaching provenance captured",
        "status": "pass" if journal and not unexplained else "info",
        "note": "Every material edit has an explanation." if journal and not unexplained else
                f"{len(unexplained)} material edit(s) still lack an explanation.",
    })

    annotations = context.get("log_annotations") or []
    counterexamples = [a for a in annotations if a.get("label") == "counterexample"]
    checks.append({
        "name": "Counterexamples flagged",
        "status": "pass" if counterexamples else "info",
        "note": (f"{len(counterexamples)} counterexample(s) recorded — carry these into "
                 "wireless_ce_avatar so the rule doesn't over-generalize.") if counterexamples else
                "None flagged yet. Not required, but worth checking for edge cases before Save.",
    })

    return {
        "status": "assessed",
        "summary": "Teaching evidence recorded for this session. Correctness is validated externally.",
        "checks": checks,
        "labeled_examples": len(annotations),
        "counterexample_count": len(counterexamples),
        "positive_keywords": used,
        "effective_excludes": effective_excludes,
        "external_validation": "not_run",
        "limitations": ["TP/FP/FN correctness is only ever measured by running this skill in wireless_ce_avatar."],
    }


def build_validation_packet(draft: Dict, context: Dict) -> Dict:
    """Package a skill draft + its teaching evidence for external validation.

    wireless_ce_avatar is the system of record for "is this a good skill": it
    runs the candidate against real issues/logs and reports outcomes. This
    workbench never computes that itself — it only prepares what that system
    needs (the rule + the provenance behind it) and, later, stores whatever
    comes back (see apply_external_validation).
    """
    return {
        "skill_key": draft.get("skill_key") or draft.get("name"),
        "domain": draft.get("domain"),
        "keywords": draft.get("keywords", []),
        "exclusive": draft.get("exclusive", []),
        "expert_rules": draft.get("expert_rules", ""),
        "teaching_evidence": {
            "labeled_examples": context.get("log_annotations") or [],
            "operation_journal": context.get("operation_journal") or "",
        },
    }


def apply_external_validation(skill_or_draft: Dict, validation_result: Dict) -> Dict:
    """Attach a validation result reported back by wireless_ce_avatar.

    Never computed locally — this only stores the outcome (TP/FP/FN, failure
    cases) so the engineer can see it in the Skill Editor and use failure
    cases as the starting point for the next round of teaching.
    """
    skill_or_draft["external_validation"] = {
        "status": validation_result.get("status", "validated"),
        "validation_system": validation_result.get("validation_system", "wireless_ce_avatar"),
        "evaluated_cases": validation_result.get("evaluated_cases"),
        "true_positive": validation_result.get("true_positive"),
        "false_positive": validation_result.get("false_positive"),
        "false_negative": validation_result.get("false_negative"),
        "failure_cases": validation_result.get("failure_cases", []),
    }
    return skill_or_draft


# ============================================================================
# Phase 2 — AutoSkill-style retrieval-assisted skill management
# (Agent C retrieval -> Agent B judge -> Agent D baseline merge)
#
# synthesize_skill_draft() above always hands back a BRAND-NEW draft; this
# section decides, per draft, whether that draft should actually become a
# new skill, fold into an existing one, or be flagged as likely redundant —
# advisory only, never auto-writing: the caller (learning_routes.converge)
# attaches the decision to the draft and the engineer still confirms/edits
# in the Edit-Skill modal before anything is saved.
#
# Two DISTINCT signals feed the decision, deliberately kept separate:
#   - continuity: `state.active_skill_key`, the ONE skill the engineer
#     explicitly loaded this session as a filter baseline. Strong intent
#     signal, but NOT auto-trusted — it still has to pass the same
#     similarity + judge gate as anything else (see route_draft's Tier 0).
#     A candidate that drifted onto an unrelated topic mid-session must not
#     be force-merged into whatever happened to be loaded at the start.
#   - retrieval: Agent C's domain-wide top-M search (skill_retrieval), used
#     whenever continuity doesn't apply or doesn't pass its own gate.
# ============================================================================

JUDGE_SYS_PROMPT = """\
You are the Skill Set Manager for a Wi-Fi/BT log-analysis skill library.
Task: decide how to file a newly synthesized candidate skill given the most
similar EXISTING skills already in the library (same domain).

Output ONLY strict JSON, no markdown, no commentary:
{"action": "add"|"merge"|"discard", "target_skill_key": "<key or null>", "reason": "<one short sentence>"}

Decide in this order:
1. Capability-overlap gate: if the candidate targets the SAME diagnostic
   scenario as an existing skill (same root-cause family / same log pattern
   it isolates), after ignoring wording or keyword-order differences, action
   MUST be "merge" into that skill's key — never "add".
2. Discard gate: choose "discard" ONLY if the candidate adds nothing beyond
   what an existing skill already fully covers (same keywords, same rules,
   no new constraint). A candidate with even one genuinely new keyword or
   rule should be "merge", not "discard".
3. Otherwise, if the candidate is a genuinely distinct diagnostic scenario
   from every skill shown (different root cause / different log signature),
   choose "add".

If "candidate_keyword_quality" is present, it is GROUND-TRUTH measured
evidence from actually running the candidate's keywords against this
session's real log — not the model's own guess. unique_hits=0 on an include
keyword means it matched zero lines that weren't ALREADY caught by another
enabled keyword this session (redundant, even if it reads as novel);
dropped=0 on an exclude term means it removed nothing. Weigh this evidence
heavily in the discard gate: a candidate whose EVERY keyword measures zero
is much stronger discard evidence than wording/description similarity alone
— prefer discard/merge over add in that case even if the topic looks new.

Rules:
- target_skill_key MUST be one of the keys in "existing_skills" when action
  is "merge"; null for "add"/"discard".
- Never invent a key that was not shown to you.
- Keep "reason" under 20 words and concrete (name the overlapping capability
  or what's new)."""


def judge_candidate(llm_helper, draft: Dict, neighbors: List[Tuple[str, Skill, float]],
                     quality: Optional[Dict[str, Dict]] = None) -> Dict:
    """Agent B — decides add/merge/discard for one synthesized draft against
    a SHORT list of its most similar existing skills (see
    skill_retrieval.retrieve_top_m / route_draft's Tier 0+1). Returns
    {"action", "target_skill_key", "reason"}.

    `quality` (see keyword_quality_map) is optional GROUNDED evidence — this
    session's actual per-keyword unique_hits/dropped counts — folded into the
    prompt as "candidate_keyword_quality" so the discard gate can lean on
    real log data instead of judging novelty from wording alone. This is the
    signal a generic text-only judge (AutoSkill's own) structurally can't
    have, since it never sees the underlying log.

    Fails closed to "add" (the pre-Phase-2 behavior) on any parse failure, an
    unavailable LLM, an empty neighbor list, or an invented/unknown
    target_skill_key — a wrong merge onto the wrong skill silently corrupts
    that skill's content, which is a worse failure mode than an extra "add"
    the engineer can manually clean up later. Every uncertain path defaults
    to the safe side."""
    if not neighbors:
        return {"action": "add", "target_skill_key": None,
                "reason": "No sufficiently similar existing skill."}
    if llm_helper is None or not getattr(llm_helper, "is_ready", True):
        return {"action": "add", "target_skill_key": None,
                "reason": "LLM unavailable; defaulted to add."}

    valid_keys = {key for key, _sk, _sc in neighbors}
    existing_for_llm = [
        {"key": key, "name": sk.name, "description": sk.description,
         "keywords": sk.keywords, "exclusive": sk.exclusive, "similarity": sc}
        for key, sk, sc in neighbors
    ]
    payload = {
        "candidate": {
            "name": draft.get("name"), "description": draft.get("description"),
            "keywords": draft.get("keywords"), "exclusive": draft.get("exclusive"),
        },
        "existing_skills": existing_for_llm,
    }
    if quality:
        candidate_terms = list(draft.get("keywords") or []) + list(draft.get("exclusive") or [])
        term_quality = {t: quality[t] for t in candidate_terms if t in quality}
        if term_quality:
            payload["candidate_keyword_quality"] = term_quality
    user_content = json.dumps(payload, ensure_ascii=False)

    try:
        raw = llm_helper.chat(
            messages=[{"role": "user", "content": user_content}],
            system_content=JUDGE_SYS_PROMPT,
            temperature=0.0,
            max_tokens=300,
        )
        obj = parse_json_loose(raw) or {}
        action = str(obj.get("action") or "add").strip().lower()
        if action not in ("add", "merge", "discard"):
            action = "add"
        target = str(obj.get("target_skill_key") or "").strip() or None
        if action == "merge" and target not in valid_keys:
            # Guardrail: never trust a merge onto a key that wasn't actually
            # shown to the model — treat as "add" instead of guessing.
            action, target = "add", None
        return {"action": action, "target_skill_key": target,
                "reason": str(obj.get("reason") or "").strip()}
    except Exception as e:
        return {"action": "add", "target_skill_key": None,
                "reason": f"Judge call failed ({e}); defaulted to add."}


def _dedupe_ci(base: List[str], additions: List[str]) -> Tuple[List[str], List[str]]:
    """Case-insensitive union of `additions` onto `base`, preserving base's
    existing order first. Returns (merged_list, actually_new_items) — the
    second is what the Edit-Skill modal highlights green (see basic_merge_
    draft's `diff` output)."""
    seen = {b.lower() for b in base if b}
    new_items: List[str] = []
    for item in additions or []:
        item = str(item or "").strip()
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        new_items.append(item)
    return list(base) + new_items, new_items


def keyword_quality_map(filter_stats: Optional[Dict]) -> Dict[str, Dict]:
    """Text -> {"hits", "unique_hits", "dropped"} from the session's last
    compute_filter_stats() result (state.filter_stats). This is the grounded,
    zero-LLM-cost evidence Phase 3 uses to tell a genuinely load-bearing new
    keyword apart from one that only LOOKS novel in the conversation but
    contributed nothing measurable this session — the differentiator a
    generic AutoSkill-style merge (LLM guesswork only) can't have, since it
    has no access to the actual log at all. Returns {} for missing/empty
    stats so every caller can treat "no evidence" and "evidence says keep"
    uniformly (see basic_merge_draft's default-to-keep policy below)."""
    if not filter_stats:
        return {}
    out: Dict[str, Dict] = {}
    for pf in filter_stats.get("per_filter", []) or []:
        text = str(pf.get("text") or "").strip()
        if text:
            out[text] = {
                "hits": pf.get("hits"),
                "unique_hits": pf.get("unique_hits"),
                "dropped": pf.get("dropped"),
            }
    return out


def basic_merge_draft(existing: Skill, draft: Dict, quality: Optional[Dict[str, Dict]] = None) -> Dict:
    """Agent D — DETERMINISTIC baseline merge (no LLM), safe by construction:
    it can only ADD to `existing`, never silently drop or overwrite its
    content. A smarter LLM-assisted semantic-union merge (matching AutoSkill's
    Pmerge — proper conflict resolution, stale-detail pruning) is a later
    phase; this is the guardrail that makes "merge" safe to ship without that
    yet existing.

      - keywords / exclusive: case-insensitive union, existing's own order
        kept first so the modal's chip order doesn't jump around. A NEW
        keyword/exclude-term is only added to the live merged list when
        `quality` (see keyword_quality_map) shows it's actually load-bearing
        this session — unique_hits > 0 for an include, dropped > 0 for an
        exclude. One measured as CONTRIBUTING NOTHING (unique_hits == 0 /
        dropped == 0) is held out into `low_value_keywords`/
        `low_value_exclusive` instead — informational only, never silently
        discarded, the engineer can still add it back by hand in the modal.
        A keyword `quality` has no data for (not enabled this round, or no
        filter run at all) defaults to KEEP — absence of evidence is not
        evidence of redundancy, so it's never penalized on that basis alone.
      - description: kept from `existing` (the skill's stable identity)
        unless the draft's is clearly more complete (>10% longer) — a thin
        one-off candidate description must never overwrite a carefully
        scoped existing one.
      - expert_rules: existing's rules kept verbatim; only genuinely NEW
        lines from the draft (not already present, case-insensitive) are
        appended below a blank-line separator. Nothing from `existing` is
        ever removed here.
      - name: kept from `existing` — a merge must never rename a skill out
        from under its established key/identity.

    Also attaches `diff` = {new_keywords, new_exclusive, rules_added_text} —
    the EXACT shape templates/log_viewer.html's Edit-Skill modal already
    knows how to render as a green "NEW" highlight banner (see
    skill_editor.js's renderDiffBanner, built for this, previously unused
    because nothing ever produced a diff). `new_keywords`/`new_exclusive`
    here are the CONFIRMED ones actually present in `keywords`/`exclusive`
    below — low-value ones are reported separately, never as a green chip."""
    quality = quality or {}

    def _split_by_quality(candidates: List[str], zero_field: str) -> Tuple[List[str], List[str]]:
        """(confirmed, low_value) — a candidate is low-value only when
        `quality` has a definite measurement AND that measurement is exactly
        zero; missing/unmeasured/None keeps it confirmed (see docstring)."""
        confirmed, low_value = [], []
        for kw in candidates:
            q = quality.get(kw)
            measured = q.get(zero_field) if q else None
            if measured is not None and int(measured) == 0:
                low_value.append(kw)
            else:
                confirmed.append(kw)
        return confirmed, low_value

    all_new_keywords_base, all_new_keywords = _dedupe_ci(existing.keywords, draft.get("keywords") or [])
    all_new_exclusive_base, all_new_exclusive = _dedupe_ci(existing.exclusive, draft.get("exclusive") or [])
    confirmed_kw, low_value_kw = _split_by_quality(all_new_keywords, "unique_hits")
    confirmed_ex, low_value_ex = _split_by_quality(all_new_exclusive, "dropped")

    merged_keywords = list(existing.keywords) + confirmed_kw
    merged_exclusive = list(existing.exclusive) + confirmed_ex

    draft_desc = str(draft.get("description") or "").strip()
    description = existing.description
    if draft_desc and len(draft_desc) > len(existing.description.strip()) * 1.1:
        description = draft_desc

    existing_rules = (existing.expert_rules or "").strip()
    draft_rules = str(draft.get("expert_rules") or "").strip()
    rules_added_text = ""
    if draft_rules:
        existing_norm = existing_rules.lower()
        new_lines = [ln for ln in draft_rules.split("\n")
                     if ln.strip() and ln.strip().lower() not in existing_norm]
        rules_added_text = "\n".join(new_lines)
    expert_rules = f"{existing_rules}\n\n{rules_added_text}".strip() if rules_added_text else existing_rules

    merged = dict(draft)
    merged["name"] = existing.name
    merged["description"] = description
    merged["keywords"] = merged_keywords
    merged["exclusive"] = merged_exclusive
    merged["expert_rules"] = expert_rules
    merged["diff"] = {
        "new_keywords": confirmed_kw,
        "new_exclusive": confirmed_ex,
        "rules_added_text": rules_added_text,
    }
    merged["low_value_keywords"] = low_value_kw
    merged["low_value_exclusive"] = low_value_ex
    return merged


# Tier 0 (continuity) thresholds — deliberately more permissive than Tier 1's
# retrieval floor, since an explicit "the engineer loaded this skill this
# session" action is stronger evidence than incidental lexical overlap.
_CONTINUITY_MIN_SCORE = 0.15    # below this, don't even ask the judge about it
_CONTINUITY_FORCE_SCORE = 0.55  # above this, skip the extra judge call entirely
# Tier 1 (general retrieval) floor — a neighbor below this is noise, not
# worth spending a judge call on.
_RETRIEVAL_MIN_SCORE = 0.12


def route_draft(llm_helper, draft: Dict, pool: Dict[str, Skill],
                 continuity_skill_key: Optional[str] = None,
                 filter_stats: Optional[Dict] = None) -> Dict:
    """Agent B+C+D entry point for ONE synthesized draft. Returns the
    (possibly merge-rewritten) draft dict with an attached `judge` field:
        {"action": "add"|"merge"|"discard", "target_skill_key": str|None,
         "target_skill_name": str|None, "target_skill_version": str|None,
         "reason": str, "source": "continuity"|"retrieval"}
    and, for "merge", `skill_key` set to the target so the Edit-Skill modal
    opens in edit-existing mode with the merged (see basic_merge_draft)
    content pre-filled instead of a blank new draft.

    Tier 0 — continuity: if `continuity_skill_key` (state.active_skill_key,
    the ONE skill the engineer explicitly loaded this session) is set, check
    ONLY that skill first, in isolation. A high-confidence match skips the
    LLM call entirely (cheap, since it's a single deterministic score); a
    moderate match still asks the judge to confirm same-capability before
    committing. Either way this is a strong-but-verified signal, never a
    blind override.

    Tier 1 — retrieval: runs whenever Tier 0 doesn't apply (nothing loaded,
    draft's domain mismatch) or doesn't clear its own bar (low score, or the
    judge disagreed) — Agent C searches the WHOLE domain pool and Agent B
    judges among the top matches, same as a from-scratch FRESH-mode export.

    `filter_stats` (state.filter_stats, the session's last compute_filter_
    stats() result) is optional grounded evidence — see keyword_quality_map —
    fed to BOTH the judge (as discard-gate evidence) and the merge (to hold
    back newly-added keywords/excludes that measured zero marginal value this
    session rather than blindly unioning everything). None/missing degrades
    gracefully to Phase 2's plain-union behavior."""
    quality = keyword_quality_map(filter_stats)
    ruled_out: set = set()

    if continuity_skill_key and continuity_skill_key in pool:
        continuity_skill = pool[continuity_skill_key]
        score = skill_retrieval.score_against(draft, continuity_skill)
        if score >= _CONTINUITY_MIN_SCORE:
            if score >= _CONTINUITY_FORCE_SCORE:
                decision = {
                    "action": "merge", "target_skill_key": continuity_skill_key,
                    "reason": f"Continues the pre-loaded skill \"{continuity_skill.name}\" "
                              f"(similarity {score:.2f}).",
                }
            else:
                decision = judge_candidate(
                    llm_helper, draft, [(continuity_skill_key, continuity_skill, score)], quality)
            if decision["action"] == "merge" and decision.get("target_skill_key") == continuity_skill_key:
                return _apply_judge_decision(draft, decision, pool, source="continuity", quality=quality)
            # Judge looked at the pre-loaded skill specifically and said "not
            # the same capability" — don't re-litigate it in Tier 1 with the
            # exact same evidence; let it compete fairly (or not at all) among
            # the general neighbors instead.
            ruled_out.add(continuity_skill_key)

    neighbors = skill_retrieval.retrieve_top_m(draft, pool, top_m=3, exclude_keys=ruled_out)
    neighbors = [n for n in neighbors if n[2] >= _RETRIEVAL_MIN_SCORE]
    decision = judge_candidate(llm_helper, draft, neighbors, quality)
    return _apply_judge_decision(draft, decision, pool, source="retrieval", quality=quality)


def _apply_judge_decision(draft: Dict, decision: Dict, pool: Dict[str, Skill], source: str,
                           quality: Optional[Dict[str, Dict]] = None) -> Dict:
    """Turns a judge decision into the final draft shape the route sends to
    the frontend — folds the merge (Agent D) when applicable, and always
    attaches a `judge` field so the Edit-Skill modal can explain the
    suggestion regardless of which action was chosen."""
    action = decision.get("action") or "add"
    target_key = decision.get("target_skill_key")
    target = pool.get(target_key) if target_key else None

    if action == "merge" and target is not None:
        merged = basic_merge_draft(target, draft, quality)
        merged["skill_key"] = target_key
        merged["judge"] = {
            "action": "merge", "target_skill_key": target_key,
            "target_skill_name": target.name, "target_skill_version": target.version,
            "reason": decision.get("reason") or "", "source": source,
        }
        return merged

    # add / discard / merge-with-missing-target (guardrail) all keep the
    # draft AS-IS — discard is advisory only: the engineer still gets the
    # modal and can save it as a new skill if they disagree with the judge.
    out = dict(draft)
    out["skill_key"] = None
    out["judge"] = {
        "action": action if action in ("add", "discard") else "add",
        "target_skill_key": None, "target_skill_name": None, "target_skill_version": None,
        "reason": decision.get("reason") or "", "source": source,
    }
    return out

