# Copycat — Log Triage & Self-Evolving Skill Learning

A standalone Flask app for triaging Wi-Fi/Bluetooth driver logs and turning
an engineer's filtering + debugging judgment into reusable, versioned
**skills** an LLM can apply on the next log. It's a trimmed-down,
independently-built sibling of
[wireless_ce_avatar/IntelAvatar](https://github.com/kj-fang/wireless_ce_avatar),
keeping only the log-viewing/filtering + chat + skill-learning line.

## Responsibility boundary: Copycat is a teaching workbench, not a validator

**Copycat measures how COMPLETE the teaching evidence is, never whether the
skill is CORRECT.** That distinction is load-bearing throughout the codebase:

- **Copycat** collects the teaching trajectory — filter edits with measured
  effect, engineer-written reasons, evidence/counterexample-labeled log
  lines, LLM-clarified scope — and synthesizes it into a skill draft with an
  evidence summary attached (`learning_service.assess_teaching_evidence`).
  It never computes TP/FP/FN or a pass/fail verdict from that same session's
  data — see that function's docstring for why: a skill that looks good
  against the log it was taught on is not evidence it generalizes.
- **[wireless_ce_avatar](https://github.com/kj-fang/wireless_ce_avatar)** is
  the system of record for "is this a good skill" — it runs a candidate
  against real historical issues/logs and reports the actual TP/FP/FN,
  success rate, and stability. Copycat only *prepares* what that system needs
  (`learning_service.build_validation_packet`) and *stores* whatever comes
  back (`apply_external_validation`) — the live integration itself isn't
  wired up yet (see [Status](#status--whats-not-done-yet)).

The skill lifecycle (retrieval, add/merge/discard judge, versioned merging)
is built around ideas from [**AutoSkill**](https://github.com/ECNU-ICALK/AutoSkill)
(*"Experience-Driven Lifelong Learning via Skill Self-Evolution"*), grounded
in this project's own filter statistics rather than text similarity alone.
The interview/clarification layer draws on a few more papers, cited
precisely (not overclaimed) in [Paper grounding](#paper-grounding) below.

## What it does

### 1. Log Viewer
- Manually pick a driver log (`.log`, `.hci.txt`, DDD export, …) and an
  optional `.tat` filter file (**TextAnalysisTool.NET**'s native XML format —
  `enabled` / `excluding` / `case_sensitive` / `regex` / colors, fully
  respected).
- WiFi vs Bluetooth is auto-detected from the picked log (filename hints,
  then a content sniff), which switches the skill dropdown to the right pool
  and normalizes dateless BT `.hci.txt` / WiFi DDD timestamp formats
  (including a tab-separated `frame#\tHH:MM:SS:mmm\t...` variant) into one
  canonical shape. Switching to a log in a *different* domain than the one
  currently filtered clears the stale filter instead of silently re-running
  it and reporting a confusing "0 lines matched."
- Include/exclude keywords, checkbox-toggle, or load an existing skill's
  keyword set as the starting filter — every edit re-runs the filter and is
  recorded with its *measured marginal effect*: hit count, unique
  contribution, noise dropped, and its top co-firing keyword — powering
  everything downstream instead of re-scanning the log. A **Show all** button
  jumps back to the unfiltered view without re-picking the file. Skills carry
  no color info of their own (only a `.tat` file does), so a skill-loaded
  filter set is auto-assigned a color palette, alternating full-block and
  text-only treatments so a long filter list doesn't read as a wall of
  saturated color.
- A collapsible **System Event Log** panel (BT captures) auto-discovers the
  matching Windows System event export next to the driver log; rows
  click-sync to the nearest driver-log line by timestamp, and vice versa. The
  driver log is customer-local time while the event log is UTC — Copycat
  reads the capture machine's fixed UTC offset from a `systeminfo.txt`/
  `system_info.txt` sitting near the log (same field IntelAvatar's own
  timezone resolver reads) and corrects for it, rather than comparing the two
  raw and landing on a plausible-looking but wrong line.
- A deterministic (zero-LLM-cost) **red-flag detector** notices a redundant
  keyword, a no-op exclude, a suspiciously large expansion/drop, or removing
  something load-bearing — and surfaces it as an inline question the instant
  it happens.
- **Evidence / counterexample line annotation** — mark any visible log line
  `E` (supports a filter/rule) or `X` (counterexample — an edge case or line
  that would be a false positive). Attribution is by **filter identity**
  (`tat_parser.matched_keywords_for_line`), never by guessing which historical
  edit "owns" a line — a loaded skill/`.tat` file never recorded a per-keyword
  edit history to guess from in the first place, so identity-based
  attribution is the only version of this that's actually reliable (two
  earlier approaches — guessing from the edit history, then a manual
  step-picker UI — were both tried and dropped). The filter table's `Ev`
  column (`2E/1X`) is the one authoritative view; the Steps panel's own E/X
  counts are explicitly an indirect, secondary cross-reference, never claimed
  as "this step caused this evidence."

### 2. Skill-Building Chat
- Claude-style layout: your messages sit in a right-aligned bubble; the
  assistant's replies flow as plain full-width Markdown text.
- **Log Round & Analyze**, with an **Ambiguity Gate** — one LLM call analyzes
  the current filter state and self-scores readiness (0–100), per-goal
  coverage (knowledge / scope / minimal-keywords / evidence), and a
  claim-by-claim verified-vs-asserted validation. It only asks a follow-up
  question when it can show the ambiguity is real: it first drafts 2–3
  concrete interpretations of the most important unexplained edit, each with
  its own predicted observable behavior; if every interpretation lands on the
  same behavior, it asks nothing. Only on genuine divergence does it ask the
  ONE question that would discriminate between those behaviors — never a
  generic "is this temporary or permanent?" template. (See
  [Paper grounding](#paper-grounding) — ClarifyGPT's consistency check +
  GATE's discriminating-question framing.) A nudge card appears in-chat when
  there are unlogged filter changes worth a round — gated on whether an edit
  actually moved something measurable (a load-bearing keyword, dropped noise,
  a meaningful survivor swing), not on raw edit count, so exploring a long
  filter list checkbox-by-checkbox doesn't spam it.
- **Per-step teaching** — click 🎓 on any filter edit in the Steps panel to
  explain, in your own words, why you made it; the LLM condenses it into a
  confirmable knowledge-core statement, adds its own expert second opinion,
  and may ask one interactive follow-up (answer or skip). The response is
  kept as a permanent, structured chat entry (not lost once dismissed).
- Every message is tagged with the step it concerns (`#N`) or `All` for
  general knowledge, via a compact selector next to Send — replay after a
  page reload preserves the same tags.
- Two independent modes, chosen per export: **FRESH** (teach from scratch,
  nothing else consulted) or **PRIOR** (same-domain existing skills shown so
  the interview only asks about what's genuinely new).

### 3. Export → Self-Evolving Skill Lifecycle (AutoSkill-integrated)
Exporting doesn't just dump a new file — every synthesized draft goes through
a **teaching-evidence assessment**, then a retrieval → judge → merge
pipeline, **advisory only**: nothing is written until you confirm in the
Edit-Skill modal.

- **Teaching evidence assessment** (`assess_teaching_evidence`) — reports
  which keywords were actually exercised on the log, whether every material
  edit has an engineer explanation, and how many counterexamples were
  flagged. This is provenance for you to review, explicitly **not** a
  correctness score — see the [Responsibility boundary](#responsibility-boundary-copycat-is-a-teaching-workbench-not-a-validator)
  above. The same data is packaged (`build_validation_packet`) for eventual
  hand-off to `wireless_ce_avatar`, and there's a slot to store whatever
  comes back (`apply_external_validation`) — the live call isn't wired up yet.

- **Versioning** — every skill carries a `version` (bumped on each edit) and
  a rolling `version_history` snapshot of its pre-edit state, so a skill's
  evolution is inspectable and auditable, not silently overwritten.
- **Retrieval-assisted maintenance decision** (add / merge / discard) — a
  lightweight keyword-set retriever narrows the field to the few most
  similar existing skills, then an LLM judge decides:
  - **Tier 0 — continuity**: if you explicitly loaded a skill this session,
    it's checked first, in isolation — strong intent signal, but still
    gated by a real similarity/judge check, never blindly trusted (a session
    that drifts onto an unrelated topic won't get force-merged into whatever
    happened to be loaded at the start).
  - **Tier 1 — retrieval**: otherwise, the whole domain pool is searched for
    the closest matches.
  - Every uncertain path (parse failure, an invented target key, an
    unavailable LLM) fails closed to **add** — a wrong merge silently
    corrupts an existing skill, which is a strictly worse outcome than one
    extra draft to clean up later.
- **Data-grounded merge** — merging is a safe, deterministic union (never
  overwrites or drops existing content), and it's *validated against this
  session's actual filter statistics*: a newly-proposed keyword that matched
  zero unique lines this run (`unique_hits == 0`) — or an exclude term that
  dropped nothing (`dropped == 0`) — is held back from the auto-merge instead
  of blindly unioned in, with the reason shown so you can still add it back
  by hand. This is the one signal a text-only judge structurally can't have,
  since it never sees the underlying log.
- The Edit-Skill modal shows exactly what's being proposed — a brand-new
  skill, or a merge into a named existing one (with new keywords/rules
  highlighted green) — before anything touches disk.

### 4. Skill Library
A standalone page listing every learned skill (WiFi ∪ BT) with the same
Edit-Skill modal — inspect, hand-edit keywords/exclusive/expert_rules, or
delete.

## Architecture

```
app.py                     Flask entry point — registers all blueprints
configs/
  global_configs.py        App-wide state (LLM helper, loaded skill pools)
  path_configs.py           Key + shared-skill-drive paths
  set_up_app.py             Startup: configure LLM, load skill sources
blueprints/
  main/                     Landing page
  log_viewer/                Log/filter picking, apply_filter, event log,
                               red-flag Q&A, line annotation (/annotate_line),
                               show_all (unfiltered view)
  chatbot/                   Free-form chat send/reset (step-tagged)
  learning/                   Log Round (Ambiguity Gate), per-step teach/ask,
                               readiness, converge/save, teaching-evidence
                               assessment
  skills/                     Skill library CRUD
services/
  llm_service.py               LLM_helper — Anthropic/OpenAI-compatible client,
                                 token usage tracking, prompt caching
  session_store.py             Per-browser-session working state
                                 (incl. log_annotations)
  event_log_service.py         Windows System Event Log (.evt/.evtx) reader +
                                 capture-machine UTC-offset detection
                                 (systeminfo.txt) for click-sync
  skill_service.py             Skill model + YAML read/write, versioning
  skill_retrieval.py           Agent C — keyword-set similarity retrieval
  learning_service.py          Interview prompts (Ambiguity Gate), readiness
                                 assessment, skill synthesis, judge (Agent B),
                                 stat-validated merge (Agent D), teaching-
                                 evidence assessment + external-validation
                                 packet/hook (assess_teaching_evidence /
                                 build_validation_packet /
                                 apply_external_validation)
utils/
  tat_parser.py                 .tat XML parsing + filter/stats engine +
                                 matched_keywords_for_line (E/X attribution
                                 by filter identity)
  operation_journal.py          Per-edit journal with measured effect + red flags
  file_picker.py, browser_utils.py, helpers.py, json_utils.py
templates/, static/           log_viewer.html (main workbench), skills.html,
                                shared skill_editor.js modal, style.css
tests/                          Unit tests for the learning pipeline
                                  (see Tests below)
data/skills/
  local/                        THIS engineer's own copycat-originated
                                  skills — the only files the app writes to
  shared_cache/                 Read-only mirror of the shared corp drive,
                                  auto-refreshed every app startup — never
                                  hand-edited, gitignored (regenerated data)
```

## Paper grounding

Cited precisely — each entry states exactly what's implemented vs. referenced
as a future direction, so this list doesn't overclaim:

- **[ClarifyGPT](https://arxiv.org/abs/2310.10996)** + **[GATE](https://arxiv.org/abs/2310.11589)**
  (*"Eliciting Human Preferences with Language Models"*) — the Log Round
  Ambiguity Gate: only ask when alternate interpretations of an edit would
  produce genuinely different observable behavior, and the question itself
  must discriminate between those behaviors rather than being a generic
  template. Implemented in `learning_service.analyze_round`.
- **[ASI — Agent Skill Induction](https://arxiv.org/abs/2504.06821)** — the
  induction/verification split: Copycat induces the candidate skill and
  reports what evidence backs it (`assess_teaching_evidence`), but real
  verification runs in the actual target environment — here, that's
  `wireless_ce_avatar`, not a same-session self-check.
- **[AutoSkill](https://github.com/ECNU-ICALK/AutoSkill)** — retrieval-
  assisted skill management, an add/merge/discard judge, and versioned
  merging (see Export section above). Fully implemented for the single-draft
  case; a periodic *Consolidator* pass across the whole local skill bank
  (AutoManual-style batch merge/redundancy cleanup) is deliberately **not**
  built yet — see Status below.
- **[AutoManual](https://arxiv.org/abs/2405.16247)** — conceptual reference
  for preserving trajectory + reason + effect + provenance together (the
  Steps panel + operation journal), and for the future Consolidator idea
  above. Not a full reimplementation of AutoManual's Planner/Builder/
  Consolidator agent framework.
- **τ-bench** — reference point for *future* repeated-trial/reliability
  checking. That kind of evaluation belongs in `wireless_ce_avatar` (running
  a skill repeatedly against real cases), not as a Copycat script measuring
  itself against the same data it was taught on.

## Status — what's not done yet

- **`wireless_ce_avatar` integration is not live.** `build_validation_packet`/
  `apply_external_validation` exist and are unit-tested, but there's no actual
  HTTP/API call wired up yet — that needs the real endpoint, auth, and
  request/response schema from that system first.
- **No Consolidator / `consolidate_skill_bank` yet.** Deliberately deferred
  until enough externally-validated cases have accumulated to make batch
  merging meaningful — not just "not implemented yet," but not *ready* to
  design in detail until that data exists.
- **No repeated-execution/reliability reporting in Copycat itself** (see the
  τ-bench note above) — that stays in `wireless_ce_avatar`'s scope.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Covers the Ambiguity Gate (only asks on genuine behavioral divergence),
`assess_teaching_evidence` (reports counterexamples without scoring
correctness), deterministic (non-LLM) evidence-coverage computation,
filter-identity-based E/X attribution, and that a session reset clears
case-specific teaching state.

## Setup

```powershell
pip install -r requirements.txt
```

## Configure the LLM (GNAI / Anthropic-compatible endpoint)

Same resolution order as IntelAvatar: a corporate shared `keys.py` is tried
first; if unreachable, drop your own key file in as
`configs/keys_local.py` (same schema, gitignored):

```python
# configs/keys_local.py
gnaigpt_token = "..."
gnaigpt_url   = "https://gnai.intel.com/api/providers/anthropic"
gnaigpt_model = "claude-4-6-sonnet"
```

`configs/path_configs.py`'s `KEY_PATH_prim` / `KEY_PATH_bkup` point at the
same shared drive locations as IntelAvatar; whichever resolves first (local
override → primary share → backup share) wins.

## Run

```powershell
python app.py
```

Opens automatically in Chrome on a freshly-picked free port (never a fixed
`:5000`, so a leftover process from an earlier run can't collide with it).

## Skill data model

Each entry in `data/skills/local/{skills,bt_skills}.yaml`:

```yaml
<skill_key>:
  name: "..."
  description: "..."          # mutually exclusive from other skills' scope
  keywords:                     # minimal include-keyword set
    - "..."
  exclusive:                    # noise terms to drop
    - "..."
  expert_rules: |               # numbered domain knowledge / hard rules
    1. ...
  version: "0.1.3"
  version_history: |            # rolling JSON audit trail (pre-edit snapshots)
    [...]
```

### Two clearly separate skill folders

- **`data/skills/local/`** — this engineer's own copycat-originated skills.
  The *only* files the app ever writes to. A skill key that only exists via
  the shared drive is never edited or shadowed in place here — see/merge
  onto an existing key only works when that key is already local; otherwise
  a fresh, non-colliding local key is minted instead (skill_service.
  save_skill). Genuinely-owned content — commit it like any other work
  product.
- **`data/skills/shared_cache/`** — a read-only local mirror of the shared
  corp drive (`skills_config/{skills,bt_skills}.yaml` +
  per-engineer `user_contributions/`), refreshed automatically on every app
  startup (`skill_service.refresh_shared_cache`, best-effort — if the share
  is unreachable that startup, the app keeps serving whatever was cached
  from the last successful sync instead of the pool going empty). Never
  hand-edited, never written to by the UI, gitignored (regenerated data).

Phase 2's retrieval-assisted skill maintenance (add/merge/discard judge)
only ever considers `local/` skills as merge targets — a shared-drive skill
that looks similar to new teaching never gets silently modified; the new
teaching becomes its own local skill instead.
