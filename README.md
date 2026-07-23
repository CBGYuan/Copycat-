# Copycat — Log Triage & Self-Evolving Skill Learning

A standalone Flask app for triaging Wi-Fi/Bluetooth driver logs and turning
an engineer's filtering + debugging judgment into reusable, versioned
**skills** an LLM can apply on the next log. It's a trimmed-down,
independently-built sibling of
[wireless_ce_avatar/IntelAvatar](https://github.com/kj-fang/wireless_ce_avatar),
keeping only the log-viewing/filtering + chat + skill-learning line, with the
skill lifecycle rebuilt around ideas from
[**AutoSkill**](https://github.com/ECNU-ICALK/AutoSkill) (*"Experience-Driven
Lifelong Learning via Skill Self-Evolution"*): retrieval-assisted skill
management, an add/merge/discard judge, and versioned merging — grounded in
this project's own filter statistics rather than text similarity alone.

## What it does

### 1. Log Viewer
- Manually pick a driver log (`.log`, `.hci.txt`, DDD export, …) and an
  optional `.tat` filter file (**TextAnalysisTool.NET**'s native XML format —
  `enabled` / `excluding` / `case_sensitive` / `regex` / colors, fully
  respected).
- WiFi vs Bluetooth is auto-detected from the picked log (filename hints,
  then a content sniff), which switches the skill dropdown to the right pool
  and normalizes dateless BT `.hci.txt` / WiFi DDD timestamp formats into one
  canonical shape.
- Include/exclude keywords, checkbox-toggle, or load an existing skill's
  keyword set as the starting filter — every edit re-runs the filter and is
  recorded with its *measured marginal effect*: hit count, unique
  contribution, noise dropped, and its top co-firing keyword — powering
  everything downstream instead of re-scanning the log.
- A collapsible **System Event Log** panel (BT captures) auto-discovers the
  matching Windows System event export next to the driver log; rows
  click-sync to the nearest driver-log line by timestamp, and vice versa.
- A deterministic (zero-LLM-cost) **red-flag detector** notices a redundant
  keyword, a no-op exclude, a suspiciously large expansion/drop, or removing
  something load-bearing — and surfaces it as an inline question the instant
  it happens.

### 2. Skill-Building Chat
- Claude-style layout: your messages sit in a right-aligned bubble; the
  assistant's replies flow as plain full-width Markdown text.
- **Log Round & Analyze** — one LLM call that analyzes the current filter
  state, asks 1–3 grounded follow-up questions (structured choice-or-custom,
  same shape as Claude's own `AskUserQuestion`), and self-scores readiness
  (0–100) plus per-goal coverage (knowledge / scope / minimal-keywords) and a
  claim-by-claim verified-vs-asserted validation — so a threshold the
  engineer stated but the log never proved can't silently be exported as
  fact.
- **Per-step teaching** — click 🎓 on any filter edit in the Steps panel to
  explain, in your own words, why you made it; the LLM condenses it into a
  confirmable knowledge-core statement, adds its own expert second opinion,
  and may ask one interactive follow-up (answer or skip) without losing the
  thread of what step it's about.
- Every message is tagged with the step it concerns (`#N`) or `All` for
  general knowledge, via a compact selector next to Send — replay after a
  page reload preserves the same tags.
- Two independent modes, chosen per export: **FRESH** (teach from scratch,
  nothing else consulted) or **PRIOR** (same-domain existing skills shown so
  the interview only asks about what's genuinely new).

### 3. Export → Self-Evolving Skill Lifecycle (AutoSkill-integrated)
Exporting doesn't just dump a new file — every synthesized draft goes through
a retrieval → judge → merge pipeline, **advisory only**: nothing is written
until you confirm in the Edit-Skill modal.

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
  log_viewer/                Log/filter picking, apply_filter, event log, red-flag Q&A
  chatbot/                   Free-form chat send/reset (step-tagged)
  learning/                   Log Round, per-step teach/ask, readiness, converge/save
  skills/                     Skill library CRUD
services/
  llm_service.py               LLM_helper — Anthropic/OpenAI-compatible client,
                                 token usage tracking, prompt caching
  session_store.py             Per-browser-session working state
  event_log_service.py         Windows System Event Log (.evt/.evtx) reader
  skill_service.py             Skill model + YAML read/write, versioning
  skill_retrieval.py           Agent C — keyword-set similarity retrieval
  learning_service.py          Interview prompts, readiness assessment,
                                 skill synthesis, judge (Agent B),
                                 stat-validated merge (Agent D)
utils/
  tat_parser.py                 .tat XML parsing + filter/stats engine
  operation_journal.py          Per-edit journal with measured effect + red flags
  file_picker.py, browser_utils.py, helpers.py, json_utils.py
templates/, static/           log_viewer.html (main workbench), skills.html,
                                shared skill_editor.js modal, style.css
data/skills/
  local/                        THIS engineer's own copycat-originated
                                  skills — the only files the app writes to
  shared_cache/                 Read-only mirror of the shared corp drive,
                                  auto-refreshed every app startup — never
                                  hand-edited, gitignored (regenerated data)
```

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
