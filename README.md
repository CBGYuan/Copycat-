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
- **Toggling a filter is meant to feel instant**, since it is the single most
  repeated action in the workbench. Three things were costing it:
  - The scan tested every rule up to *three* times per line (once for the raw
    hit count, again for the include split, again for the exclude split), each
    through a per-rule closure. Each line now resolves its matched set **once**
    and the splits are set lookups — **~1.9× faster** on a 113k-line capture
    (0.58s → 0.30s, byte-identical output). Tempting non-fix, measured and
    rejected: gating each line behind one union regex of all patterns was
    **2.9× slower**, because `needle in line` is a fast C substring search
    while an alternation of ~17 literals puts Python's backtracking engine to
    work at every position.
  - Every click paid a flat 350 ms trailing debounce before the request even
    left. The coalescing is now **leading-edge**: an isolated click (the
    common case) fires immediately, and a burst still collapses into exactly
    one follow-up run.
  - Re-running replaced the log with "Running filter…", flashing the whole
    pane away and back and discarding the scroll position. The previous rows
    now stay put, dimmed, until the new ones arrive.
- **Select a token in a log line, make it a filter.** Highlight any substring
  in the log pane and a small picker offers `+ Keyword` (keep lines containing
  it) or `− Noise` (drop them). Two actions rather than one because those mean
  opposite things — picking for you would be a guess about intent. The point
  isn't saved typing: the selected text is verbatim, and retyped keywords are
  where `TASK_DISCONNECT` quietly becomes `TASK_DISCONECT` and matches nothing.
- **Focus a time window** — a labeled "Issue Time" button (with a custom
  tooltip that shows immediately on hover, not the slow native one) opens a
  popover
  with a custom 24-hour time picker and a ±minutes field; it seeds itself from
  the first visible log line, with a shortcut to re-seed from whatever line is
  currently on screen. The picker is three independently-scrollable HH/MM/SS
  columns, not `<input type="time">` — the native control renders as an
  unstyleable OS grid and, worse, silently displays in the browser locale's
  12-hour AM/PM format, which invites misreading a 24-hour driver-log
  timestamp by exactly 12 hours. The field only exists while the popover is
  open, rather than occupying the widest part of the toolbar permanently for a
  control most sessions never touch. While a window is applied the button
  stays marked and a badge shows it — clicking the badge clears it. It narrows
  every filter to ±N minutes around the chosen time, so a 200k-line capture
  collapses to the moments that matter. The window is sliced by binary search
  over a lazily-built timestamp index (most sessions never focus, so nobody
  pays for the index up front),
  and line numbers stay **real file line numbers**, not window-relative ones,
  so evidence annotations survive focusing and clearing. The header reports
  "scanned N of M lines" whenever a window is active, because a hit count
  that silently means something different is worse than no hit count.
- A collapsible **System Event Log** panel auto-discovers the matching Windows
  System event export next to the driver log; rows click-sync to the nearest
  driver-log line by timestamp, and vice versa. Shown for **WiFi and BT alike**
  — what decides whether it is useful is whether a capture actually shipped one,
  which has nothing to do with the domain.

  **Timezone alignment.** Windows stores `TimeCreated/@SystemTime` in **UTC**
  (Event Viewer only *displays* local time). The text-log frame is domain
  specific: WiFi decoder/WPP output uses the analysing engineer's local
  timezone, while BT `.hci.txt` uses the customer's timezone from
  `systeminfo.txt`/`system_info.txt`. Copycat shifts Event XML into that chosen
  frame and labels it explicitly in the panel:

  ```
  WiFi event 21:45 UTC + engineer UTC+08:00 = 05:45 next day -> matches WiFi text at 05:45
  BT   event 21:45 UTC + customer UTC-05:00 = 16:45          -> matches HCI text at 16:45
  ```

  WiFi uses the host OS's historical local offset for the event date (including
  its DST rule). BT accepts both `(UTC-05:00)` and current IntelAvatar
  `(GMT-0500)` System Info forms. If BT System Info has no usable timezone, the
  badge says so and click-sync is disabled rather than pretending raw UTC is
  customer-local.
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
- **Over-matching signal** — because attribution is per keyword, an *include*
  keyword whose only labels are `X` means every line it was labeled on was
  called wrong. That keyword's `Ev` cell turns red, and
  `assess_teaching_evidence` reports it as `over_matching_keywords` with a
  `warn` check. It is reported, never auto-applied: whether the term it drags
  in belongs in `exclusive` is the engineer's call, not the workbench's.

### 2. Skill-Building Chat
- Claude-style layout: your messages sit in a right-aligned bubble; the
  assistant's replies flow as plain full-width Markdown text.
- **Set the comparison baseline** — one button, and it is the deliberate start
  of the teaching loop. It stays disabled until a log is loaded **and the
  filter actually matches something**, glows once it can be pressed, and
  everything that feeds knowledge back (teaching a step, Export) waits behind
  it. Pressing it makes one LLM call that commits a **structured baseline
  read**: what scenario this looks like, which keywords it expects to be
  load-bearing, which it expects to be noise, and what it doesn't know yet.

  Gated rather than automatic for two reasons. A baseline formed on a filter
  that survives nothing *describes* nothing — and it then becomes the thing
  every later edit is compared against, so a junk first read poisons the whole
  session's divergence detection. And auto-firing spent a call on every `.tat`
  load, including the ones immediately replaced by a different file.

  That prediction is recorded *before* you teach anything, which is what makes
  the rest of the loop possible.
- **You then teach by filtering.** Every edit is measured, and a
  deterministic (zero-LLM-cost) pass compares what you did against what the
  baseline committed to:
  - **Contradiction** — the baseline called a keyword load-bearing and you
    cut it, or called it noise and you promoted it. Materiality is checked
    *before* opinion: an edit only counts if it actually moved something
    measurable (unique hits, survivor delta), so exploring a filter list
    checkbox-by-checkbox never triggers anything.
  - **Omission** — you added something the baseline never mentioned. This is
    a provenance gap, not an ambiguity, so it never gets a *discriminating*
    question (there is no competing reading to discriminate between).
    Instead it gets an open, **non-blocking** provenance-elicitation question
    — "why does this matter, does it generalize?" — since this is the
    highest-value target for distilling what you know into the skill; the
    Steps panel's 🎓 hint still lights up too, for whenever you'd rather
    explain it there instead.
- **A contradiction or an omission becomes a question**, and only once per
  edit. A contradiction's question must discriminate between two concrete
  readings that predict different observable behavior — never a generic "is
  this temporary or
  permanent?" template. (See [Paper grounding](#paper-grounding) —
  ClarifyGPT's consistency check, GATE's discriminating-question framing, and
  AutoManual's provenance-vs-ambiguity distinction.)
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
- **Update baseline** — the same button re-reads the filter after the baseline
  is set (`force=true`), instead of only ever being usable once. Each re-read
  keeps the prior version in a rolling history (last 10) and reports a
  **delta from the previous read** — what newly counts as load-bearing or
  noise — so re-baselining after a big filter change is legible rather than a
  silent swap of one opinion for another.
- **Case intake, asked in the conversation** — the case description ("what is
  this capture about?") and the choice of reference documents are pure
  up-front framing for the first read, so they are *asked for*, once, as the
  first card in the chat when a log is loaded — not typed into a panel above
  the conversation they belong to. Answering posts a compact **Question
  context** note into the transcript (description, reference docs, context
  size) with a **Rewrite** button that re-asks the same question; the note is
  re-posted right before the baseline analysis, so the transcript records what
  the read was *given* next to what it produced. An earlier design put both in
  a collapsible "Refinement" row under the chat header, which meant the
  framing lived in a panel the engineer had to know to open, and never
  appeared in the transcript at all. The value still lands in the same
  `state.case_summary` and is still fed to every later chat turn and question
  prompt — only the way it is collected changed.
- **Interview mode** — `ask` is the behavior described above: at most one
  question, only on measurable divergence, taking the highest-impact
  unresolved branch. A `quiet` mode (never interrupts automatically; also
  genuinely cheaper, since it drops the structured-question schema from every
  chat turn's system prompt *and* suppresses the auto-clarify call) still
  exists in `decision_ledger.VALID_MODES` and is honored end-to-end, but
  **has no selector in the UI at present** — the workbench runs `ask`.
  Whether an unresolved decision is flagged before Export is deliberately
  **not** part of this setting: that check is unconditional (see the decision
  ledger below), because whether a question was asked and whether an asked
  question went unanswered are two different concerns. An earlier design tied
  them together as Smart/Grill modes, which meant the default mode exported
  with open decisions and no warning at all.
- **Decision ledger** — every question the interview asks (baseline
  contradiction, per-step follow-up, clarification) is logged as one entry —
  open / resolved / deferred — in a session-only ledger, reachable from a
  small branch icon in the chat header. The icon is deliberately quiet: it
  carries no standing "0/0", and lights up with a count only when a decision
  is actually still **open**. It is
  *never* written into the exported skill YAML; at Export time it is folded
  into a **review-only spec** (scope, triggers, required evidence, exclusions,
  resolved vs. still-open decisions) shown alongside the draft, so what you
  decided and what you skipped is visible before you save, without changing
  Avatar's established file shape. Each entry carries its own `blocking` flag
  — a real specification decision blocks (i.e. warns) by default, an optional
  teach-step follow-up explicitly does not — so Export warns on genuinely
  unresolved decisions in **every** mode, without an optional follow-up
  nagging you about nothing.
- Sending a chat message before a baseline exists no longer requires one —
  it shows a **"Send without a baseline?"** confirmation instead of a hard
  block, since chatting freely is low-stakes compared to teaching a step or
  exporting (which stay locked, see above). Confirm once and it proceeds.

### 3. Export → Self-Evolving Skill Lifecycle (AutoSkill-integrated)
Exporting doesn't just dump a new file — every synthesized draft goes through
a **teaching-evidence assessment**, then a retrieval → judge → merge
pipeline, **advisory only**: nothing is written until you confirm in the
Edit-Skill modal.

- **Teaching evidence assessment** (`assess_teaching_evidence`) — reports
  which keywords were actually exercised on the log, whether every material
  edit has an engineer explanation, how many counterexamples were flagged,
  and the per-keyword E/X tally (`keyword_labels`) including any include
  keyword backed by counterexamples alone. This is provenance for you to
  review, explicitly **not** a
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

#### One Export button, two behaviours
Export always writes to `data/skills/local/` only, and what it produces
depends on a single fact: **whether a skill was loaded this session.**

- **No skill loaded** → the drafts are written exactly as synthesized. No
  de-duplication, no cloud content, no lineage. The local file contains only
  what you taught.
- **Skill loaded** → that skill becomes the **parent**. Its content is
  carried over in full and this session's additions are kept visibly
  separate:
  - **De-duplication** (`utils/skill_dedup.py`, ported from ACE's Curator)
    drops what merely repeats the parent. Two deliberate divergences from
    ACE: near-duplicates are *flagged for review, never auto-dropped* (a
    false positive there would silently delete a keyword you taught), and
    **substring containment beats similarity ratio** — TAT keywords are
    substring matchers, so a parent's `"DeAuth"` provably covers a child's
    `"DeAuth detected"` at any ratio, while the reverse direction *widens*
    coverage and must be kept. Those two are reported separately, never
    merged into one "similar" bucket.
  - **Flat, never delta-only.** `wireless_ce_avatar`'s loader reads exactly
    name/description/keywords/exclusive/expert_rules and silently ignores
    every other key — so a child storing only its delta and pointing at a
    `parent` key would load there with a fraction of its keywords and
    analyse logs quietly wrongly. Inheritance is expressed as *structure*
    (parent content first, then a `# ── Extension:` marker, then the new
    material) plus a recorded `parent`/`lineage` chain, not as a reference to
    resolve at load time.
  - The modal colours every item: **blue `INH`** = inherited from the parent,
    **green `NEW`** = taught this session. Same colour language as the Skill
    Library's origin dots (blue = from the shared side, green = originated
    locally).

Two guards on that path, both derived from how Avatar actually consumes a
skill rather than from taste:

- **Inheritance depth is capped at 2 generations.** Because exports are flat,
  each generation's keyword list is a superset of its parent's; by the third,
  the filter matches most of the log, Avatar's per-skill evidence payload
  overruns its line budget and gets head+tail truncated with the middle
  dropped — a skill that returns "almost everything, minus the middle" has
  stopped focusing anything. Over-deep exports are **not refused**: the draft
  is filed as a fresh standalone skill and the UI explains why. Your
  knowledge is never what gets discarded.
- **The child's description must be distinguishable from its parent's.**
  Avatar's agent picks a skill from the `name: description` lines alone and
  passes a key from an enum — it never sees keywords at selection time. Since
  a child inherits every parent keyword, that one sentence is the *entire*
  basis for telling them apart. Guarded twice: the synthesis prompt is handed
  the baseline skill and told what it is inheriting (only when inheritance
  will genuinely happen), and a deterministic similarity check catches it
  when the model ignores that — surfaced as an amber note under the
  description field that clears live as you rewrite it. Advisory; Save is
  never blocked on it.

#### Trigger conditions (`triggers`)
Every skill can declare the conditions under which it applies — platform,
phase, environment, whatever actually bounds it:

```yaml
triggers:
  - "platform is LNL"
  - "resume from S4"
```

This is the structural answer to the problem above. "Write a distinguishing
description" is a style request that can only be checked after the fact with a
similarity score; "declare when this applies" is a claim that can be checked
directly, and a condition the parent doesn't have provably separates the two —
so declaring one clears the description warning.

They are edited as chips but **compiled into the description on save**:

```
description: "Roam decisions driven by candidate grade delta. Applies when: platform is LNL; resume from S4."
```

Same structured-in / flat-out shape as lineage, for the same reason: Avatar's
loader ignores a `triggers:` key, so a boundary that lived only there would
document intent without ever enforcing it. Compiling is idempotent (the clause
is stripped before being rebuilt), and version snapshots store the base
sentence plus the trigger list separately so a restore brings back the
revision's own conditions rather than a stale baked-in clause.

### 4. Skill Library
A two-pane page (`/skills/`) for everything the workbench can see.

- **Base YAML picker** (top of the page) — which file this domain's skills are
  read from. The list is the local mirror of the corp share: the team baseline
  (`skills.yaml` / `bt_skills.yaml`) plus, for WiFi, each engineer contribution
  file found there — and a 📁 button for **any skills YAML anywhere on disk**
  (a colleague's export, a snapshot kept beside a case, an older copy), since
  the mirror only ever contains what the corp share ships. A browsed-in file is
  checked for parseability before it is adopted and gets its own entry in the
  dropdown, so the control can never show the team baseline while the pool
  actually came from elsewhere. WiFi and Bluetooth each keep their own choice,
  and the choice survives page reloads and switching between the Log Viewer
  and here.

  **The chosen file is the WHOLE baseline**, not a layer on top of the team
  file. Before this, the pool was an implicit three-way merge — team baseline
  → this engineer's contribution → local — that nobody could see or reproduce:
  a skill could resolve from any of the three, and the UI could only report
  which had won after the fact. That also quietly broke the Export promise
  that an inherited skill's inherited half is exactly one file's content.
  Locally-saved skills still layer on top, because those are this workbench's
  own output rather than part of any baseline.

- **Left — lineage forest.** Skills drawn by ancestry: indent is generation,
  with connector elbows. Each row carries an origin dot (blue `shared`,
  purple `contribution`, green `local`), a `LOADED` badge for the session's
  current baseline, its generation depth, and its version count. Selecting a
  node lights its whole ancestor chain in a dimmer style — context for the
  selection, not a second selection. A skill whose parent isn't in the pool
  is still drawn (as a root, with the dangling reference shown) rather than
  disappearing.
- **Right — version trail.** A clickable timeline of every recorded revision,
  live entry first. Selecting a past revision doesn't show a bare snapshot —
  it shows **what restoring it would do**: green for what would come back,
  red strikethrough for what would be dropped.
- **Load as baseline** sets which skill the next Export inherits from. It
  deliberately does *not* replace the filters on screen — the Log Viewer's own
  skill dropdown owns that.

Those are two genuinely different things, so they are two separate pieces of
state and two separate controls:

| | what it answers | set by |
|---|---|---|
| the Log Viewer's **skill dropdown** | whose keywords are on screen right now | loading a skill in the Log Viewer (cleared by opening a raw `.tat`) |
| the **"Export inherits from" badge** | what the next Export inherits from | "Load as baseline" in the Skill Library, or loading a skill in the Log Viewer |

The badge appears only when the two differ — which is exactly the state that
used to be invisible and made the dropdown look like it was lying. Its `×`
drops the inheritance without touching the filters.

- **Applies when** — the skill's trigger conditions (see the Export section),
  shown as chips.
- **Measured usage** — how many times this workbench loaded the skill, how many
  lines its filters last matched, and the keywords the engineer had to add
  *almost every time* they loaded it. That last one is a coverage gap stated by
  their hands rather than their words, and it is measured rather than inferred:
  a keyword added in 4 of 4 sessions is a concrete "this skill should probably
  own it". A skill never loaded here is flagged as such — which means *no local
  evidence either way*, never grounds to delete something a teammate may rely
  on. Stored in a sidecar `skill_memory.json`, never in the YAML Avatar reads.

Three rules the restore path holds to:

1. **Restore moves the version forward, never backward.** Restoring v0.1.1
   while at v0.1.4 lands you at v0.1.5 carrying v0.1.1's content, with v0.1.4
   pushed onto the trail. Rewinding the counter or truncating the trail would
   make restore the one operation in the app capable of losing work.
2. **Restore never touches `parent`/`lineage`.** No past revision of a
   skill's own body can change where it came from.
3. **Only `local` skills are editable or restorable.** Shared and
   contribution entries are a read-only mirror of the corp drive: their
   history is viewable, and saving an edit to one mints a *new* local skill
   rather than shadowing the original.

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
  skills/                     Skill library — lineage graph, version trail,
                                restore, set-as-baseline, CRUD
services/
  llm_service.py               LLM_helper — Anthropic/OpenAI-compatible client,
                                 token usage tracking, prompt caching
  session_store.py             Per-browser-session working state
                                 (incl. log_annotations)
  event_log_service.py         Windows System Event Log (.evt/.evtx) reader +
                                 capture-machine UTC-offset detection
                                 (systeminfo.txt) for click-sync
  skill_service.py             Skill model + YAML read/write, versioning,
                                 lineage, crash-safe atomic writes
  skill_retrieval.py           Agent C — IDF-weighted token retrieval (the
                                 cheap half of AutoSkill's dense+BM25 hybrid;
                                 no model, no network)
  skill_memory.py              Per-skill measured usage + coverage gaps,
                                 sidecar json, never in the skills YAML
  decision_ledger.py            Session-only ledger of interview questions +
                                 answers (Ask/Quiet), folded into a
                                 review-only spec at Export — never written
                                 into the skill YAML
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
  divergence.py                 Deterministic baseline-vs-action comparison —
                                  contradiction / omission split, materiality
                                  checked before opinion (zero LLM cost)
  skill_dedup.py                ACE-Curator-derived de-duplication, extension
                                  builder, lineage-depth + description-conflict
                                  guards (zero LLM cost)
  file_picker.py, browser_utils.py, helpers.py, json_utils.py
templates/                    log_viewer.html (main workbench markup + a
                                single bootstrap object of server values),
                                skills.html (Skill Library), base.html
static/js/
  log_viewer.js               All Log Viewer behaviour. Kept out of the
                                template so the browser can cache it, editors
                                can lint it, and a stale copy shows up as a
                                stale file rather than silently inside a
                                re-rendered page. Reads server values only
                                through the `LV` object the template defines.
  skill_editor.js             The shared Edit-Skill modal
tests/                          Unit tests for the learning pipeline. Local
                                  only, not checked in (see Tests below)
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

The suite is each engineer's own local verification material and is not
checked in, so a fresh clone won't have `tests/`.

```powershell
python -m unittest discover -s tests -v
```

136 tests, no LLM or network access required. They cover:

- **Filter engine** — after the hot loop was rewritten to test each rule once
  per line, every rule *kind* is pinned down against the old semantics:
  case-insensitive vs case-sensitive spelling, regex, a **disabled** rule
  still owing a raw hit count, a malformed regex matching nothing instead of
  raising, and an exclude still dropping its line and being credited for the
  drop — all read from the same single pass.
- **Baseline + divergence** — contradictions vs omissions, materiality
  checked before opinion, pre-baseline edits excluded, an omission producing
  a non-blocking open elicitation question (never a blocking, discriminating
  one) that isn't re-asked once elicited, a contradiction asked once then
  suppressed, effect attribution withheld across a focus-window change (a
  focused run and an unfocused one count different populations), and an
  Update Baseline delta correctly reporting what changed between two reads.
- **Decision ledger** — it stays a session-only sidecar (never touches the
  skill YAML), de-duplicates a repeated question by `source_key`, and
  resolving/deferring updates status without losing the original question.
- **IDF-weighted retrieval** — a rare shared token outweighs pool-wide
  boilerplate, an unseen token is treated as maximally distinctive, and
  scores stay stable regardless of which other candidate was excluded from
  the same search.
- **Skill-level memory** — usage counts and last-matched-lines survive
  round-trips, coverage gaps only surface once a keyword was added after
  nearly every load (not a one-off), and deleting a skill forgets its history.
- **De-duplication** — substring containment beating similarity ratio in both
  directions (`covered` vs `widens`), near-matches routed to review rather
  than auto-dropped, and rules itemised before comparison so a reworded
  duplicate is caught.
- **Inheritance** — the parent's content fully resolved into the child, the
  `parent`/`lineage` chain surviving the YAML round-trip *and* later edits,
  the loaded cloud skill never copied into the local file, the depth cap
  refusing a third generation without discarding the draft, and a
  description that merely rewords the parent's being flagged.
- **Version trail** — restore moving the version forward rather than
  rewinding, leaving lineage alone, and refusing on non-local skills.
- **Durable writes** — a failure while serializing or while replacing leaves
  the previous file byte-for-byte intact, no temp files are left behind, the
  backup holds the prior version, and an unparseable local file makes every
  write path refuse instead of erasing it.
- **Teaching evidence** — `assess_teaching_evidence` reports counterexamples
  without scoring correctness, deterministic (non-LLM) evidence-coverage
  computation, filter-identity-based E/X attribution, an include keyword
  labeled only by counterexamples being reported as over-matching (reported,
  never auto-moved into `exclusive`), and that a session
  reset clears case-specific teaching state.

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

Startup does the minimum synchronously and pushes the slow parts onto daemon
threads, so the server is listening and the UI is usable immediately:

- Skills load from the **local cache** first; the shared corp-drive refresh
  runs in the background and updates the pool when it lands. The app already
  had "if the sync fails, use the last cache" as a fallback — this just makes
  that the normal startup path rather than only a network-outage path.
- LLM key resolution reads from a UNC corp share and can take several
  seconds, so it also runs in the background. Until it finishes, LLM-backed
  routes return a clear "not configured yet" instead of hanging, and a
  self-removing banner polls `/llm_status` so you can see when it's ready.

**Teaching state lives in memory for the session only.** Closing the browser
or restarting the app starts a fresh teaching session by design — chat
history, the operation journal, and the committed baseline are all
case-specific and deliberately not carried across. Saved skills are on disk
and unaffected.

### Standalone executable

Engineers without a Python environment run `Copycat.exe` from the Releases
page instead. Same app, no install.

```powershell
pyinstaller copycat.spec        # -> dist\Copycat.exe
```

A frozen build has two roots and `configs/path_configs.py` keeps them apart:
`BUNDLE_ROOT` is the temp folder the one-file exe unpacks into and is wiped
on exit, so `templates/` and `static/` are read from there, while
`PROJECT_ROOT` is the folder the exe sits in and is the only place the app
writes. `data\skills\` therefore appears **next to the exe** and survives
restarts — keep the exe somewhere permanent, not in Downloads.

The build is unsigned, so Windows SmartScreen will warn on first run.

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
  triggers:                     # conditions this skill applies under; ALSO
    - "resume from S4"            #   compiled into `description` above
  parent: "connection_flow"     # only on an inherited skill — the key it was
  lineage:                        #   built on, and the full root→…→parent chain
    - "connection_flow"
  version: "0.1.3"
  version_history: |            # rolling JSON audit trail (pre-edit snapshots)
    [...]
```

`parent` / `lineage` / `triggers` are documentation and organisation only — Avatar's
loader ignores them, which is exactly why they are safe to carry. The
keywords, exclusive and expert_rules written here are always **fully
resolved** (the parent's content merged in), never delta-only. A loaded
"cloud" skill is never copied into this file: only the child lives here, with
its own `version` line starting at `0.1.0`.

**Writes are crash-safe.** The YAML is serialized *before* any file is
opened, written to a temp file, fsynced, then `os.replace()`d onto the target
— atomic on both Windows and POSIX, so a reader sees either the whole old
file or the whole new one, never a partial write. One generation of backup is
kept beside it as `skills.yaml.bak`. If the local file is ever unreadable,
every write path (save / delete / restore) **refuses loudly and leaves it
untouched** rather than treating it as "no skills" and persisting that
emptiness over the real content — the display side still degrades gracefully,
so a broken local file never stops the app from starting.

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
  Every file in here is a candidate baseline in the Skill Library's Base YAML
  picker; exactly one is active per domain at a time.

So each domain's pool is exactly two layers — **one chosen baseline file, then
the local file on top**. A skill's origin dot in the Library says which layer
it came from, and only `local` entries are writable.

Phase 2's retrieval-assisted skill maintenance (add/merge/discard judge)
only ever considers `local/` skills as merge targets — a shared-drive skill
that looks similar to new teaching never gets silently modified; the new
teaching becomes its own local skill instead.
