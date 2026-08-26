/*
 * Log Viewer workbench behaviour — everything the main page does: file
 * pickers, the TAT filter table, the focus window, the event-log panel, the
 * skill-building chat, the Steps panel, and the Export flow.
 *
 * Lives here rather than inline in log_viewer.html so the browser can cache
 * it, editors can lint it, and a stale copy is visible as a stale file rather
 * than silently served inside a re-rendered page.
 *
 * Everything the server has to inject arrives through the `LV` object that
 * log_viewer.html defines just before loading this file:
 *   LV.url.<endpoint>  — route URLs (url_for, dots replaced by underscores)
 *   LV.boot.<name>     — session state to restore on load
 * Nothing else in here may read from the template.
 */
let filterData = LV.boot.filters;
let operationData = LV.boot.operations;
let annotationData = LV.boot.logAnnotations;
// Last /apply_filter's divergence report (utils/divergence.py): which of the
// engineer's material, still-unexplained edits CONTRADICT the baseline read
// (worth one clarifying question) vs. which the baseline simply had no view
// on (omissions — a passive 🎓 hint in the Steps panel, never a question).
// Computed server-side with no LLM call, so it refreshes on every filter run.
let divergenceData = {};
// What the next Export inherits from. NOT the same thing as the skill behind
// the TAT Filter header's 🎓 picker (that one is filterSkillKey — whose
// keywords are actually on screen). They coincide when a skill is loaded from
// the Log Viewer, and diverge when a baseline is picked from the Skill
// Library, which deliberately leaves the filters alone.
let activeSkillKey = LV.boot.activeSkillKey;
let activeSkillName = LV.boot.baselineSkillName;
// Which skill's keywords are currently ON SCREEN. This used to be read off a
// <select> element; it is a plain variable now that the control is an icon
// picker, so the value no longer depends on a DOM node existing.
let filterSkillKey = LV.boot.filterSkillKey || '';
let filterSkillName = LV.boot.filterSkillName || '';
let currentDraft = null;
let decisionLedger = LV.boot.decisionLedger || {mode: 'ask', items: [], open: 0, resolved: 0, deferred: 0, blocking: 0};
let interviewMode = 'ask';
let selectedSkillKeys = Array.isArray(LV.boot.selectedSkillKeys) ? [...LV.boot.selectedSkillKeys] : [];
let availableSkillDocs = [];
let currentLogDomain = LV.boot.logDomain || 'wifi';
let contextLineCount = Number(LV.boot.contextLineCount || 0);
let contextTimeSpan = LV.boot.contextTimeSpan || {};
// The case summary used to live in a textarea inside a collapsible "Context"
// row. It is now asked for IN THE CONVERSATION (renderCaseIntakeCard), so the
// value has to survive as state rather than as a DOM node — the intake card is
// removed from the chat log once answered. Server-side it is still the same
// state.case_summary, still fed to every chat turn and every question prompt,
// so nothing downstream changed.
let caseSummaryText = LV.boot.caseSummary || '';
// Whether the intake card is currently on screen, so a second trigger (log
// reload racing the boot path) can't stack two of them.
let _caseIntakeOpen = false;
// Domain-specific UTC -> text-log conversion. WiFi text is stamped in the
// analysing engineer's local frame; BT HCI text is customer-local. Keep the
// basis visible so a plausible-looking nearest-time jump is never ambiguous.
let eventSyncOffsetMin = LV.boot.eventSyncOffsetMin;
let eventSyncBasis = LV.boot.eventSyncBasis || '';
let customerUtcOffsetMin = LV.boot.customerUtcOffsetMin;

// The LLM key.py read (UNC corp share, no timeout of its own) now happens on
// a background thread AFTER the server starts listening (see configs.
// set_up_app._configure_llm) instead of blocking app startup — so a page
// loaded in the first few seconds can render with llm_ready still false even
// though it'll very likely be true moments later. Poll /llm_status and clear
// the banner in place once it flips, instead of leaving a stale "not
// configured" warning that only a manual reload would fix.
if (!LV.boot.llmReady) {
(function pollLlmStatus(attempt) {
    attempt = attempt || 0;
    if (attempt > 20) return; // ~60s — genuinely unreachable by then, stop polling
    fetch(LV.url.main_llm_status)
        .then(r => r.json())
        .then(d => {
            if (d.ready) {
                const banner = document.getElementById('llmBanner');
                if (banner) banner.remove();
            } else {
                setTimeout(() => pollLlmStatus(attempt + 1), 3000);
            }
        })
        .catch(() => setTimeout(() => pollLlmStatus(attempt + 1), 3000));
})();
}

// Global "one async action at a time" lock — every control that triggers an
// LLM call or a state-mutating fetch (baseline read, Export Skill, chat send,
// step teach/ask, and the skill-select dropdown) is disabled while ANY of
// them is in flight. This is what stops a stray click mid-request from
// racing another action — the load-skill dropdown in particular must never
// change except from a deliberate, isolated user click while nothing else is
// busy. Elements opt in via the `data-busy-lock` attribute; dynamically
// created buttons (step teach/ask icons) also check isBusy() at creation time
// so a re-render mid-request doesn't hand out a fresh enabled button.
let _busy = false;
function isBusy() { return _busy; }
function setBusy(v) {
    _busy = v;
    document.querySelectorAll('[data-busy-lock]').forEach(elx => {
        // chatSendBtn is deliberately exempted below: while busy it turns
        // into a Stop button rather than a disabled one, so an in-flight
        // chat response can actually be cancelled instead of just ignored.
        if (elx.id === 'chatSendBtn') return;
        elx.disabled = v;
    });
    const sendBtn = document.getElementById('chatSendBtn');
    if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.classList.toggle('btn-primary', !v);
        sendBtn.classList.toggle('btn-outline-danger', v);
        sendBtn.setAttribute('aria-label', v ? 'Stop response' : 'Send chat message');
        sendBtn.innerHTML = v ? '<i class="fas fa-stop"></i>' : '<i class="fas fa-paper-plane"></i>';
    }
    // Re-apply the Baseline gate after the generic busy lock is released.
    // Otherwise setBusy(false) would enable Export even when no comparison
    // baseline exists (most visible after an allowed one-off chat send).
    if (document.getElementById('baselineBtn')) setBaselineGate();
}

// Which step (or "all" for general/session-wide knowledge) the NEXT typed
// chat message gets tagged with — set by the step-context pill row above the
// chat input (see renderStepTagSelector). Step-scoped interactions (teach/ask
// cards) tag their own messages directly with that step's seq, ignoring this.
let currentStepTag = 'all';

// textContent -> innerHTML escapes < > &, but NOT quotes -- and several call
// sites interpolate into a double-quoted attribute (data-raw-text, title),
// where a quote inside a log line would break out of that attribute.
function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Parse the leading timestamp + up to 4 consecutive [bracketed] fields into
// FIXED-WIDTH (ch-unit, monospace) columns so rows actually line up like a
// table — margin-based spacing (the previous approach) can't do this,
// because "[3]" and "[19]" are different widths, so everything after them
// drifts out of alignment row to row. Longer-than-slot values are ellipsized
// rather than breaking the grid. Only the run of brackets immediately after
// the timestamp is treated as fields; anything after that (including
// brackets that are part of the free-form message) is left as plain text.
//
// Always emits all 5 leading slots (timestamp + 4 tag slots), even when a
// line has none of them — e.g. a hex-dump/continuation line that wraps under
// a timestamped line above it. Without this, such lines would have nothing
// to push their text right, so it'd start flush under the line-number
// gutter instead of lining up under the *message* column like a real
// continuation should.
function formatLogLine(rawText) {
    const tsMatch = rawText.match(/^(\d{2}\/\d{2}\/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})\s*/);
    let rest = rawText;
    let tsText = '';
    if (tsMatch) {
        tsText = tsMatch[1];
        rest = rawText.slice(tsMatch[0].length);
    }
    let tags = [];
    const tagRun = rest.match(/^((?:\[[^\[\]]*\]\s*){1,4})/);
    if (tagRun) {
        tags = tagRun[0].match(/\[[^\[\]]*\]/g) || [];
        rest = rest.slice(tagRun[0].length);
    }
    const slotClasses = ['tat-col-id', 'tat-col-mod', 'tat-col-vis', 'tat-col-fn'];
    let html = `<span class="tat-col tat-col-ts">${escapeHtml(tsText)}</span>`;
    for (let i = 0; i < slotClasses.length; i++) {
        html += `<span class="tat-col ${slotClasses[i]}">${escapeHtml(tags[i] || '')}</span>`;
    }
    html += `<span class="tat-col-msg">${escapeHtml(rest)}</span>`;
    return html;
}

// Spreadsheet-style letter labels a, b, ... z, aa, ab, ... (TAT "Modifiers" column).
function letterLabel(n) {
    let s = '';
    do { s = String.fromCharCode(97 + (n % 26)) + s; n = Math.floor(n / 26) - 1; } while (n >= 0);
    return s;
}

function renderFilters() {
    const box = document.getElementById('filterList');
    if (!filterData.length) {
        box.innerHTML = '<div class="tat-filter-empty">No .tat file or skill loaded yet</div>';
        document.getElementById('filterCount').textContent = 0;
        return;
    }
    let html = '';
    let enabledCount = 0;
    filterData.forEach((f, i) => {
        if (f.enabled) enabledCount++;
        // Pattern cell painted with the filter's own colors (same colors that
        // highlight the matching log lines above), exactly like TAT's filter panel.
        let patStyle = '';
        if (f.back_color) patStyle += `background:${f.back_color};`;
        if (f.fore_color) patStyle += `color:${f.fore_color};`;
        const patClass = 'tat-filter-pattern' + (f.excluding ? ' is-exclude' : '');
        const badge = f.excluding ? '<span class="badge-exclude">EXCL</span> ' : '';
        const hits = (f.hits !== undefined) ? f.hits : '';
        // Authoritative teaching-evidence tally for this exact keyword+role
        // — always unambiguous (unlike the Steps panel's best-effort view),
        // since a filter's identity never depends on how/when it was added.
        const filterAnn = annotationData.filter(a =>
            (a.matched_keywords || []).some(k => k.excluding === f.excluding
                && k.text.toLowerCase() === String(f.text || '').toLowerCase())
        );
        const evN = filterAnn.filter(a => a.label === 'evidence').length;
        const cxN = filterAnn.filter(a => a.label === 'counterexample').length;
        // Only-X on an include keyword means every line it was labeled on was
        // called wrong — the one per-keyword over-matching signal the labels
        // can measure (see learning_service.assess_teaching_evidence).
        const overMatching = !f.excluding && cxN > 0 && evN === 0;
        const evidTitle = overMatching
            ? `${cxN} counterexample(s) and no evidence — this keyword may be over-matching`
            : (evN || cxN)
            ? `${evN} evidence line(s), ${cxN} counterexample(s) labeled for this keyword`
            : 'No labeled evidence yet';
        const evidText = (evN || cxN) ? `${evN}E/${cxN}X` : '';
        const evidCls = 'c-evid' + (overMatching ? ' is-over-matching' : '');
        html += `<div class="tat-filter-row">
            <span class="c-mod"><input type="checkbox" id="f_${i}" ${f.enabled ? 'checked' : ''} onchange="toggleFilter(${i}, this.checked)"><label for="f_${i}" class="tat-filter-letter">${letterLabel(i)}</label></span>
            <span class="c-pat"><span class="${patClass}" style="${patStyle}">${badge}${escapeHtml(f.text)}</span></span>
            <span class="c-hit">${hits}</span>
            <span class="${evidCls}" title="${evidTitle}">${evidText}</span>
            <span class="c-del"><button class="btn-remove-filter" onclick="removeFilter(${i})" title="Remove">&times;</button></span>
        </div>`;
    });
    box.innerHTML = html;
    document.getElementById('filterCount').textContent = enabledCount;
}

// Every filter edit the engineer makes is recorded server-side with its
// marginal effect; the Steps panel (chat header) is what surfaces that
// timeline and lets them teach WHY via the 🎓 flow.
function syncOps(d) {
    if (d && d.operations) {
        operationData = d.operations;
        surfaceRedFlags();
        renderStepPanel();
        renderStepTagSelector();
        setTeachingLocks();   // teach buttons were just rebuilt
    }
}

// Compact tag button next to the send button: shows current tag ("All" or
// "#N"); clicking it toggles a small dropdown of all available steps.
// Step-scoped cards (teach/red-flag) tag their own messages directly and
// ignore this; it's only for the free-typed chat box.

// Teaching steps are numbered from the first thing actually TAUGHT. The
// server's seq counts every filter edit from the session's first one, so with
// four pre-baseline setup edits the first taught step read "#5" — a number
// that answers a question nobody asked. Setup edits get no number at all:
// nothing can point at one, so a number would only be something to look up in
// vain. Display only — o.seq stays the wire identity for every server call.
function stepLabel(seq) {
    const op = operationData.find(o => o.seq === seq);
    if (!op) return '#' + seq;
    if (op.phase === 'setup') return 'Setup';
    const teaching = operationData.filter(o => o.phase !== 'setup');
    return '#' + (teaching.findIndex(o => o.seq === seq) + 1);
}

function renderStepTagSelector() {
    const dropdown = document.getElementById('stepTagDropdown');
    const btn = document.getElementById('stepTagBtn');
    if (!dropdown || !btn) return;
    // Setup edits are not taggable: a setup step can never carry a reason (no
    // teach button, never asked "why"), so tagging a message to one only
    // looked like it filed the knowledge there. It still reaches the model as
    // ordinary conversation, exactly as an "All" message does.
    const taggable = (o) => o.phase !== 'setup'
        && o.action !== 'load_skill' && o.action !== 'load_tat';
    if (currentStepTag !== 'all' && !operationData.some(o => o.seq === currentStepTag && taggable(o))) {
        currentStepTag = 'all';
    }
    btn.textContent = currentStepTag === 'all' ? 'All' : stepLabel(currentStepTag);

    dropdown.innerHTML = '';
    const mkPill = (tag, label, title) => {
        const b = el('button', 'step-tag-pill' + (currentStepTag === tag ? ' is-active' : ''), label);
        b.type = 'button';
        if (title) b.title = title;
        b.onclick = () => {
            currentStepTag = tag;
            renderStepTagSelector();
            dropdown.style.display = 'none';
        };
        return b;
    };
    dropdown.appendChild(mkPill('all', 'All', 'Tag as general / session-wide knowledge'));
    operationData.filter(taggable).forEach(o => {
        dropdown.appendChild(mkPill(o.seq, stepLabel(o.seq),
            `Tag as about step ${stepLabel(o.seq)} (${o.verb} "${o.label || o.text}")`));
    });
}

function toggleStepTagDropdown() {
    const dd = document.getElementById('stepTagDropdown');
    dd.style.display = dd.style.display === 'none' ? 'flex' : 'none';
}

// ---- Passive red-flag questions ------------------------------------------
// detect_red_flags() on the server is a deterministic (NO LLM) check that
// runs on every filter apply — a redundant keyword, a no-op exclude, a big
// unexplained drop, disabling something load-bearing. This surfaces those as
// an unprompted question card in chat, same interaction as an interview
// question but costing zero tokens and firing instantly. Each flag fires
// once (tracked by "seq:type" so re-syncing the same op list doesn't repeat it).
const _shownFlags = new Set();
function surfaceRedFlags() {
    operationData.forEach(o => {
        (o.red_flags || []).forEach(flag => {
            const key = o.seq + ':' + flag.type;
            if (_shownFlags.has(key)) return;
            _shownFlags.add(key);
            showRedFlagCard(o.seq, flag.question);
        });
    });
}

// Renders a chat card whose answer ties back to a SPECIFIC operation's
// `reason` (via /log_viewer/answer_red_flag, seq+question+answer -> that
// op's journal reason) — shared by two triggers with different framing:
//   - the passive red-flag detector (opts omitted): "noticed something"
//   - the active per-step "🎓 teach" button (opts.badge/opts.icon set):
//     the engineer explicitly asked to explain this step
// Either way the underlying mechanic is identical: recording the answer as
// THAT operation's reason is what makes the Operation journal's "why"
// indicator update for the right row, and what feeds compact()'s per-edit
// reason into skill synthesis instead of the answer floating free in chat.
function showRedFlagCard(seq, question, opts) {
    opts = opts || {};
    const badgeText = opts.badge || '🚩 Noticed something — not asked, just flagged';
    const prefix = opts.icon || '🚩 ';
    const box = document.getElementById('chatBox');
    const card = el('div', 'chat-question-card flag-card mb-2');
    const badge = el('div', 'chat-q-progress flag-progress', badgeText);
    card.appendChild(badge);
    card.appendChild(el('div', 'chat-q-text', question));

    const submit = (answer) => {
        if (isBusy()) return;
        setBusy(true);
        card.remove();
        appendMsg('assistant', prefix + question, seq);
        appendMsg('user', answer, seq);
        fetch(LV.url.log_viewer_answer_red_flag, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({seq: seq, question: question, answer: answer})
        }).then(r => r.json()).then(d => {
            setBusy(false);
            syncOpsQuiet(d); // reason now recorded server-side
            if (opts.afterSubmit) opts.afterSubmit();
        }).catch(() => setBusy(false));
    };
    const answerBox = buildAnswerBox({
        placeholder: 'Type your answer… (or Dismiss if not relevant)',
        hint: question,
        onSubmit: submit,
        onSkip: () => { card.remove(); if (opts.afterSubmit) opts.afterSubmit(); },
        skipLabel: 'Dismiss',
    });
    answerBox.style.display = 'flex';
    card.__answerBox = answerBox;
    card.appendChild(answerBox);
    box.appendChild(card);
    scrollChatToBottom();
}

// Same as syncOps but skips re-triggering red-flag surfacing / step render —
// used after answer_red_flag's own response, which only carries the reason
// update (no new flags to show, panel already reflects the just-answered one).
function syncOpsQuiet(d) {
    if (d && d.operations) { operationData = d.operations; renderStepPanel(); setTeachingLocks(); }
}

// ---- Step viewer -----------------------------------------------------------
// Read-only timeline: click a step (a filter edit) to jump the chat scroll to
// whatever was asked/said around that moment. Uses each operation's
// chat_index (how many chat messages existed at the moment it was recorded)
// to slice chatHistoryMirror — never sends anything, never asks a new question.
// Lives in its own permanent card now (left column, under TAT Filter) —
// always rendered, no open/close toggle to gate it behind.
let _setupStepsOpen = false;
function renderStepPanel() {
    const panel = document.getElementById('stepPanel');
    if (!panel) return;
    // The teach buttons below are rebuilt from scratch here, so whatever lock
    // the baseline gate applied to the previous set is gone — reapplied at the
    // end of this function.
    panel.innerHTML = '';

    const body = el('div', 'step-body');
    panel.appendChild(body);

    if (!operationData.length) {
        body.appendChild(el('div', 'step-empty', 'No filter edits yet this session.'));
        return;
    }
    // Setup edits (everything up to the baseline — see operation_journal's
    // `phase`) are how the engineer got the filter set into a workable state,
    // not knowledge being taught. They still belong in the panel, because the
    // baseline is only interpretable against the set they produced, but they
    // are shown as a quiet preamble: a collapsed header, a dot instead of a
    // step number, and no "why?" prompt.
    // Teaching steps are displayed from #1 (see stepLabel) — the pre-baseline
    // setup edits no longer push the first taught step's number up.
    let setupHeaderDone = false;
    let teachingHeaderDone = false;
    // Once a baseline exists the setup edits are settled history: they still
    // have to be reachable (the baseline only reads against the set they
    // produced) but they no longer compete for the space the teaching steps
    // need. Before the baseline they ARE the panel, so they stay open.
    const pastSetup = baselineDone || operationData.some(o => o.phase !== 'setup');
    const setupRows = el('div', 'step-setup-rows');
    setupRows.hidden = pastSetup && !_setupStepsOpen;
    operationData.forEach((o, i) => {
        const isSetup = o.phase === 'setup';
        if (isSetup && !setupHeaderDone) {
            setupHeaderDone = true;
            const n = operationData.filter(x => x.phase === 'setup').length;
            const label = 'Setup — getting the filter ready';
            const h = el(pastSetup ? 'button' : 'div', 'step-phase-head is-setup');
            h.title = 'Filter edits made before the baseline read. They shaped the starting '
                    + 'point rather than teaching anything, so they are not asked about.';
            if (pastSetup) {
                h.type = 'button';
                h.appendChild(el('span', 'step-phase-caret', _setupStepsOpen ? '\u25be' : '\u25b8'));
                h.appendChild(el('span', null, `${label} (${n})`));
                h.onclick = () => { _setupStepsOpen = !_setupStepsOpen; renderStepPanel(); };
            } else {
                h.textContent = label;
            }
            body.appendChild(h);
            body.appendChild(setupRows);
        }
        if (!isSetup && !teachingHeaderDone && setupHeaderDone) {
            teachingHeaderDone = true;
            body.appendChild(el('div', 'step-phase-head', 'Teaching — since the baseline'));
        }
        const from = o.chat_index;
        const to = (i + 1 < operationData.length) ? operationData[i + 1].chat_index : chatHistoryMirror.length;
        const count = Math.max(0, to - from);
        const item = el('button', 'step-item' + (o.excluding ? ' is-exclude' : '')
                                              + (isSetup ? ' is-setup' : ''));
        item.type = 'button';
        item.appendChild(el('span', 'step-seq', isSetup ? '·' : stepLabel(o.seq)));
        item.appendChild(el('span', 'step-text', o.verb + ' "' + (o.label || o.text) + '"'));
        if (o.effect_phrase) item.appendChild(el('span', 'step-effect', o.effect_phrase));
        if (o.reason) item.appendChild(el('span', 'step-has-reason', '✓ why'));
        if (o.reason) item.appendChild(el('span', 'step-has-reason', o.evidence_status === 'measured' ? 'measured' : 'asserted'));
        // Per-step teaching evidence — an INDIRECT, best-effort view: counts
        // log_annotations whose matched_keywords include this Step's own
        // keyword+role (see tat_parser.matched_keywords_for_line). The
        // authoritative count lives on the Filter list (renderFilters) since
        // filter attribution is always unambiguous; a "load skill/.tat" Step
        // brought in many keywords at once with no single one of its own, so
        // it deliberately shows nothing here.
        const stepAnn = (o.action === 'load_skill' || o.action === 'load_tat') ? [] : annotationData.filter(a =>
            (a.matched_keywords || []).some(k => k.excluding === o.excluding
                && k.text.toLowerCase() === String(o.text || '').toLowerCase())
        );
        if (stepAnn.length) {
            const evN = stepAnn.filter(a => a.label === 'evidence').length;
            const cxN = stepAnn.filter(a => a.label === 'counterexample').length;
            const parts = [];
            if (evN) parts.push(evN + ' evidence');
            if (cxN) parts.push(cxN + ' counterexample' + (cxN === 1 ? '' : 's'));
            if (parts.length) item.appendChild(el('span', 'step-evidence-count', parts.join(' / ')));
        }
        if (count) item.appendChild(el('span', 'step-msgcount', count + ' msg' + (count === 1 ? '' : 's')));
        item.title = 'Jump to what was said around this edit';
        item.onclick = () => jumpToStep(from, to);

        const row = el('div', 'step-row');
        row.appendChild(item);
        // "load_skill"/"load_tat" aren't a keyword add/remove with a "why" to
        // teach — only real filter edits get the teach/ask icons. Two entry
        // points, engineer's choice: 🎓 = write your own explanation first
        // (user-led); ❓ = have the LLM ask you one targeted question instead
        // (LLM-led, always skippable).
        if (o.action !== 'load_skill' && o.action !== 'load_tat' && !isSetup) {
            const teachBtn = el('button', 'step-teach-btn');
            teachBtn.type = 'button';
            teachBtn.innerHTML = '<i class="fas fa-graduation-cap"></i>';
            teachBtn.title = 'Teach this step — write your explanation, then LLM may ask a follow-up';
            // OMISSION hint: this edit had a measured effect, is still
            // unexplained, and the baseline read had no view on it either
            // (see utils/divergence.py). That's a provenance gap, not an
            // ambiguity — so it gets a passive, ignorable nudge here rather
            // than a clarifying question in chat. Never blocks anything, and
            // clears by itself once a reason is recorded (the edit then drops
            // out of the omission list server-side).
            const hint = (divergenceData.omissions || []).find(om => om.seq === o.seq && om.highlight);
            if (hint) {
                teachBtn.classList.add('step-teach-suggest');
                teachBtn.title = `Not explained yet — ${hint.effect_phrase}. `
                    + 'The first read of this filter had no view on this keyword, so this is knowledge only you have.';
            }
            teachBtn.disabled = isBusy();
            teachBtn.onclick = (e) => { e.stopPropagation(); openStepExplainBox(o.seq, teachBtn); };
            row.appendChild(teachBtn);
        }
        (isSetup ? setupRows : body).appendChild(row);
    });
}

function jumpToStep(fromIdx, toIdx) {
    const box = document.getElementById('chatBox');
    // Clear any previous highlight.
    box.querySelectorAll('.step-highlight').forEach(n => n.classList.remove('step-highlight'));
    const kids = box.children;
    if (fromIdx >= kids.length) {
        // Nothing was said yet at this point — scroll to end so it's obvious.
        scrollChatToBottom();
        return;
    }
    const end = Math.min(toIdx, kids.length);
    // Flash the BUBBLE itself (not the flex row wrapping it) so the highlight
    // shows against its own opaque background — see appendMsg's row > bubble
    // structure. Non-message children (question/explain/confirm cards) have
    // no .chat-bubble, so fall back to flashing the element itself.
    for (let i = fromIdx; i < end; i++) {
        (kids[i].querySelector('.chat-bubble') || kids[i]).classList.add('step-highlight');
    }
    kids[fromIdx].scrollIntoView({block: 'center', behavior: 'smooth'});
}

// ---- Compact path input helpers (copy-to-clipboard + transient status,
// mirrors IntelAvatar log_chatbot's Log File sidebar section) ----
function showPathStatus(elId, msg, type) {
    const el = document.getElementById(elId);
    el.className = 'path-status ' + type;
    el.textContent = msg;
    if (type === 'ok') {
        setTimeout(() => {
            if (el.textContent === msg) { el.className = 'path-status'; el.textContent = ''; }
        }, 3000);
    }
}

function copyPath(inputId, btn) {
    const val = document.getElementById(inputId).value || '';
    if (!val.trim()) return;
    const flash = () => {
        if (!btn) return;
        btn.classList.add('copied');
        setTimeout(() => btn.classList.remove('copied'), 1200);
    };
    if (navigator.clipboard) {
        navigator.clipboard.writeText(val).then(flash).catch(() => {
            const inp = document.getElementById(inputId);
            inp.select(); document.execCommand('copy'); flash();
        });
    } else {
        const inp = document.getElementById(inputId);
        inp.select(); document.execCommand('copy'); flash();
    }
}

// Repopulate both skill pickers for the given domain ('wifi' | 'bt') and
// show a small badge so it's clear which skill set is in play.
function refreshSkillList(domain) {
    currentLogDomain = domain || 'wifi';
    fetch(`/skills/list?domain=${domain}`).then(r => r.json()).then(d => {
        if (!d.success) return;
        availableSkillDocs = d.skills || [];
        // A skill loaded from the other domain isn't in this list any more, so
        // the header picker must stop claiming it is on screen.
        if (filterSkillKey && !availableSkillDocs.some(s => s.key === filterSkillKey)) {
            filterSkillKey = '';
            filterSkillName = '';
        }
        renderSkillLoadPicker();
        // A domain change makes documents from the old domain invalid. The
        // server applies the same validation; doing it here keeps the picker
        // honest before that round trip finishes.
        const valid = new Set(availableSkillDocs.map(s => s.key));
        const filteredSelection = selectedSkillKeys.filter(key => valid.has(key));
        if (filteredSelection.length !== selectedSkillKeys.length) {
            selectedSkillKeys = filteredSelection;
            persistQuestionContext({selected_skill_keys: selectedSkillKeys});
        }
        renderSkillDocPicker();

        const badge = document.getElementById('domainBadge');
        badge.textContent = domain === 'bt' ? 'BT' : 'WiFi';
        badge.className = 'domain-badge ' + (domain === 'bt' ? 'domain-bt' : 'domain-wifi');
        badge.style.display = 'inline-block';
        updateQuestionContextStatus();
        renderExportBaselineBadge();
    });
}

// ---- The TAT Filter header's 🎓 skill picker ------------------------------
// Was a full-width "-- or load a learned skill --" <select> plus an All/None
// pair sitting on their own row inside the card body. That row cost the filter
// table a chunk of the card's fixed height permanently, for controls used once
// or twice a session — so both moved up into the header, and the select became
// an icon that opens the same popup shape the chat header's "Load skills" uses.
function toggleSkillLoadPicker(force) {
    const menu = document.getElementById('skillLoadMenu');
    const button = document.getElementById('skillLoadToggle');
    if (!menu || !button) return;
    const open = force !== undefined ? force : menu.hidden;
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) positionSkillLoadMenu();
}

// The menu is position:fixed (see the CSS note), so it has to be placed by
// hand: below its button normally, flipped above it when the workbench sits
// low enough that the list would run off the bottom of the window — which it
// does at ordinary laptop heights, and used to hide most of the skills.
function placeAnchoredMenu(menu, button) {
    if (!menu || !button || menu.hidden) return;
    const rect = button.getBoundingClientRect();
    const gap = 6;
    const edge = 10;                       // never touch the window edge
    const below = window.innerHeight - rect.bottom - gap - edge;
    const above = rect.top - gap - edge;
    const flip = below < 220 && above > below;
    menu.style.maxHeight = Math.max(150, Math.floor(flip ? above : below)) + 'px';
    menu.style.left = Math.round(Math.max(
        edge, Math.min(rect.left, window.innerWidth - menu.offsetWidth - edge))) + 'px';
    if (flip) {
        menu.style.top = 'auto';
        menu.style.bottom = Math.round(window.innerHeight - rect.top + gap) + 'px';
    } else {
        menu.style.bottom = 'auto';
        menu.style.top = Math.round(rect.bottom + gap) + 'px';
    }
}

function positionSkillLoadMenu() {
    placeAnchoredMenu(
        document.getElementById('skillLoadMenu'),
        document.getElementById('skillLoadToggle'),
    );
}

function renderSkillLoadPicker() {
    const toggle = document.getElementById('skillLoadToggle');
    if (toggle) {
        toggle.classList.toggle('has-skill', !!filterSkillKey);
        toggle.title = filterSkillKey
            ? `Filter keywords loaded from "${filterSkillName || filterSkillKey}" — click to load a different skill.`
            : 'Load a learned skill\'s keywords as the filter.';
    }
    const list = document.getElementById('skillLoadList');
    if (!list) return;
    if (!availableSkillDocs.length) {
        list.innerHTML = '<div class="skill-doc-picker-empty">No learned skills for this domain yet.</div>';
        return;
    }
    list.innerHTML = availableSkillDocs.map(skill => `
        <button type="button" class="skill-load-option${skill.key === filterSkillKey ? ' is-current' : ''}"
                onclick="loadSkill('${escapeHtml(skill.key)}')">
          <span><b>${escapeHtml(skill.name)}</b><small>${escapeHtml(skill.description || 'No description')}</small></span>
          ${skill.key === filterSkillKey ? '<i class="fas fa-check"></i>' : ''}
        </button>`).join('');
}

// Render the log (raw, before any filter — or filtered, once one has run)
// as TAT-style rows: sticky line-number gutter + column-aligned, optionally
// colored text.
// ---- Virtual log pane -----------------------------------------------------
// A real capture is six figures of lines and every rendered row is ~9 DOM
// nodes, so putting a whole result on the page costs ~1M nodes and seconds of
// layout — which is why the pane used to be hard-capped at the first 500 rows.
// Instead only the rows in (and just outside) the viewport are ever in the
// DOM: two spacer divs stand in for the rest so the scrollbar still represents
// the full result, and pages are pulled from /log_viewer/preview_page as they
// scroll into range. Rows are single-line (white-space:nowrap), so one
// measured row height describes every row.
const vlog = {
    gen: 0,             // bumped per view; page responses from an older view are dropped
    total: 0,
    rows: [],           // sparse, indexed by view position
    rowH: 0,
    pageSize: 500,      // matches the server's own page size
    pending: new Set(),
    emptyMessage: '',
    first: -1,
    last: -1,
    maxLineNo: 0,
    minWidth: 0,        // widest content seen, so scrolling can't shrink the pane
    highlightIndex: null,
};

function logRowHtml(p) {
    let style = '';
    if (p.back_color) style += `background:${p.back_color};`;
    if (p.fore_color) style += `color:${p.fore_color};`;
    // data-ms = the row's timestamp as epoch ms (or empty) — lets the event
    // panel jump to the nearest log line by time, and lets a log-row click
    // find the nearest event.
    const ms = parseLogTimeMs(p.text);
    const annotation = annotationData.find(item => item.line_no === p.line_no);
    const annotationClass = annotation ? ` is-annotated annotation-${annotation.label}` : '';
    const activeCls = (label) => (annotation && annotation.label === label) ? ' is-active' : '';
    const matchedArr = '[' + (p.matched || []).join(',') + ']';
    const annotationButtons = p.line_no == null ? '' : `<span class="log-annotation-tools">
        <button type="button" data-label="evidence" aria-label="Mark as supporting evidence" aria-pressed="${!!annotation && annotation.label === 'evidence'}" class="${activeCls('evidence').trim()}" title="Evidence — this line supports the scenario or rule" onclick="annotateLogLine(event, ${p.line_no}, 'evidence', ${matchedArr})">E</button>
        <button type="button" data-label="counterexample" aria-label="Mark as counterexample" aria-pressed="${!!annotation && annotation.label === 'counterexample'}" class="${activeCls('counterexample').trim()}" title="Counterexample — this line is an exception or challenges the rule" onclick="annotateLogLine(event, ${p.line_no}, 'counterexample', ${matchedArr})">X</button>
    </span>`;
    return `<div class="tat-log-row${annotationClass}" data-line-no="${p.line_no == null ? '' : p.line_no}" data-raw-text="${escapeHtml(p.text)}"${ms != null ? ` data-ms="${ms}"` : ''}>` +
        `<span class="tat-log-lineno">${p.line_no != null ? p.line_no : ''}</span>` +
        `<span class="tat-log-text" style="${style}">${formatLogLine(p.text)}</span>` +
        annotationButtons +
        `</div>`;
}

function logPlaceholderRowHtml() {
    return '<div class="tat-log-row tat-log-row-loading">' +
        '<span class="tat-log-lineno"></span><span class="tat-log-text">…</span></div>';
}

// Start a NEW view: `preview` is its first page, `total` the number of rows
// the whole view has (defaults to the page itself, for callers with no total).
function renderLogRows(preview, emptyMessage, total) {
    const box = document.getElementById('previewBox');
    const rows = preview || [];
    vlog.gen++;
    vlog.rows = [];
    vlog.pending.clear();
    vlog.total = (total == null) ? rows.length : Math.max(total, rows.length);
    vlog.emptyMessage = emptyMessage || 'Empty file';
    vlog.first = vlog.last = -1;
    vlog.maxLineNo = 0;
    vlog.minWidth = 0;
    vlog.highlightIndex = null;
    setLogGotoNote('');     // the note described the view being replaced
    rows.forEach((p, i) => { vlog.rows[i] = p; });
    if (!vlog.total) {
        box.innerHTML = `<div class="tat-log-empty">${escapeHtml(vlog.emptyMessage)}</div>`;
        return;
    }
    // Rows sit inside .tat-log-inner (display:inline-block) instead of
    // directly in the scroll container — an inline-block shrink-wraps to
    // whatever the WIDEST row's content actually needs, and every row inside
    // it is width:100% of THAT shared width. Without this wrapper each row's
    // own width:auto only ever resolved against the viewport, so a short
    // line's colored .tat-log-text span (flex:1 0 auto now, see CSS) had
    // nothing wide to grow into and its color band stopped right where the
    // text did — full-length color bars only ever appeared for the single
    // longest line on screen.
    box.innerHTML = '<div class="tat-log-inner">'
        + '<div class="tat-log-pad"></div><div class="tat-log-body"></div><div class="tat-log-pad"></div>'
        + '</div>';
    box.scrollTop = 0;
    renderVisibleLogRows(true);
}

function renderVisibleLogRows(force) {
    const box = document.getElementById('previewBox');
    const inner = box.querySelector('.tat-log-inner');
    if (!inner) return;
    const pads = inner.querySelectorAll('.tat-log-pad');
    const body = inner.querySelector('.tat-log-body');
    if (!vlog.rowH) {
        // Read the ONE height every row is pinned to (--log-row-h, see
        // .tat-log-row) rather than measuring whichever row happened to render
        // first: a measurement is only right if every other row matches it,
        // and making that true is exactly what the CSS variable is for.
        const declared = parseFloat(
            getComputedStyle(document.documentElement).getPropertyValue('--log-row-h'));
        if (declared > 0) {
            vlog.rowH = declared;
        } else {
            body.innerHTML = logRowHtml(vlog.rows[0] || {line_no: 1, text: ' '});
            vlog.rowH = (body.firstElementChild && body.firstElementChild.offsetHeight) || 15;
        }
    }
    const overscan = 25;
    const first = Math.max(0, Math.floor(box.scrollTop / vlog.rowH) - overscan);
    const last = Math.min(vlog.total, first + Math.ceil(box.clientHeight / vlog.rowH) + overscan * 2);
    if (!force && first === vlog.first && last === vlog.last) return;
    vlog.first = first;
    vlog.last = last;

    let html = '';
    let hasGap = false;
    for (let i = first; i < last; i++) {
        const row = vlog.rows[i];
        if (!row) { html += logPlaceholderRowHtml(); hasGap = true; continue; }
        if (row.line_no > vlog.maxLineNo) vlog.maxLineNo = row.line_no;
        html += logRowHtml(row);
    }
    // Size the line-number gutter to the widest line number SEEN SO FAR, never
    // shrinking it — re-deriving it per window would shift the timestamp column
    // sideways every time scrolling crossed a digit boundary.
    box.style.setProperty('--lineno-w', Math.max(4, String(vlog.maxLineNo).length + 0.5) + 'ch');
    pads[0].style.height = (first * vlog.rowH) + 'px';
    pads[1].style.height = (Math.max(0, vlog.total - last) * vlog.rowH) + 'px';
    body.innerHTML = html;
    // Same reasoning as the gutter: the inline-block shrink-wraps to the widest
    // row currently rendered, so without a floor the horizontal scroll range
    // would jump around as rows enter and leave the window.
    inner.style.minWidth = '';
    vlog.minWidth = Math.max(vlog.minWidth, inner.scrollWidth);
    if (vlog.minWidth > box.clientWidth) inner.style.minWidth = vlog.minWidth + 'px';
    highlightLogIndex();
    if (hasGap) requestLogPages(first, last);
}

function requestLogPages(first, last) {
    const gen = vlog.gen;
    const firstPage = Math.floor(first / vlog.pageSize);
    const lastPage = Math.floor(Math.max(first, last - 1) / vlog.pageSize);
    for (let p = firstPage; p <= lastPage; p++) {
        const offset = p * vlog.pageSize;
        if (vlog.pending.has(p) || vlog.rows[offset] !== undefined) continue;
        vlog.pending.add(p);
        fetch(LV.url.log_viewer_preview_page, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({offset: offset, limit: vlog.pageSize}),
        }).then(r => r.json()).then(d => {
            if (gen !== vlog.gen) return;  // the view was replaced mid-flight
            vlog.pending.delete(p);
            if (!d || !d.success || !d.rows) return;
            d.rows.forEach((row, k) => { vlog.rows[d.offset + k] = row; });
            renderVisibleLogRows(true);
        }).catch(() => { if (gen === vlog.gen) vlog.pending.delete(p); });
    }
}

// Centre view row `index` in the pane and flash it. The row may not be loaded
// yet; the highlight is re-applied on every render until the view changes.
function scrollLogToIndex(index) {
    const box = document.getElementById('previewBox');
    if (index == null || !vlog.rowH) return;
    vlog.highlightIndex = index;
    box.scrollTop = Math.max(0, index * vlog.rowH - box.clientHeight / 2 + vlog.rowH / 2);
    renderVisibleLogRows(true);
}

// ---- Getting somewhere precisely -----------------------------------------
// A 100k-line view is 1.9M virtual pixels in a ~350px pane: the scrollbar
// thumb bottoms out at its 17px minimum and one pixel of drag covers ~300
// rows, so dragging can only ever express "somewhere over there" and there is
// no scroll-speed setting that changes that — the ratio is total rows over
// track pixels. The answer is not a slower scrollbar but what the scrollbar
// can't do: go exactly somewhere.

// Scroll so `index` (0-based view row) is the TOP row, clamped into range.
function scrollLogToRowTop(index) {
    const box = document.getElementById('previewBox');
    if (!vlog.rowH || !vlog.total) return;
    const clamped = Math.max(0, Math.min(vlog.total - 1, index));
    box.scrollTop = clamped * vlog.rowH;
    renderVisibleLogRows(false);
}

function gotoLogRow(rowNumber) {
    const n = parseInt(rowNumber, 10);
    if (!Number.isFinite(n) || !vlog.total) return;
    // The number typed is the SOURCE line number — the one in the pane's left
    // column and the only one on screen. Only the server knows where a given
    // line sits in the current view, since the view is paged and, once
    // filtered, is a sparse subset of the file.
    fetch(LV.url.log_viewer_row_for_line, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({line_no: n}),
    }).then(r => r.json()).then(d => {
        if (!d.success || d.index == null) return;
        // Centre it rather than putting it flush at the top — an engineer
        // jumping to a line almost always wants the lines around it too.
        scrollLogToIndex(d.index);
        setLogGotoNote(d.exact ? '' : `line ${n} filtered out — showing ${d.line_no}`);
    }).catch(() => {});
}

// Says where Go-to actually landed when it couldn't land where it was asked.
// Silence there was the whole problem: the pane just moved somewhere else.
function setLogGotoNote(text) {
    const note = document.getElementById('logGotoNote');
    if (!note) return;
    note.textContent = text;
    note.hidden = !text;
}

function highlightLogIndex() {
    const box = document.getElementById('previewBox');
    box.querySelectorAll('.tat-log-row.is-time-hit').forEach(r => r.classList.remove('is-time-hit'));
    if (vlog.highlightIndex == null) return;
    const body = box.querySelector('.tat-log-body');
    const el = body && body.children[vlog.highlightIndex - vlog.first];
    if (el) el.classList.add('is-time-hit');
}


// ---- Select-a-token-in-a-line -> make it a filter -------------------------
// The lightest gesture in the interactive-log-parsing literature: instead of
// reading a token off a line and retyping it into the filter box, select it
// and click. Two actions rather than one, because Copycat's two filter kinds
// mean opposite things and the paper's single "dummy token" idea maps onto
// neither cleanly: an include keyword KEEPS lines, an exclude term DROPS them,
// so silently picking one for the engineer would be a guess about intent.
//
// The pay-off is not saved typing, it is that the selected text is verbatim —
// retyped keywords are where "TASK_DISCONNECT" quietly becomes
// "TASK_DISCONECT" and matches nothing.
let tokenPickSelection = '';

function hideTokenPicker() {
    const el = document.getElementById('tokenPicker');
    if (el) el.style.display = 'none';
    tokenPickSelection = '';
}

function showTokenPicker(text, x, y) {
    let el = document.getElementById('tokenPicker');
    if (!el) {
        el = document.createElement('div');
        el.id = 'tokenPicker';
        el.className = 'token-picker';
        el.innerHTML =
            '<span class="token-picker-text"></span>' +
            '<button type="button" data-kind="include" title="Filter the log down to lines containing this">+ Keyword</button>' +
            '<button type="button" data-kind="exclude" title="Drop lines containing this as noise">&minus; Noise</button>';
        document.body.appendChild(el);
        el.addEventListener('mousedown', (e) => e.preventDefault()); // keep the selection alive
        el.addEventListener('click', function (e) {
            const btn = e.target.closest('button[data-kind]');
            if (!btn || !tokenPickSelection) return;
            addFilterText(tokenPickSelection, btn.dataset.kind === 'exclude');
            hideTokenPicker();
        });
    }
    tokenPickSelection = text;
    el.querySelector('.token-picker-text').textContent =
        text.length > 32 ? text.slice(0, 32) + '…' : text;
    el.style.display = 'flex';
    // Clamp into the viewport — a selection near the right edge would
    // otherwise push the picker off screen.
    const w = el.offsetWidth || 220;
    el.style.left = Math.max(4, Math.min(x, window.innerWidth - w - 8)) + 'px';
    el.style.top = Math.max(4, y - el.offsetHeight - 8) + 'px';
}

document.addEventListener('mouseup', function (e) {
    if (e.target.closest && e.target.closest('#tokenPicker')) return;
    const sel = window.getSelection();
    const text = sel ? String(sel).trim() : '';
    // Only inside the log pane, and only a token-sized selection — a stray
    // drag across half the log is not someone naming a keyword.
    const inLog = sel && sel.anchorNode && sel.anchorNode.parentElement
        && sel.anchorNode.parentElement.closest('.tat-log-text');
    if (!text || !inLog || text.length > 120 || text.includes('\n')) {
        hideTokenPicker();
        return;
    }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    showTokenPicker(text, rect.left, rect.top);
});
document.addEventListener('scroll', hideTokenPicker, true);

function annotateLogLine(event, lineNo, label, matchedFilters) {
    event.stopPropagation();
    const row = event.target.closest('.tat-log-row');
    fetch(LV.url.log_viewer_annotate_line, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            line_no: lineNo, label: label, text: row ? row.dataset.rawText : '',
            matched_filters: matchedFilters || [],
        })
    }).then(r => r.json()).then(d => {
        if (!d.success) { alert(d.message || 'Annotation failed'); return; }
        annotationData = d.annotations || [];
        const annotation = annotationData.find(item => item.line_no === lineNo);
        row.className = 'tat-log-row' + (annotation ? ` is-annotated annotation-${annotation.label}` : '');
        // Re-mark which single button (E/C/N/X) is the active one for this
        // line now — toggling off clears every button back to inactive.
        row.querySelectorAll('.log-annotation-tools button').forEach((btn) => {
            const active = !!annotation && btn.dataset.label === annotation.label;
            btn.classList.toggle('is-active', active);
            btn.setAttribute('aria-pressed', String(active));
        });
        // A new/changed annotation can change which filter(s) show an
        // evidence/counterexample count — keep the Filter list and Steps
        // panel in sync live (both derive their counts from annotationData).
        renderFilters();
        renderStepPanel();
    }).catch(e => alert('Annotation failed: ' + e));
}

// Parse a driver-log line's leading "MM/DD/YYYY-HH:MM:SS.mmm" timestamp into
// epoch ms (local), or null if the line has none. Used for event↔log sync.
function parseLogTimeMs(text) {
    const m = String(text).match(/^(\d{2})\/(\d{2})\/(\d{4})-(\d{2}):(\d{2}):(\d{2})\.(\d{3})/);
    if (!m) return null;
    return new Date(+m[3], +m[1] - 1, +m[2], +m[4], +m[5], +m[6], +m[7]).getTime();
}

// Parse an event-log "YYYY-MM-DD HH:MM:SS" (UTC) timestamp into epoch ms,
// SHIFTED by the domain-specific offset so it lands in the same wall-clock
// frame as parseLogTimeMs(): engineer-local for WiFi, customer-local for BT.
function parseEvtTimeMs(text) {
    const m = String(text).match(/^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})/);
    if (!m || typeof eventSyncOffsetMin !== 'number') return null;
    let ms = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime();
    ms += eventSyncOffsetMin * 60000;
    return ms;
}

// ---- Collapsing the two file pickers, and fitting the page to the window ---
// The Log picker and the System Event Log picker are each used once at the
// start of a session and then never again — but they were holding ~90px of
// permanent vertical space, and that was the difference between the workbench
// fitting the window and hanging below the fold. Below the fold is where the
// case-intake card's Continue/Skip buttons ended up, which is what made the
// whole page feel like it was sliding around. So both fold into a one-line
// chip once they have a file, and click reopens the full picker.
function _fileName(path) {
    const parts = String(path || '').split(/[\\/]/);
    return parts[parts.length - 1] || '';
}

function setLogBarCollapsed(collapsed) {
    const full = document.getElementById('logBarFull');
    const chip = document.getElementById('logBarChip');
    const collapseBtn = document.getElementById('logBarCollapseBtn');
    if (!full || !chip) return;
    const path = document.getElementById('logPathInput').value.trim();
    const canCollapse = collapsed && !!path;      // never hide the only way to load one
    full.hidden = canCollapse;
    chip.hidden = !canCollapse;
    // Only worth offering once there's a file to fold back to — with none
    // picked yet the row has to stay open, so a collapse control on it would
    // do nothing.
    if (collapseBtn) collapseBtn.hidden = !path;
    if (canCollapse) {
        document.getElementById('logBarChipName').textContent = _fileName(path);
        chip.title = path;
    }
}

function setEvtPathCollapsed(collapsed) {
    const group = document.getElementById('evtPathGroup');
    const chip = document.getElementById('evtPathChip');
    const collapseBtn = document.getElementById('evtPathCollapseBtn');
    if (!group || !chip) return;
    const path = document.getElementById('evtPathInput').value.trim();
    const canCollapse = collapsed && !!path;
    group.hidden = canCollapse;
    chip.hidden = !canCollapse;
    if (collapseBtn) collapseBtn.hidden = !path;
    if (canCollapse) {
        document.getElementById('evtPathChipName').textContent = _fileName(path);
        chip.title = path;
    }
}

// The log pane keeps the flat height CSS gives it (.tat-log's 48vh) — it was
// briefly made to shrink-to-fit the viewport so the page never scrolled, but
// that made the log itself, the thing actually being worked on, too short to
// read whenever the workbench below it was tall. The log wins; the page
// scrolls instead. Only Chat/Steps/TAT Filter are kept to a fixed height (see
// .workbench-panel).

function pickLog() {
    fetch(LV.url.log_viewer_pick_log, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                baselineDone = false;
                baselineEverDone = false;   // new capture = new case, nothing left to export
                contextLineCount = 0;
                contextTimeSpan = {};
                // A new capture is a new case — drop the previous framing and
                // ask for this one in the conversation (renderCaseIntakeCard
                // below, once the log has actually rendered).
                caseSummaryText = '';
                updateQuestionContextStatus();
                document.getElementById('logPathInput').value = d.log_path;
                setLogBarCollapsed(true);
                showPathStatus('logStatus', '✔ Log loaded', 'ok');
                if (d.domain) refreshSkillList(d.domain);

                // Show the log immediately — don't make the engineer wait
                // for a .tat/skill to be loaded just to see the file.
                document.getElementById('logPaneTitle').textContent = 'Log';
                lastMatchCount = d.view_total;
                document.getElementById('matchCount').textContent = d.view_total;
                document.getElementById('totalLines').textContent = d.total_lines;
                document.getElementById('statsSummary').textContent = 'no filter applied yet';
                document.getElementById('statsSummary').title = '';
                renderLogRows(d.preview, null, d.view_total);
                // A new log always clears any focus window server-side (see
                // /pick_log) — an issue time from the OLD file's frame could
                // silently slice this one down to zero lines. Reset the UI too.
                document.getElementById('focusTimeInput').value = '';
                setFocusUiState(null);
                // BT capture → reveal the event panel (auto-discovered file
                // enables it; WiFi/none hides it). Reset any open state first.
                eventSyncOffsetMin = d.event_sync_offset_min;
                eventSyncBasis = d.event_sync_basis || '';
                customerUtcOffsetMin = d.customer_utc_offset_min;
                updateEvtSection(d.domain, d.event_log_available, d.event_log_path);
                // This log's own lines had no date (dateless BT HCI export /
                // WiFi DDD export) — the DATE component shown is an estimate
                // (see the badge's tooltip for the anchor source).
                document.getElementById('dateSynthWarn').style.display = d.date_synthesized ? 'inline' : 'none';

                // Server clears the filter set when the new log's domain
                // differs from the previous one (see /pick_log's
                // filters_cleared) — a WiFi filter re-run against a freshly
                // loaded BT capture would just silently return "0 lines
                // matched", which reads exactly like the new log failed to
                // load. Sync filterData either way so the TAT Filter table
                // never shows something the server doesn't actually have.
                filterData = d.filters || [];
                renderFilters();
                if (d.filters_cleared) {
                    showPathStatus('tatStatus', 'Different log domain — previous filter cleared', 'ok');
                } else if (filterData.length) {
                    // Filters already set up — run right away (replaces this
                    // raw view), then read the filter set against the NEW log.
                    applyFilter().then(() => setBaselineGate());
                }
                // Ask what the case is about, in the conversation, now that
                // there is a capture to talk about. Answering it lights the
                // baseline button (setBaselineGate) as the next step.
                renderCaseIntakeCard();
            } else showPathStatus('logStatus', d.message, 'err');
        });
}

// Jumps the log pane back to the raw, unfiltered view — reuses the already-
// picked log_path server-side (see /log_viewer/show_all) instead of opening
// the file dialog again. Doesn't touch filters/operations: re-running a
// filter afterward (e.g. via a checkbox toggle) picks up right where it left
// off, this is purely a "let me see everything again for a sec" view switch.
function showAllLog() {
    fetch(LV.url.log_viewer_show_all, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (!d.success) { alert(d.message); return; }
            document.getElementById('logPaneTitle').textContent = 'Log';
            lastMatchCount = d.view_total;
            document.getElementById('matchCount').textContent = d.view_total;
            document.getElementById('totalLines').textContent = d.total_lines;
            document.getElementById('statsSummary').textContent = '';
            document.getElementById('statsSummary').title = '';
            renderLogRows(d.preview, null, d.view_total);
            // /show_all always clears any active focus window server-side —
            // mirror that in the UI so the Focus controls don't lie about state.
            setFocusUiState(null);
        });
}

// ── Issue-time focus window (±N min) ────────────────────────────────────────
// Narrows both the raw preview and every subsequent /apply_filter run to a
// small time-bounded slice around a picked "issue time" (see utils.tat_parser.
// slice_by_focus_window / /log_viewer/set_focus). The point isn't the
// narrower view by itself — it's that a checkbox toggle on a multi-million-
// line capture then only rescans that slice instead of the whole file.
// Persists server-side until cleared or a different log is picked.
// ── Focus window popover ──────────────────────────────────────────────────
// The crosshair button opens it; the time field only exists while it is open.
// Keeping a text box and two buttons permanently in the log header spent the
// widest part of the toolbar on a control most sessions never touch.
let focusDraftSnapshot = null;

function toggleFocusSelection() {
    const pop = document.getElementById('focusPopover');
    if (!pop || pop.style.display === 'none') {
        toggleFocusPopover(true);
    } else {
        cancelFocusSelection();
    }
}

function toggleFocusPopover(force) {
    const pop = document.getElementById('focusPopover');
    if (!pop) return;
    const open = force !== undefined ? force : pop.style.display === 'none';
    pop.style.display = open ? 'flex' : 'none';
    const focusBtn = document.getElementById('focusBtn');
    focusBtn.classList.toggle('is-open', open);
    focusBtn.setAttribute('aria-expanded', String(open));
    if (open) {
        focusDraftSnapshot = {
            time: document.getElementById('focusTimeInput').value,
            windowMin: document.getElementById('focusWindowInput').value,
        };
        // Seed from the first visible log line's own time — the engineer is
        // almost always focusing somewhere inside what they can see, and an
        // empty picker is fiddly to fill from scratch.
        let seed = document.getElementById('focusTimeInput').value;
        if (!seed) {
            const first = document.querySelector('#previewBox .tat-log-row:not(.tat-log-row-loading) .tat-log-text');
            const m = first && first.textContent.match(/(\d{2}):(\d{2}):(\d{2})/);
            if (m) seed = `${m[1]}:${m[2]}:${m[3]}`;
        }
        initTimeWheel(seed);
    } else {
        closeTimeWheel();
    }
}

function cancelFocusSelection() {
    if (focusDraftSnapshot) {
        initTimeWheel(focusDraftSnapshot.time || '');
        document.getElementById('focusWindowInput').value = focusDraftSnapshot.windowMin || 5;
    }
    focusDraftSnapshot = null;
    toggleFocusPopover(false);
}

document.addEventListener('click', function (e) {
    if (e.target.closest && !e.target.closest('.focus-wrap')) cancelFocusSelection();
});

// ---- Custom 24-hour time picker -------------------------------------------
// Replaces <input type="time">. Two reasons: the native control's dropdown is
// an unstyleable OS-rendered grid (see the screenshot that prompted this),
// and it silently displays in the browser locale's 12-hour AM/PM format —
// driver-log timestamps are always 24-hour, so a picker showing "01:44:37 PM"
// invites exactly the off-by-12-hours mistake this feature exists to prevent.
// Three scrollable columns instead; #focusTimeInput stays as the single
// source of truth (a hidden "HH:MM:SS" string) so focusLogTime() and the
// seed-from-log-line logic above don't need to know a custom widget exists.
const CTP_COLS = { h: { id: 'ctpHour', max: 24 }, m: { id: 'ctpMinute', max: 60 }, s: { id: 'ctpSecond', max: 60 } };
let ctpBuilt = false;

function buildTimeWheelColumns() {
    if (ctpBuilt) return;
    Object.keys(CTP_COLS).forEach((unit) => {
        const col = document.getElementById(CTP_COLS[unit].id);
        if (!col) return;
        let html = '';
        for (let i = 0; i < CTP_COLS[unit].max; i++) {
            const v = String(i).padStart(2, '0');
            html += `<div class="ctp-opt" data-val="${v}">${v}</div>`;
        }
        col.innerHTML = html;
        col.dataset.unit = unit;
    });
    document.getElementById('timeWheelPanel').addEventListener('click', function (e) {
        const opt = e.target.closest('.ctp-opt');
        if (!opt) return;
        selectWheelValue(opt.parentElement.dataset.unit, opt.dataset.val);
    });
    ctpBuilt = true;
}

function selectWheelValue(unit, val) {
    const col = document.getElementById(CTP_COLS[unit].id);
    col.querySelectorAll('.ctp-opt').forEach((o) => o.classList.toggle('is-selected', o.dataset.val === val));
    const parts = (document.getElementById('focusTimeInput').value || '00:00:00').split(':');
    parts[unit === 'h' ? 0 : unit === 'm' ? 1 : 2] = val;
    const full = parts.join(':');
    document.getElementById('focusTimeInput').value = full;
    document.getElementById('focusTimeText').textContent = full;
}

function scrollWheelToSelected(instant) {
    Object.keys(CTP_COLS).forEach((unit) => {
        const col = document.getElementById(CTP_COLS[unit].id);
        const sel = col.querySelector('.is-selected');
        if (sel) sel.scrollIntoView({ block: 'center', behavior: instant ? 'auto' : 'smooth' });
    });
}

// `value` is "HH:MM:SS" or "" — populates the display + hidden field + column
// highlighting. Does not open the dropdown; that's toggleTimeWheel's job.
function initTimeWheel(value) {
    buildTimeWheelColumns();
    document.getElementById('focusTimeInput').value = value || '';
    document.getElementById('focusTimeText').textContent = value || '--:--:--';
    if (value) {
        const [h, m, s] = value.split(':');
        selectWheelValue('h', h); selectWheelValue('m', m); selectWheelValue('s', s);
    }
}

function toggleTimeWheel(e) {
    if (e) e.stopPropagation();
    buildTimeWheelColumns();
    const panel = document.getElementById('timeWheelPanel');
    const open = panel.style.display === 'none';
    panel.style.display = open ? 'block' : 'none';
    document.getElementById('ctpTrigger').classList.toggle('is-open', open);
    if (open) requestAnimationFrame(() => scrollWheelToSelected(true));
}

function closeTimeWheel() {
    const panel = document.getElementById('timeWheelPanel');
    if (panel) panel.style.display = 'none';
    const trigger = document.getElementById('ctpTrigger');
    if (trigger) trigger.classList.remove('is-open');
}

// The "Use visible line's time" shortcut inside the picker — re-seeds without
// closing the popover, for re-centering after scrolling the log.
function seedTimeWheelFromLog() {
    const first = document.querySelector('#previewBox .tat-log-row:not(.tat-log-row-loading) .tat-log-text');
    const m = first && first.textContent.match(/(\d{2}):(\d{2}):(\d{2})/);
    if (!m) return;
    initTimeWheel(`${m[1]}:${m[2]}:${m[3]}`);
    scrollWheelToSelected(false);
}
document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    const guard = document.getElementById('baselineGuardModal');
    if (guard && guard.style.display !== 'none') {
        closeBaselineGuard();
        return;
    }
    cancelFocusSelection();
});

function focusLogTime() {
    const raw = document.getElementById('focusTimeInput').value.trim();
    if (!raw) return;
    const win = parseInt(document.getElementById('focusWindowInput').value, 10);
    fetch(LV.url.log_viewer_set_focus, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({time: raw, window_min: (win > 0 ? win : 5)}),
    })
        .then(r => r.json())
        .then(d => {
            if (!d.success) { alert(d.message); return; }
            focusDraftSnapshot = null;
            toggleFocusPopover(false);
            setFocusUiState({center: d.focus_center, window_min: d.focus_window_min});
            // Re-run whatever view is currently active so the narrowing takes
            // effect immediately instead of waiting for the next edit.
            if (filterData.some(f => f.enabled && !f.excluding)) {
                applyFilter();
            } else {
                document.getElementById('logPaneTitle').textContent = 'Log';
                lastMatchCount = d.view_total;
                document.getElementById('matchCount').textContent = d.view_total;
                document.getElementById('totalLines').textContent = d.total_lines;
                renderLogRows(d.preview, null, d.view_total);
            }
        });
}

function clearFocus() {
    fetch(LV.url.log_viewer_clear_focus, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (!d.success) return;
            setFocusUiState(null);
            if (filterData.some(f => f.enabled && !f.excluding)) applyFilter();
        });
}

// Single source of truth for the Focus badge + clear button visibility, so
// every caller (focusLogTime/clearFocus/showAllLog/applyFilter) stays in sync
// with the server's actual state instead of each re-deriving it independently.
function setFocusUiState(focus) {
    const badge = document.getElementById('focusBadge');
    const clearBtn = document.getElementById('clearFocusBtn');
    if (!focus || !focus.center) {
        badge.style.display = 'none';
        badge.textContent = '';
        clearBtn.style.display = 'none';
        document.getElementById('focusBtn').classList.remove('is-active');
        return;
    }
    const timePart = focus.center.split('-').pop().split('.')[0]; // HH:MM:SS, no ms
    badge.textContent = `🎯 ±${focus.window_min || 5}m @ ${timePart}`;
    badge.title = `Only ±${focus.window_min || 5} minutes around ${focus.center.split('-').pop()} is being scanned. Click to clear.`;
    badge.style.display = '';
    clearBtn.style.display = '';
    document.getElementById('focusBtn').classList.add('is-active');
}

// ── System Event Log panel (collapsible, above the log rows) ──────────────
// Shown for any capture that HAS one beside it (WiFi or BT — see
// updateEvtSection). Auto-discovered next to the log or picked manually; rows
// click-sync to the nearest driver-log line by timestamp, with the capture
// machine's UTC offset applied so the two are compared in one frame.
let _evtLoaded = false, _evtOpen = false, _evtData = [], _evtLoading = false;
let _evtOffset = 0, _evtHasMore = false, _evtTotal = 0;
// True once per freshly-(re)loaded event log, until its first page comes
// back — tells the next fetch to ask the server to pick the Source dropdown
// value from what the capture actually contains (see detect_default_source_
// filter) instead of leaving it on whatever the dropdown was last set to.
let _evtAutoPending = true;
const _EVT_PAGE = 300;

// Show/hide + enable the event section based on domain + availability.
// `path` (optional) syncs the evtPathInput readout — omitted on calls that
// don't have a fresh path to report (the badge/enable-state still updates).
// Shown for BOTH domains now. What decides whether the panel is useful is
// whether a System event export was actually found beside the capture, not
// whether the capture is WiFi or BT — a WiFi capture that ships one used to
// have no way to open it. `available` is that answer; when a log is loaded and
// nothing was found the section still shows, so the manual pick button is
// reachable, and the toggle stays disabled until there is something to open.
function updateEvtSection(domain, available, path) {
    const section = document.getElementById('evtSection');
    const hasLog = !!document.getElementById('logPathInput').value.trim();
    section.style.display = hasLog ? 'block' : 'none';
    if (!hasLog) { _closeEvtPanel(); return; }
    const btn = document.getElementById('evtToggleBtn');
    btn.disabled = !available;
    if (path !== undefined) document.getElementById('evtPathInput').value = path || '';
    // Collapsed once there IS a file; expanded (so the browse button is right
    // there) when auto-discovery found nothing and the engineer has to pick.
    setEvtPathCollapsed(!!document.getElementById('evtPathInput').value.trim());
    updateEvtTzBadge();
    // A freshly (re)loaded log invalidates any cached events.
    _evtLoaded = false; _evtData = []; _evtOffset = 0; _evtAutoPending = true;
    if (_evtOpen && !available) _closeEvtPanel();
}

function _utcOffsetLabel(offsetMin) {
    if (typeof offsetMin !== 'number') return 'unknown';
    const sign = offsetMin >= 0 ? '+' : '-';
    const abs = Math.abs(offsetMin);
    return `UTC${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`;
}

function _eventFrameLabel() {
    if (eventSyncBasis === 'engineer_local') return 'engineer local';
    if (eventSyncBasis === 'customer') return 'customer local';
    return 'local';
}

// Make both the raw Event XML basis and the chosen text-log frame explicit.
function updateEvtTzBadge() {
    const badge = document.getElementById('evtTzBadge');
    if (!badge) return;
    if (typeof eventSyncOffsetMin === 'number') {
        const offset = _utcOffsetLabel(eventSyncOffsetMin);
        const frame = _eventFrameLabel();
        badge.textContent = `🌐 ${frame} · ${offset}`;
        if (eventSyncBasis === 'engineer_local') {
            const customer = _utcOffsetLabel(customerUtcOffsetMin);
            badge.title =
                `Raw Event XML time is UTC. WiFi text-log time is generated in the analysing `
                + `engineer's local timezone, so events are converted to ${frame} (${offset}) `
                + `before syncing. Customer System Info reports ${customer}; that offset is shown `
                + `for reference but is not used to align this WiFi log.`;
        } else {
            badge.title =
                `Raw Event XML time is UTC. BT HCI text-log time is in the customer's timezone, `
                + `so events are converted to ${frame} (${offset}) using System Info before syncing.`;
        }
        badge.classList.add('evt-tz-ok');
        badge.classList.remove('evt-tz');
    } else {
        badge.textContent = '⚠ customer timezone unknown';
        badge.title = 'Raw Event XML time is UTC. This BT text log is customer-local, but no usable timezone was found in System Info, so event-to-log time sync is disabled.';
        badge.classList.add('evt-tz');
        badge.classList.remove('evt-tz-ok');
    }
}

function _closeEvtPanel() {
    _evtOpen = false;
    document.getElementById('evtPanel').style.display = 'none';
    const arrow = document.getElementById('evtArrow');
    if (arrow) arrow.classList.remove('open');
}

function pickEventLog() {
    fetch(LV.url.log_viewer_pick_event_log, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (!d.success) { if (d.message !== 'No file selected') alert(d.message); return; }
            const btn = document.getElementById('evtToggleBtn');
            btn.disabled = false;
            document.getElementById('evtPathInput').value = d.event_log_path || '';
            eventSyncOffsetMin = d.event_sync_offset_min;
            eventSyncBasis = d.event_sync_basis || '';
            customerUtcOffsetMin = d.customer_utc_offset_min;
            updateEvtTzBadge();
            _evtLoaded = false; _evtData = []; _evtOffset = 0; _evtAutoPending = true;
            if (!_evtOpen) toggleEvtPanel(); else _evtResetAndFetch();
        });
}

function toggleEvtPanel() {
    const btn = document.getElementById('evtToggleBtn');
    if (btn.disabled) return;
    _evtOpen = !_evtOpen;
    document.getElementById('evtPanel').style.display = _evtOpen ? 'block' : 'none';
    document.getElementById('evtArrow').classList.toggle('open', _evtOpen);
    if (_evtOpen && !_evtLoaded) { _evtLoaded = true; _evtResetAndFetch(); }
}

function evtFilterChanged() { _evtResetAndFetch(); }

function _evtResetAndFetch() {
    _evtData = []; _evtOffset = 0; _evtHasMore = false;
    document.getElementById('evtBody').innerHTML =
        '<p class="evt-msg">Loading…</p>';
    _evtFetchPage();
}

function _evtFetchPage() {
    if (_evtLoading) return;
    _evtLoading = true;
    // Only the first page of a freshly-(re)loaded log asks the server to pick
    // the Source value — a manual dropdown change (evtFilterChanged) never
    // sets _evtAutoPending, so it's never overridden once the user has chosen.
    const useAuto = _evtAutoPending && _evtOffset === 0;
    fetch(LV.url.log_viewer_parse_event_log, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            offset: _evtOffset, limit: _EVT_PAGE,
            source_filter: document.getElementById('evtSourceFilter').value,
            level_filter: document.getElementById('evtLevelFilter').value,
            auto_source: useAuto,
        }),
    })
        .then(r => r.json())
        .then(d => {
            if (d.error) { document.getElementById('evtBody').innerHTML = `<p class="evt-msg evt-msg-err">${escapeHtml(d.error)}</p>`; return; }
            if (useAuto) {
                _evtAutoPending = false;
                if (d.applied_source_filter) document.getElementById('evtSourceFilter').value = d.applied_source_filter;
            }
            _evtTotal = d.total || 0;
            _evtHasMore = d.has_more || false;
            _evtOffset += (d.events || []).length;
            _evtData = _evtData.concat(d.events || []);
            _evtRenderTable(d.time_header || 'Time (UTC)');
        })
        .catch(e => { document.getElementById('evtBody').innerHTML = `<p class="evt-msg evt-msg-err">Network error: ${escapeHtml(e.message)}</p>`; })
        .finally(() => { _evtLoading = false; });
}

// The capture-local-equivalent of an event's raw UTC time — same shift
// jumpLogToMs/jumpEvtToMs already use to compare the two timestamp sources,
// just formatted for a human to eyeball next to the driver log's own
// MM/DD/YYYY-HH:MM:SS.mmm rows instead of having to do the offset math
// themselves to tell whether a sync landed somewhere reasonable.
function _evtLocalTimeLabel(text) {
    if (typeof eventSyncOffsetMin !== 'number') return '';
    const ms = parseEvtTimeMs(text);
    if (ms == null) return '';
    const d = new Date(ms);
    const pad = n => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}/${pad(d.getDate())}-${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _evtRenderTable(timeHeader) {
    const body = document.getElementById('evtBody');
    if (!_evtData.length) { body.innerHTML = '<p class="evt-msg">No matching events.</p>'; document.getElementById('evtCount').textContent = ''; return; }
    let html = `<table class="evt-table"><colgroup>
        <col style="width:212px;"><col style="width:26px;"><col style="width:90px;"><col style="width:48px;">
      </colgroup><thead><tr>
        <th>${escapeHtml(timeHeader)}</th><th>Lv</th><th>Source</th><th>ID</th>
      </tr></thead><tbody>`;
    for (let i = 0; i < _evtData.length; i++) {
        const ev = _evtData[i];
        const dot = (ev.level === 'Error' || ev.level === 'Critical') ? '🔴'
            : ev.level === 'Warning' ? '🟡' : '🟢';
        const localLabel = _evtLocalTimeLabel(ev.time);
        html += `<tr data-evt-idx="${i}">
            <td>${escapeHtml(ev.time)}${localLabel ? `<span class="evt-time-local" title="${escapeHtml(localLabel)} (${escapeHtml(_eventFrameLabel())})">·${escapeHtml(localLabel)}</span>` : ''}</td>
            <td style="text-align:center;">${dot}</td>
            <td>${escapeHtml(ev.source)}</td>
            <td>${escapeHtml(ev.event_id)}</td>
          </tr>`;
    }
    html += '</tbody></table>';
    body.innerHTML = html;
    document.getElementById('evtCount').textContent = `${_evtData.length}/${_evtTotal}`;

    body.onscroll = function () {
        if (!_evtHasMore || _evtLoading) return;
        if (body.scrollTop + body.clientHeight >= body.scrollHeight * 0.7) _evtFetchPage();
    };
    body.querySelectorAll('tr[data-evt-idx]').forEach(function (tr) {
        tr.addEventListener('click', _onEvtRowClick);
        tr.addEventListener('mouseenter', _onEvtRowEnter);
        tr.addEventListener('mouseleave', _onEvtRowLeave);
    });
}

// Event row clicked → scroll the driver log to the nearest line by time.
function _onEvtRowClick(e) {
    const ev = _evtData[+e.currentTarget.dataset.evtIdx];
    if (!ev) return;
    const ms = parseEvtTimeMs(ev.time);
    if (ms == null) return;
    document.querySelectorAll('.evt-table tr.is-active').forEach(t => t.classList.remove('is-active'));
    e.currentTarget.classList.add('is-active');
    jumpLogToMs(ms);
}

// Find the driver-log row whose timestamp is closest to `ms`, scroll it into
// view within the log pane, and flash it. Resolved server-side: the pane only
// renders the rows on screen, so the nearest row is usually not in the DOM.
function jumpLogToMs(ms) {
    fetch(LV.url.log_viewer_nearest_row, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ms: ms}),
    })
        .then(r => r.json())
        .then(d => { if (d && d.success) scrollLogToIndex(d.index); })
        .catch(() => {});
}

function _onEvtRowEnter(e) {
    const ev = _evtData[+e.currentTarget.dataset.evtIdx];
    if (!ev) return;
    const card = document.getElementById('evtHoverCard');
    const lvColor = (ev.level === 'Error' || ev.level === 'Critical') ? '#dc2626'
        : ev.level === 'Warning' ? '#ca8a04' : '#16a34a';
    const localLabel = _evtLocalTimeLabel(ev.time);
    let h = '<table>';
    h += `<tr><th>Time</th><td>${escapeHtml(ev.time)} (UTC)</td></tr>`;
    if (localLabel) h += `<tr><th>Aligned</th><td>${escapeHtml(localLabel)} — ${escapeHtml(_eventFrameLabel())} / text-log frame</td></tr>`;
    h += `<tr><th>Level</th><td style="color:${lvColor};font-weight:600;">${escapeHtml(ev.level)}</td></tr>`;
    h += `<tr><th>Source</th><td>${escapeHtml(ev.source)}</td></tr>`;
    h += `<tr><th>ID</th><td>${escapeHtml(ev.event_id)}</td></tr>`;
    if (ev.message) h += `<tr><th>Message</th><td>${escapeHtml(ev.message)}</td></tr>`;
    h += '</table>';
    card.innerHTML = h;
    const rect = e.currentTarget.getBoundingClientRect();
    card.style.left = Math.min(rect.right + 8, window.innerWidth - 380) + 'px';
    let top = rect.top;
    if (top + 220 > window.innerHeight - 20) top = window.innerHeight - 240;
    if (top < 60) top = 60;
    card.style.top = top + 'px';
    card.style.display = 'block';
}

function _onEvtRowLeave() { document.getElementById('evtHoverCard').style.display = 'none'; }

// Reverse sync: given a driver-log line's time (ms), highlight + scroll to the
// nearest event row in the panel. No-op if events aren't loaded yet.
function jumpEvtToMs(ms) {
    if (!_evtData.length) return;
    let bestIdx = -1, bestDiff = Infinity;
    for (let i = 0; i < _evtData.length; i++) {
        const evMs = parseEvtTimeMs(_evtData[i].time);
        if (evMs == null) continue;
        const diff = Math.abs(evMs - ms);
        if (diff < bestDiff) { bestDiff = diff; bestIdx = i; }
    }
    if (bestIdx < 0) return;
    const body = document.getElementById('evtBody');
    const tr = body.querySelector(`tr[data-evt-idx="${bestIdx}"]`);
    if (!tr) return;
    body.querySelectorAll('.evt-table tr.is-active').forEach(t => t.classList.remove('is-active'));
    tr.classList.add('is-active');
    body.scrollTop = tr.offsetTop - body.clientHeight / 2 + tr.offsetHeight / 2;
}

function pickTat() {
    fetch(LV.url.log_viewer_pick_tat, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                baselineDone = false;
                document.getElementById('tatPathInput').value = d.tat_path;
                showPathStatus('tatStatus', '✔ Filter loaded', 'ok');
                filterData = d.filters;
                renderFilters();
                syncOps(d);
                // Baseline AFTER the filter run — it reads the resulting
                // stats, which don't exist until applyFilter has populated
                // them server-side.
                if (document.getElementById('logPathInput').value.trim()) applyFilter().then(() => setBaselineGate());
            } else showPathStatus('tatStatus', d.message, 'err');
        });
}

// The export baseline is only worth showing when it DIFFERS from the skill
// whose keywords are on screen — i.e. it was picked in the Skill Library,
// which sets the baseline without touching the filters. When the two agree
// (the normal case: a skill loaded right here), the header picker already
// says so and a second indicator would just be noise.
function renderExportBaselineBadge() {
    const badge = document.getElementById('exportBaselineBadge');
    if (!badge) return;
    if (!activeSkillKey || activeSkillKey === filterSkillKey) {
        badge.style.display = 'none';
        return;
    }
    document.getElementById('exportBaselineName').textContent = activeSkillName || activeSkillKey;
    badge.style.display = 'flex';
}

// Drop the inheritance without touching the filters — the mirror image of
// "Load as baseline". Before this there was no way back: the Skill Library
// could only ever SET a baseline.
function clearExportBaseline() {
    if (isBusy()) return;
    setBusy(true);
    fetch(LV.url.skills_clear_baseline, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            setBusy(false);
            if (!d.success) { alert(d.message || 'Could not clear the baseline'); return; }
            activeSkillKey = '';
            activeSkillName = '';
            renderExportBaselineBadge();
        })
        .catch(e => { setBusy(false); alert('Failed: ' + e); });
}

// Only ever fires from a click in the header's 🎓 picker — and that toggle is
// itself marked data-busy-lock, so it's physically un-clickable while anything
// else is in flight. The isBusy() guard here is defense in depth against a
// click that somehow lands anyway (e.g. a keyboard-driven activation racing a
// fetch); either way the skill choice can only ever change from a deliberate,
// isolated user action, never mid-flight.
function loadSkill(key) {
    if (!key || isBusy()) return;
    toggleSkillLoadPicker(false);
    setBusy(true);
    fetch(LV.url.log_viewer_load_skill, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({skill_key: key})
    }).then(r => r.json()).then(d => {
        setBusy(false);
        if (d.success) {
            baselineDone = false;
            document.getElementById('tatPathInput').value = d.tat_path;
            document.getElementById('tatPathInput').placeholder = d.tat_path ? '' : '(no .tat file — using built-in keywords)';
            showPathStatus('tatStatus', '✔ Skill loaded', 'ok');
            filterData = d.filters;
            renderFilters();
            syncOps(d);
            // Loading here sets BOTH: the filters on screen and the export
            // baseline. They now agree, so the badge hides itself.
            const loaded = availableSkillDocs.find(s => s.key === key);
            filterSkillKey = key;
            filterSkillName = (loaded && loaded.name) || key;
            activeSkillKey = key;
            activeSkillName = filterSkillName;
            renderSkillLoadPicker();
            renderExportBaselineBadge();
            // Loading a named skill auto-switches the conversation into
            // PRIOR-knowledge mode server-side (see log_viewer_routes.
            // load_skill) — sync the document picker so the UI matches, and the
            // engineer sees why the interview stops asking about content this
            // skill already covers.
            if (d.prior_knowledge) {
                selectedSkillKeys = d.selected_skill_keys || Array.from(new Set([...selectedSkillKeys, key]));
                renderSkillDocPicker();
                updateQuestionContextStatus();
            }
            if (document.getElementById('logPathInput').value.trim()) applyFilter().then(() => setBaselineGate());
        } else showPathStatus('tatStatus', d.message, 'err');
    }).catch(() => setBusy(false));
}

function toggleFilter(idx, enabled) {
    filterData[idx].enabled = enabled;
    fetch(LV.url.log_viewer_toggle_filter, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({index: idx, enabled: enabled})
    }).then(r => r.json()).then(d => { renderFilters(); syncOps(d); debounceApplyFilter(); });
}

function selectAllFilters(val) {
    const calls = filterData.map((f, i) => {
        f.enabled = val;
        return fetch(LV.url.log_viewer_toggle_filter, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({index: i, enabled: val})
        });
    });
    renderFilters();
    Promise.all(calls).then(() => debounceApplyFilter());
}

// No manual "Run Filter" button — every mutation (add/remove/toggle/load)
// re-runs the filter itself. None of this ever touches the chat/LLM anymore;
// the baseline read fires automatically when a filter set is first loaded
// (see requestBaseline), and later edits are compared against it deterministically.
function addFilter(excluding) {
    const input = document.getElementById('newFilterText');
    const text = input.value.trim();
    if (!text) return;
    addFilterText(text, excluding, () => { input.value = ''; });
}

// The shared body of "add a filter", so the typed box and the select-in-the-log
// picker go through exactly one path — same journal entry, same skill-memory
// record, same auto-rerun.
function addFilterText(text, excluding, onAdded) {
    text = (text || '').trim();
    if (!text) return;
    fetch(LV.url.log_viewer_add_filter, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text, excluding: excluding})
    }).then(r => r.json()).then(d => {
        if (!d.success) { alert(d.message); return; }
        filterData = d.filters;
        if (onAdded) onAdded();
        renderFilters();
        syncOps(d);
        if (document.getElementById('logPathInput').value.trim()) applyFilter();
    });
}

function removeFilter(idx) {
    fetch(LV.url.log_viewer_remove_filter, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({index: idx})
    }).then(r => r.json()).then(d => {
        if (!d.success) { alert(d.message); return; }
        filterData = d.filters;
        renderFilters();
        syncOps(d);
        if (document.getElementById('logPathInput').value.trim()) applyFilter();
    });
}

function applyFilter() {
    const box = document.getElementById('previewBox');
    // Keep the previous rows on screen, dimmed, instead of replacing them with
    // "Running filter…". Blanking made every toggle flash the whole log away
    // and back, which reads as much slower than it is — and it threw away the
    // engineer's scroll position on a result that is usually nearly the same.
    // Only an empty pane (very first run) gets the placeholder.
    if (box.firstElementChild) box.classList.add('is-refiltering');
    else box.innerHTML = '<div class="tat-log-empty">Running filter…</div>';
    const done = () => box.classList.remove('is-refiltering');
    return fetch(LV.url.log_viewer_apply_filter, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            done();
            if (!d.success) { box.innerHTML = ''; alert(d.message); return; }
            document.getElementById('logPaneTitle').textContent = 'Filtered Log';
            lastMatchCount = d.total_matched;
            document.getElementById('matchCount').textContent = d.total_matched;
            document.getElementById('totalLines').textContent = d.total_lines;
            const stats = document.getElementById('statsSummary');
            // The scanned count is already the "of N" in the pane title above,
            // so an active window only needs to add the whole file's size.
            stats.textContent = `${d.overlap_count} lines matched 2+ keywords`
                + (d.focus ? ` · full file: ${Number(d.full_total_lines).toLocaleString()} lines` : '');
            stats.title = `${d.overlap_count} lines matched 2 or more include keywords`
                + (d.focus ? ` — scanned ${d.total_lines} of ${d.full_total_lines} lines in file` : '');
            setFocusUiState(d.focus);
            contextLineCount = Number(d.context_count || d.preview_count || 0);
            contextTimeSpan = d.time_span || {};
            updateQuestionContextStatus();

            // Merge hit counts into the filter table (that IS the stats display now).
            // Index-based, not text-based: per_filter is now returned for
            // EVERY filter position (including disabled ones) in the same
            // order as filterData, so this also survives duplicate-text
            // filters that a text-keyed lookup would silently collide on.
            d.per_filter.forEach((f, i) => { if (filterData[i]) filterData[i].hits = f.hits; });
            renderFilters();
            // Set BEFORE syncOps: syncOps re-renders the Steps panel, which
            // reads divergenceData to decide which 🎓 icons to hint on.
            divergenceData = d.divergence || {};
            syncOps(d); // effects for prior edits are now measured

            annotationData = d.annotations || annotationData;
            renderLogRows(d.preview, 'No lines matched', d.view_total);
            // Last, so a clarifying question never delays the filter result
            // the engineer is actually waiting to look at.
            maybeClarify();
        })
        .catch(err => { done(); throw err; });  // never leave the pane dimmed
}

// Coalesce rapid successive toggles into one re-run — but LEADING edge, not
// trailing. A plain trailing debounce charged every single checkbox click a
// flat 350ms of doing nothing before the request even left, which is most of
// what made toggling feel slow: the isolated click is the common case, and it
// was paying the price of a burst that usually never came. So the first
// toggle fires immediately, and anything arriving while that run is in flight
// (or within a short window after) collapses into exactly ONE follow-up run.
let _filterCooldownTimer = null;
let _filterRunInFlight = false;
let _filterRerunQueued = false;

function debounceApplyFilter() {
    if (!document.getElementById('logPathInput').value.trim()) return; // nothing to filter yet
    if (_filterRunInFlight || _filterCooldownTimer) { _filterRerunQueued = true; return; }
    _runFilterCoalesced();
}

function _runFilterCoalesced() {
    _filterRunInFlight = true;
    _filterRerunQueued = false;
    applyFilter().catch(() => {}).then(() => {
        _filterRunInFlight = false;
        _filterCooldownTimer = setTimeout(() => {
            _filterCooldownTimer = null;
            if (_filterRerunQueued) _runFilterCoalesced();
        }, 200);
    });
}

// ---- Chat (reuses the same session-state filtered preview as system context) ----

// Small, SAFE markdown-ish renderer for the LLM's replies (which are full of
// #/##/### headers, **bold**, `code`, and - bullet lists that were
// previously dumped as literal text via textContent — that raw-symbol soup
// was the "排版太亂" complaint). Escapes first (so any stray <, >, & can
// never become real markup), then only ever introduces a small fixed set of
// safe tags via regex — no HTML from the model is ever trusted directly.
// A GitHub-style pipe table: a "| a | b |" header row immediately followed by
// a "|---|---|" separator row. Without this, the LLM's markdown tables (it
// writes them often — comparison tables, keyword/skill mappings) fell through
// to the plain-line branch as literal "| a | b |" text, which is both ugly
// and, for long rows, the source of the horizontal overflow that broke the
// page layout (a long unbroken pipe-delimited line with no wrap points).
const _TABLE_ROW_RE = /^\|.*\|$/;
const _TABLE_SEP_RE = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$/;
const _splitTableRow = row => row.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());

function renderMarkdownLite(raw) {
    const esc = escapeHtml(raw);
    const inlineFmt = s => s
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');
    const lines = esc.split('\n');
    let html = '';
    let listMode = null; // 'ul' | 'ol' | null
    const closeList = () => { if (listMode) { html += `</${listMode}>`; listMode = null; } };
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (_TABLE_ROW_RE.test(line.trim()) && i + 1 < lines.length && _TABLE_SEP_RE.test(lines[i + 1].trim())) {
            closeList();
            const headCells = _splitTableRow(line);
            html += '<div class="chat-md-table-wrap"><table class="chat-md-table"><thead><tr>';
            headCells.forEach(c => { html += `<th>${inlineFmt(c)}</th>`; });
            html += '</tr></thead><tbody>';
            i += 2; // consume header + separator
            while (i < lines.length && _TABLE_ROW_RE.test(lines[i].trim())) {
                html += '<tr>';
                _splitTableRow(lines[i]).forEach(c => { html += `<td>${inlineFmt(c)}</td>`; });
                html += '</tr>';
                i++;
            }
            html += '</tbody></table></div>';
            i--; // outer loop's i++ accounts for the row this while-loop stopped on
            continue;
        }
        const h3 = line.match(/^### (.*)/);
        const h2 = line.match(/^## (.*)/);
        const h1 = line.match(/^# (.*)/);
        const uli = line.match(/^[-*] (.*)/);
        const oli = line.match(/^\d+\. (.*)/);
        if (uli) {
            if (listMode !== 'ul') { closeList(); html += '<ul class="chat-md-list">'; listMode = 'ul'; }
            html += `<li>${inlineFmt(uli[1])}</li>`;
            continue;
        }
        if (oli) {
            if (listMode !== 'ol') { closeList(); html += '<ol class="chat-md-list">'; listMode = 'ol'; }
            html += `<li>${inlineFmt(oli[1])}</li>`;
            continue;
        }
        closeList();
        if (h3) { html += `<div class="chat-md-h3">${inlineFmt(h3[1])}</div>`; }
        else if (h2) { html += `<div class="chat-md-h2">${inlineFmt(h2[1])}</div>`; }
        else if (h1) { html += `<div class="chat-md-h1">${inlineFmt(h1[1])}</div>`; }
        else if (line.trim() === '---') { html += '<hr class="chat-md-hr">'; }
        else if (line.trim() === '') { html += '<div class="chat-md-gap"></div>'; }
        else { html += `<div>${inlineFmt(line)}</div>`; }
    }
    closeList();
    return html;
}

// Mirrors chat_history 1:1 as messages are appended (both live and on the
// initial page-load replay) — the step viewer needs this to slice "what was
// said between filter edit A and edit B" since chatBox itself only holds DOM
// nodes, not queryable message data.
let chatHistoryMirror = [];

// Matches confirm_step's exact persisted format (see stepKnowledgeCoreText
// and learning_routes.py confirm_step) — used by renderStepKnowledgeBubble so
// this content gets nicer structured styling instead of plain markdown
// prose, on BOTH the live append and the page-reload replay path (both go
// through appendMsg, so one regex covers both).
const _STEP_KNOWLEDGE_RE = /^\*\*Step #(\d+) — knowledge core:\*\* ([\s\S]*)/;

// Renders a confirm_step message (knowledge core + optional expert note +
// optional follow-up) as labeled, visually distinct blocks instead of one
// run-on markdown paragraph — same visual language as the old interactive
// step-confirm-card (see .step-confirm-core/.step-confirm-note), just
// permanent and non-interactive. Returns false (and appends nothing) for any
// other message shape, so the caller falls back to normal markdown.
function renderStepKnowledgeBubble(contentEl, content) {
    const m = content.match(_STEP_KNOWLEDGE_RE);
    if (!m) return false;
    const seq = +m[1];      // number, so it matches operationData's own seq in stepLabel
    const parts = m[2].split('\n\n');
    const core = parts[0];
    let expertNote = '', followUp = '';
    for (let i = 1; i < parts.length; i++) {
        const noteM = parts[i].match(/^\*Expert note:\* ([\s\S]*)/);
        if (noteM) { expertNote = noteM[1]; continue; }
        const fuM = parts[i].match(/^\*\(optional follow-up: ([\s\S]*)\)\*$/);
        if (fuM) { followUp = fuM[1]; }
    }
    const head = el('div', 'step-knowledge-head');
    head.innerHTML = `<i class="fas fa-brain"></i> Step ${stepLabel(seq)} knowledge core`;
    contentEl.appendChild(head);
    contentEl.appendChild(el('div', 'step-confirm-core', core));
    if (expertNote) {
        const note = el('div', 'step-confirm-note');
        note.appendChild(el('span', 'step-confirm-note-label', '💭 Expert note: '));
        note.appendChild(document.createTextNode(expertNote));
        contentEl.appendChild(note);
    }
    if (followUp) {
        const fu = el('div', 'step-knowledge-followup');
        fu.appendChild(el('span', 'step-confirm-note-label', '❓ '));
        fu.appendChild(document.createTextNode(followUp));
        contentEl.appendChild(fu);
    }
    return true;
}

// role/content as before, plus stepTag: the step number this message is
// about, or "all" for general/session-wide knowledge, or omitted/null for
// legacy untagged messages (no badge shown). User bubbles sit right-aligned
// in a primary-tinted bubble; assistant bubbles sit left-aligned, white with
// a border — the two-color/side split makes "who said this" unmistakable at
// a glance instead of relying on a small "You:"/"AI:" text prefix alone.
// Park the conversation at the bottom so whatever was just added is the thing
// you're looking at. Setting scrollTop once, synchronously, is not enough: a
// question card's final height isn't known at append time (the markdown body,
// the RECOMMENDED block and the option rows all lay out after), so the scroll
// landed short and the new card opened part-way up the pane with its answer
// controls below the fold — which reads as "the new question went above the
// old one". Re-running it on the next frame, once layout has settled, puts it
// where it belongs.
// Both follow-ups are deliberate, not redundant: requestAnimationFrame lands
// right after layout on a VISIBLE tab (no flicker), but it does not fire at
// all while the tab is hidden or backgrounded — which is exactly when a slow
// answer arrives and the engineer comes back to a half-scrolled pane. The
// timeout is the fallback that runs either way.
function scrollChatToBottom() {
    const box = document.getElementById('chatBox');
    if (!box) return;
    const toBottom = () => { box.scrollTop = box.scrollHeight; };
    toBottom();
    requestAnimationFrame(toBottom);
    setTimeout(toBottom, 0);
}

function appendMsg(role, content, stepTag) {
    const box = document.getElementById('chatBox');
    const isUser = (role === 'user');
    const row = el('div', 'chat-row ' + (isUser ? 'chat-row-user' : 'chat-row-assistant'));
    const bubble = el('div', 'chat-bubble ' + (isUser ? 'chat-bubble-user' : 'chat-bubble-assistant'));

    const tag = (stepTag === undefined) ? null : stepTag;
    if (tag !== null) {
        const meta = el('div', 'chat-bubble-meta');
        const isAll = (tag === 'all');
        const badge = el('span', 'chat-step-badge' + (isAll ? ' is-all' : ''), isAll ? 'All' : stepLabel(tag));
        badge.title = isAll ? 'General / session-wide knowledge' : `About step ${stepLabel(tag)}`;
        meta.appendChild(badge);
        bubble.appendChild(meta);
    }

    const contentEl = el('div', 'chat-bubble-content' + (isUser ? '' : ' chat-md'));
    if (isUser) {
        contentEl.textContent = content;
    } else if (renderStepKnowledgeBubble(contentEl, content)) {
        bubble.classList.add('step-knowledge-msg');
    } else {
        if (content.startsWith('# Baseline analysis')) {
            bubble.classList.add('baseline-analysis-msg');
        }
        contentEl.innerHTML = renderMarkdownLite(content);
    }
    bubble.appendChild(contentEl);
    row.appendChild(bubble);
    box.appendChild(row);
    scrollChatToBottom();
    chatHistoryMirror.push({role, content, step: tag});
    return row;
}

// presetMsg: sent to the LLM. displayMsg: optional short label shown in the
// bubble instead of a long preset instruction (keeps the auto-kickoff tidy).
// Small, always-visible session token counter (see services/llm_service.py
// LLM_helper._record_usage — same numbers also print to the terminal on
// every call). Session-cumulative, not per-call, since that's the more
// useful "is this getting expensive" signal while iterating on a skill.
// Last session usage {prompt_tokens, completion_tokens, total_tokens, calls},
// including the SERVER-CALCULATED estimate. The browser deliberately owns no
// pricing table or cost arithmetic; see services/pricing_service.py.
let lastSessionUsage = null;

function updateTokenBadge(sessionUsage) {
    if (!sessionUsage) return;
    lastSessionUsage = sessionUsage;
    const badge = document.getElementById('tokenBadge');
    const cost = readBackendCost(sessionUsage);
    badge.textContent = cost.available ? `🪙 Est. $${cost.total.toFixed(4)}` : '🪙 Cost n/a';
    badge.title = `${sessionUsage.total_tokens} measured tokens across ${sessionUsage.calls} LLM call(s) this session — click for the breakdown`;
    // Keep the spending popover live if it's currently open.
    const sp = document.getElementById('spendPanel');
    if (sp && sp.style.display !== 'none') renderSpendPanel();
}

// tiny token formatter (1234 -> "1,234")
function fmtTok(n) { return (n || 0).toLocaleString('en-US'); }

// Read the backend result without reproducing its rates or arithmetic here.
function readBackendCost(u) {
    const inTok = (u && u.prompt_tokens) || 0;
    const outTok = (u && u.completion_tokens) || 0;
    const breakdown = (u && u.cost_breakdown) || {};
    return {
        inTok,
        outTok,
        usdIn: Number(breakdown.input_usd || 0),
        usdOut: Number(breakdown.output_usd || 0),
        total: Number((u && u.estimated_cost_usd) || 0),
        available: !!(u && u.cost_estimate_available),
        rates: (u && u.rates_usd_per_mtok) || null,
    };
}

// One spending-popover line, laid out as a fixed-column grid so every row's
// label / formula / result line up regardless of digit count:
// Input | 12,345 tok x $3.00/1M | $0.0370
function spendLine(label, formula, usdStr, cls) {
    const row = el('div', 'spend-line' + (cls ? ' ' + cls : ''));
    row.appendChild(el('span', 'spend-line-label', label));
    row.appendChild(el('span', 'spend-line-formula', formula));
    row.appendChild(el('span', 'spend-line-usd', usdStr));
    return row;
}

// Spending popover — measured tokens plus the estimate produced by Flask.
function renderSpendPanel() {
    const p = document.getElementById('spendPanel');
    p.innerHTML = '';
    const u = lastSessionUsage || {prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, calls: 0};
    const {inTok, outTok, usdIn, usdOut, total, available, rates} = readBackendCost(u);
    const totTok = u.total_tokens || (inTok + outTok);

    p.appendChild(el('div', 'readiness-panel-title', 'Spending'));
    const table = el('div', 'spend-table');
    const inFormula = rates ? `${fmtTok(inTok)} tok x $${Number(rates.input).toFixed(2)}/1M` : `${fmtTok(inTok)} measured tok`;
    const outFormula = rates ? `${fmtTok(outTok)} tok x $${Number(rates.output).toFixed(2)}/1M` : `${fmtTok(outTok)} measured tok`;
    table.appendChild(spendLine('Input', inFormula, available ? '$' + usdIn.toFixed(4) : '—'));
    table.appendChild(spendLine('Output', outFormula, available ? '$' + usdOut.toFixed(4) : '—'));
    table.appendChild(el('div', 'spend-divider'));
    table.appendChild(spendLine('Estimated', `${fmtTok(totTok)} measured tok`, available ? '$' + total.toFixed(4) : 'Unavailable', 'spend-total'));
    p.appendChild(table);
    const models = Array.isArray(u.models) && u.models.length ? u.models.join(', ') : 'model unknown';
    p.appendChild(el('div', 'spend-sub', `${u.calls || 0} LLM call(s) · ${models}`));
    p.appendChild(el('div', 'spend-sub', available
        ? `${u.rate_source || 'Configured rate'} · Estimate only, not the GNAI invoice.`
        : 'No configured rate for this model. Measured tokens are still available.'));
}

// Spending column click — open its popover (and close Readiness's so only
// one floats at a time).
function toggleSpendPanel(evt) {
    if (evt) evt.stopPropagation();
    const p = document.getElementById('spendPanel');
    const rp = document.getElementById('readinessPanel');
    if (rp) rp.style.display = 'none';
    const opening = (p.style.display === 'none' || !p.style.display);
    p.style.display = opening ? 'flex' : 'none';
    if (opening) renderSpendPanel();
}

// forceTag: overrides the step-context selector for this ONE send — used by
// clarification follow-up answers, which are always about the whole round,
// not whatever step the selector happens to be on.
let pendingBaselineSend = null;
let baselineGuardReturnFocus = null;

function showBaselineGuard(presetMsg, displayMsg, forceTag, decisionId, answeredGap) {
    pendingBaselineSend = {presetMsg, displayMsg, forceTag, decisionId, answeredGap};
    baselineGuardReturnFocus = document.activeElement;
    const modal = document.getElementById('baselineGuardModal');
    modal.style.display = 'flex';
    document.getElementById('baselineGuardCancel').focus();
}

function closeBaselineGuard(clearPending) {
    const modal = document.getElementById('baselineGuardModal');
    if (modal) modal.style.display = 'none';
    if (clearPending !== false) pendingBaselineSend = null;
    if (baselineGuardReturnFocus && baselineGuardReturnFocus.focus) baselineGuardReturnFocus.focus();
    baselineGuardReturnFocus = null;
}

function allowChatWithoutBaselineOnce() {
    const pending = pendingBaselineSend;
    if (!pending) { closeBaselineGuard(); return; }
    closeBaselineGuard(false);
    pendingBaselineSend = null;
    sendMsg(pending.presetMsg, pending.displayMsg, pending.forceTag, true, pending.decisionId,
            pending.answeredGap);
}

// The in-flight chat request's AbortController — set only while a response
// is streaming, so stopChatSend() has something to cancel and setBusy(false)
// (via .finally below) always clears it, cancelled or not.
let _activeChatAbort = null;

function stopChatSend() {
    if (_activeChatAbort) _activeChatAbort.abort();
}

// /send_stream replies as one `event: done\ndata: {...}` frame (see
// chatbot_routes.send_stream) once the model finishes; an early-validation
// failure (empty message, no baseline, LLM not configured) instead returns
// a plain JSON body with no SSE framing at all. Try the framed form first,
// fall back to parsing the whole body as JSON so both shapes work here.
function _parseStreamDone(text) {
    const marker = 'event: done\ndata: ';
    const idx = text.lastIndexOf(marker);
    if (idx >= 0) {
        try { return JSON.parse(text.slice(idx + marker.length).split('\n\n')[0]); } catch (e) { /* fall through */ }
    }
    try { return JSON.parse(text); } catch (e) { return null; }
}

// The main chat box was a fixed-height single-line <input> — long answers
// (a rule plus its reasoning, a pasted table) just scrolled sideways out of
// view instead of wrapping, the one thing every per-question answer box
// (buildAnswerBox) already solved. Grows with typed content up to a ceiling
// and scrolls internally past it — no manual resize handle to drag.
function autoGrowChatInput(input) {
    input.style.height = 'auto';
    // border-box: scrollHeight leaves the border out, so without adding it
    // back every wrapped line ends 2px short and shows a scrollbar.
    const border = input.offsetHeight - input.clientHeight;
    input.style.height = Math.min(input.scrollHeight + border, 160) + 'px';
}

// Set once wireMainChatAttachments (near the bottom of this file) runs at
// load. sendMsg reads it below to resolve any staged image right before the
// message goes out — the same deferred-to-send behaviour buildAnswerBox and
// the case-intake card use, just for the one chat input shared across turns.
let _mainChatAttachments = null;

// answeredGap: the "Still missing" item this send is the answer TO. The card
// prepends `Re: <the gap>` so the transcript (and the model) can see which one
// is being answered, and naming it here lets the server take that echo back
// out before matching the answer against the REMAINING gaps — otherwise the
// echoed log keywords close every neighbouring item about the same symbol.
function sendMsg(presetMsg, displayMsg, forceTag, allowWithoutBaseline, decisionId, answeredGap) {
    // chatSendBtn stays enabled while busy (see setBusy) so this same click
    // handler doubles as Stop — everything else with data-busy-lock is
    // disabled, so isBusy() here only ever means "the button itself was
    // clicked again," never a stray programmatic call racing a live request.
    if (isBusy()) { stopChatSend(); return; }
    // Only the "read from the visible input" path (presetMsg === undefined —
    // the engineer actually typed/attached and hit Send or Enter) can have a
    // staged image to resolve. Every other call site passes its own text
    // directly (a chosen option, a recommended answer) and never touched the
    // attach button at all.
    if (presetMsg === undefined && _mainChatAttachments && _mainChatAttachments.hasPending()) {
        const chatInput = document.getElementById('chatInput');
        const chatSendBtn = document.getElementById('chatSendBtn');
        chatInput.disabled = true;
        chatSendBtn.disabled = true;
        // No unconditional re-enable in a .finally() here: on success the
        // recursive call below immediately calls setBusy(true) itself (the
        // real request is now in flight), and unlike a question card's answer
        // box — which gets removed from the DOM the moment it submits —
        // #chatInput/#chatSendBtn are the same two persistent elements reused
        // for the whole session. Blindly re-enabling them after the recursive
        // call returns would race setBusy and leave Send clickable while a
        // response is still streaming in.
        _mainChatAttachments.resolvePending()
            .then(() => sendMsg(presetMsg, displayMsg, forceTag, allowWithoutBaseline, decisionId,
                                answeredGap))
            .catch(() => { chatInput.disabled = false; chatSendBtn.disabled = false; });
        return;
    }
    const input = document.getElementById('chatInput');
    const msg = presetMsg !== undefined ? presetMsg : input.value.trim();
    if (!msg) return;
    if (!baselineDone && !allowWithoutBaseline) {
        showBaselineGuard(presetMsg, displayMsg, forceTag, decisionId, answeredGap);
        return;
    }
    const tag = forceTag !== undefined ? forceTag : currentStepTag;
    appendMsg('user', displayMsg !== undefined ? displayMsg : msg, tag);
    if (presetMsg === undefined) {
        input.value = '';
        autoGrowChatInput(input);
        const attachStatus = document.getElementById('chatAttachStatus');
        if (attachStatus) { attachStatus.textContent = ''; attachStatus.hidden = true; }
    }
    setBusy(true);
    // From the send, not from the assess call: the reassessment only starts
    // after the reply finishes, and that whole stretch is time the engineer
    // spends reading a list that hasn't seen the answer yet.
    assessBusy(true);
    _activeChatAbort = new AbortController();
    fetch(LV.url.chatbot_send_stream, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        signal: _activeChatAbort.signal,
        body: JSON.stringify({
            message: msg,
            step_tag: tag,
            allow_without_baseline: !!allowWithoutBaseline,
            decision_id: decisionId || '',
            decision_answer: decisionId ? (displayMsg !== undefined ? displayMsg : msg) : '',
            answered_gap: answeredGap || '',
        })
    }).then(r => r.text()).then(text => {
        const d = _parseStreamDone(text);
        if (!d) { appendMsg('assistant', '⚠️ Empty or malformed response from server.', tag); return; }
        if (d.success) {
            appendMsg('assistant', d.reply, d.step_tag !== undefined ? d.step_tag : tag);
            if (d.clarification) renderProactiveClarification(d.clarification, tag);
        } else {
            // The server checks the baseline against the CURRENT signature, so
            // it can be stale while this page still thinks one is on file.
            // Put the button back in its "needs a read" state so the next
            // click is the fix, instead of leaving a message with no control.
            if (d.baseline_required) { baselineDone = false; setBaselineGate(); }
            appendMsg('assistant', '⚠️ ' + d.message, tag);
        }
        if (d.usage) updateTokenBadge(d.usage.session);
        if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
        // Every answer re-scores readiness live (only once a filter has run —
        // the server skips assessing an empty session). This is what makes the
        // badge climb as the engineer teaches, instead of freezing between rounds.
        if (d.success) refreshAssessment();
    }).catch(e => {
        if (e.name === 'AbortError') {
            appendMsg('assistant', '⏹️ Stopped — response cancelled before finishing (no completion tokens spent past that point).', tag);
        } else {
            appendMsg('assistant', '⚠️ Network error: ' + e, tag);
        }
    }).finally(() => { setBusy(false); _activeChatAbort = null; assessBusy(false); });
}

// One high-information question when a chat message introduces knowledge
// outside (or contrary to) the committed baseline / loaded prior skill.
// Finite alternatives render as choices; diagnostic reasons and rules use a
// direct-answer field.
function renderProactiveClarification(q, stepTag) {
    if (!q || !q.question) return;
    const box = document.getElementById('chatBox');
    const card = el('div', 'chat-question-card proactive-chat-card mb-2');
    const basisLabels = {
        baseline: 'New vs baseline',
        loaded_skill: 'New vs loaded skill',
        both: 'New vs baseline + loaded skill',
    };
    card.appendChild(el('div', 'chat-q-progress', basisLabels[q.basis] || basisLabels.baseline));
    if (q.summary) card.appendChild(el('div', 'proactive-divergence-summary', q.summary));
    card.appendChild(el('div', 'chat-q-text', q.question));

    const submit = (answer) => {
        card.remove();
        const contextualAnswer = `Clarification question: ${q.question}\nEngineer answer: ${answer}`;
        sendMsg(contextualAnswer, answer, stepTag, false, q.decision_id);
    };
    const hasOptions = q.type === 'choice' && !!q.options && q.options.length >= 2;
    const optsBox = el('div', 'chat-q-opts');
    const recommendedIsOption = hasOptions && renderOptionButtons(optsBox, q.options, q.recommended_answer, submit);
    if (!recommendedIsOption) appendRecommendation(card, q);
    const skip = () => { deferDecision(q.decision_id); card.remove(); };
    attachAnswerBox(card, hasOptions, optsBox, buildAnswerBox({
        placeholder: 'Type the missing rule or reason — paste a table, or attach one…',
        hint: q.question,
        onSubmit: submit,
        onSkip: skip,
    }));
    if (hasOptions) card.appendChild(buildSkipRow(skip));
    box.appendChild(card);
    scrollChatToBottom();
}

// Live re-assessment after each chat answer: updates the readiness badge +
// the 防呆 details panel from the whole conversation so far. Cheap, standalone
// (no new analysis/questions), and a no-op until a filter has been run.
// The strip is a whole second LLM call behind the chat reply, so for that
// gap it showed the PREVIOUS round's list as if it were the answer to what
// was just taught. Counted, not a flag: the send marks it busy and the
// assess it triggers marks it again, so the two overlap without flickering.
let _assessPending = 0;

function assessBusy(on) {
    _assessPending = Math.max(0, _assessPending + (on ? 1 : -1));
    renderOpenItems(currentAssessment);
}

function refreshAssessment() {
    assessBusy(true);
    fetch(LV.url.learning_assess, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({use_prior_knowledge: priorMode()}),
    })
        .then(r => r.json())
        .then(d => {
            if (!d.success) return;
            if (d.usage) updateTokenBadge(d.usage.session);
            if (d.assessment) applyAssessment(d.assessment);
        })
        .catch(() => {}) // a failed background assess shouldn't disrupt the chat
        .finally(() => assessBusy(false));
}

function resetChat() {
    return fetch(LV.url.chatbot_reset, {method: 'POST'}).then(() => {
        document.getElementById('chatBox').innerHTML = '';
        // Server-side reset_teaching_progress() clears state.operations too
        // — mirror that here so the Steps panel doesn't go stale against
        // filter edits that no longer exist server-side.
        operationData = [];
        caseSummaryText = '';
        _caseIntakeOpen = false;
        baselineDone = false;
        baselineEverDone = false;   // the conversation WAS the export basis
        setBaselineGate();
        updateQuestionContextStatus();
        decisionLedger = {mode: interviewMode, items: [], open: 0, resolved: 0, deferred: 0, blocking: 0};
        updateDecisionLedger(decisionLedger);
        renderStepPanel();
        // Clearing the conversation clears the framing with it — ask again,
        // rather than silently carrying the old case description forward into
        // a transcript that no longer shows it.
        if (document.getElementById('logPathInput').value.trim()) renderCaseIntakeCard();
    });
}

// The most recent assessment {readiness, coverage, gaps, validation}, kept
// client-side so the Export gate and the details panel can read it without a
// refetch. Updated by the baseline read and by every chat answer.
let currentAssessment = null;

// Readiness score badge next to Export Skill — color band matches the
// readiness guide in services/learning_service.py (_ASSESS_TASKS).
function updateReadinessBadge(readiness, unverified) {
    const badge = document.getElementById('readinessBadge');
    // A dot, not a count: once the strip is gone this is the only sign that
    // unconfirmed claims are still sitting in the readiness panel.
    const warn = unverified ? ' ⚠' : '';
    if (!readiness || typeof readiness.score !== 'number') {
        badge.textContent = '🎯 —' + warn;
        badge.classList.remove('is-low', 'is-mid', 'is-high');
        return;
    }
    const score = readiness.score;
    // Number only — the word "Readiness" costs ~70px of a header that has to
    // fit Clear and Export skill too; the tooltip and the popover title carry it.
    badge.textContent = `🎯 ${score}%${warn}`;
    badge.classList.remove('is-low', 'is-mid', 'is-high');
    badge.classList.add(score >= 70 ? 'is-high' : score >= 35 ? 'is-mid' : 'is-low');
}

// The two claim buckets, split because only one of them is work. A
// contradiction has to be answered; an "asserted" claim is often something the
// log physically cannot prove (a firmware weighting rule), so it stops
// counting once the engineer confirms it is deliberate.
function claimBuckets(a) {
    const validation = (a && a.validation) || [];
    return {
        conflicts: validation.filter(v => v.status === 'contradiction' && !v.skipped),
        unconfirmed: validation.filter(v => v.status === 'asserted' && !v.acknowledged && !v.skipped),
        filed: validation.filter(v => v.status === 'asserted' && v.acknowledged && !v.skipped),
        setAside: validation.filter(v => v.skipped),
    };
}

// Single entry point for a fresh assessment (from /assess or /log_round).
function applyAssessment(a) {
    currentAssessment = a || null;
    const b = claimBuckets(a);
    updateReadinessBadge(a && a.readiness, b.conflicts.length + b.unconfirmed.length);
    renderReadinessPanel(a);
    renderOpenItems(a);
}

// What still needs work, sitting directly above the chat instead of three
// clicks deep. Two counters, deliberately separate because they are different
// jobs: gaps are questions to ANSWER, unverified claims are statements to
// CONFIRM OR CORRECT. Only the gaps keep the bar on screen — claims ride
// along while it is there, and fall back to the readiness panel once the last
// gap is answered or skipped.
//
// Only the one-line header takes layout space; the list opens DOWNWARD over
// the transcript (see .open-items-list). The chat card has a fixed height, so
// a list that grew in the flow had to steal that room from something — in the
// footer it pushed the input box clean out of the card.
//
// Deliberately NOT a transcript entry: assess_readiness re-runs after every
// answer, so appending would leave a trail of superseded lists. This is one
// element rewritten in place, same rule as the case-context note.
let _openItemsOpen = null;      // null | 'gaps' | 'claims'

function renderOpenItems(a) {
    const strip = document.getElementById('openItemsStrip');
    if (!strip) return;
    const gaps = (a && a.gaps) || [];
    const {conflicts, unconfirmed} = claimBuckets(a);
    strip.innerHTML = '';
    // The bar is the open-work list: answer or skip the last gap and it goes
    // away, rather than lingering on a claim count. Unverified claims do not
    // hold it open on their own — they are still listed in the readiness
    // panel, which the badge's ⚠ points at.
    strip.hidden = !gaps.length && !_assessPending;
    if (strip.hidden) { _openItemsOpen = null; return; }
    if (_openItemsOpen === 'claims' && !unconfirmed.length) _openItemsOpen = null;
    if (_openItemsOpen === 'conflicts' && !conflicts.length) _openItemsOpen = null;

    // stopPropagation on every control in here: re-rendering detaches the
    // clicked node, so the close-on-outside-click handler below would see a
    // target no longer inside the strip and shut it again immediately.
    const tab = (key, label, cls) => {
        const b = el('button', 'open-items-tab' + (cls ? ' ' + cls : ''));
        b.type = 'button';
        b.appendChild(el('span', 'open-items-caret', _openItemsOpen === key ? '▴' : '▾'));
        b.appendChild(el('span', 'open-items-title', label));
        b.onclick = (e) => {
            e.stopPropagation();
            _openItemsOpen = _openItemsOpen === key ? null : key;
            renderOpenItems(a);
        };
        return b;
    };

    const head = el('div', 'open-items-head');
    if (gaps.length) head.appendChild(tab('gaps', `Still missing (${gaps.length})`));
    // Contradictions get their own tab: buried among a dozen expert rules the
    // engineer supplied on purpose, the one claim that actually conflicts is
    // the one nobody reads.
    if (conflicts.length) {
        head.appendChild(tab('conflicts', `⛔ ${conflicts.length} contradiction`, 'is-bad'));
    }
    if (unconfirmed.length) {
        head.appendChild(tab('claims', `⚠ ${unconfirmed.length} not in this log`, 'is-warn'));
    }
    if (_assessPending) {
        const busy = el('span', 'open-items-busy');
        busy.appendChild(el('span', 'open-items-spin'));
        busy.appendChild(el('span', '', gaps.length ? 'rechecking…' : 'checking what\u2019s still missing…'));
        busy.title = 'The list below is the PREVIOUS round\u2019s — your last answer is still being assessed.';
        head.appendChild(busy);
    }
    // Both counters are running totals for the session, not deltas: "since
    // last time" was measured against whatever this tab last saw, so a reload
    // reported the whole session as one round. Green is the engineer's number
    // only — questions arriving is not progress, and painting the two the same
    // colour made a round that asked six read like a round that closed six.
    const settled = (a && a.settled) || 0;
    const asked = (a && a.raised) || 0;
    if (settled) head.appendChild(el('span', 'open-items-delta', `${settled} answered or skipped`));
    if (asked) head.appendChild(el('span', 'open-items-asked', `${asked} asked so far`));
    strip.appendChild(head);
    if (!_openItemsOpen) return;

    const list = el('div', 'open-items-list');
    const redraw = () => renderOpenItems(a);
    if (_openItemsOpen === 'gaps') {
        const arrived = (a && a.new_gaps) || [];
        // Newest first: the list no longer shrinks on its own, so the one
        // question that just arrived would otherwise sit at the bottom of six
        // the engineer has already read past.
        const ordered = arrived.concat(gaps.filter(g => arrived.indexOf(g) < 0));
        ordered.forEach(g => list.appendChild(gapRow(g, redraw, arrived.indexOf(g) >= 0)));
    } else if (_openItemsOpen === 'conflicts') {
        conflicts.forEach(v => list.appendChild(claimRow(v, redraw)));
    } else {
        unconfirmed.forEach(v => list.appendChild(claimRow(v, redraw)));
    }
    strip.appendChild(list);
}

function gapRow(g, redraw, isNew) {
    const item = el('div', 'open-items-item');
    const row = el('button', 'open-items-row', g);
    row.type = 'button';
    if (isNew) row.insertBefore(el('span', 'open-items-new', 'new'), row.firstChild);
    row.title = g + '\n\nClick to answer this one in the chat.';
    row.onclick = (e) => {
        e.stopPropagation();
        _openItemsOpen = null;
        openAskCard({
            tag: '🎯 Still missing',
            text: g,
            // Closed before the send: the answer is on its way into the
            // history, so this item has had its turn. Sequenced, not fired in
            // parallel — both calls answer with an assessment, and the older
            // one landing last would roll the badge back.
            onSubmit: (answer) => {
                closeGap(g, true)
                    .then(() => sendMsg(`Re: ${g}\n${answer}`, undefined, 'all', false, undefined, g));
            },
            onSkip: () => closeGap(g, false),
            skipLabel: 'Skip this',
        });
        redraw();
    };
    const skip = el('button', 'open-items-skip', 'Skip');
    skip.type = 'button';
    skip.title = "Not relevant to this case — stop counting it, here and at Export.";
    skip.onclick = (e) => { e.stopPropagation(); closeGap(g, false); };
    item.appendChild(row);
    item.appendChild(skip);
    return item;
}

// Nothing local may mark a claim VERIFIED — only the next assessment finding
// evidence in the log can do that. Filing one as deliberate expert knowledge
// is a different statement, and the only one the engineer is in a position to
// make: it says the log cannot prove this, which for a firmware rule is
// permanently true. Contradictions get no such button.
function claimRow(v, redraw) {
    const meta = _VALID_META[v.status] || _VALID_META.asserted;
    const item = el('div', 'open-items-item');
    const row = el('button', 'open-items-row', `${meta.icon} ${v.claim}`);
    row.type = 'button';
    row.title = meta.label + (v.note ? ' — ' + v.note : '') + '\n\n' + meta.prompt;
    row.onclick = (e) => {
        e.stopPropagation();
        _openItemsOpen = null;
        openClaimCard(v, meta);
        redraw();
    };
    item.appendChild(row);
    const skip = el('button', 'open-items-skip', 'Skip');
    skip.type = 'button';
    skip.title = 'Not this session\u2019s argument \u2014 stop counting it.'
               + '\nSays nothing about whether it is true, and it still shows at Export.';
    skip.onclick = (e) => { e.stopPropagation(); skipClaim(v.claim); };
    item.appendChild(skip);
    if (v.status === 'asserted') {
        const ack = el('button', 'open-items-skip open-items-force', 'Force expert');
        ack.type = 'button';
        ack.title = "Override the log check \u2014 this is domain knowledge on purpose, and the log"
                  + "\ncan't prove it and never will."
                  + "\nStops the warning. It still exports as asserted, not verified.";
        ack.onclick = (e) => {
            e.stopPropagation();
            if (forceExpertRule(v.claim)) acknowledgeClaim(v.claim);
        };
        item.appendChild(ack);
    }
    return item;
}

// One confirmation for both entry points: it is one click from a strip that
// re-renders under the cursor, and nothing later re-checks the claim.
function forceExpertRule(claim) {
    return confirm('Force this in as expert knowledge?\n\n' + claim
        + '\n\nThe log check is OVERRIDDEN \u2014 this claim will never be checked'
        + '\nagainst the log, now or later.'
        + '\nIt stops the warning, and still exports as asserted, never as verified.');
}

// Three ways out of one card, because a claim has three honest endings: cite
// the line that shows it, correct it, or state that the log was never going
// to show it. Skip is the fourth and says nothing about the claim itself —
// it is recorded, not swallowed, and still appears at Export.
function openClaimCard(v, meta) {
    const isAsserted = v.status === 'asserted';
    openAskCard({
        tag: `${meta.icon} ${meta.label} — ${meta.prompt}`,
        text: v.claim,
        placeholder: isAsserted
            ? 'Cite the line below, or correct the claim in your own words…'
            : 'Correct it here…',
        lineLookup: true,
        onSubmit: (answer) => sendMsg(`Re: ${v.claim}\n${answer}`, undefined, 'all', false),
        onSkip: () => skipClaim(v.claim),
        skipLabel: 'Skip',
        skipTitle: 'Not this session\u2019s argument \u2014 stop counting it.'
                 + '\nSays nothing about whether it is true, and it still shows at Export.',
        extraActions: isAsserted ? [{
            label: 'Force expert rule',
            cls: 'btn-outline-danger',
            title: "Override the log check: force this in as domain knowledge the log can't prove.",
            onClick: (close) => {
                if (!forceExpertRule(v.claim)) return;
                close();
                acknowledgeClaim(v.claim);
            },
        }] : [],
    });
}

function acknowledgeClaim(claim) {
    return fetch(LV.url.learning_ack_claim, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({claim: claim}),
    })
        .then(r => r.json())
        .then(d => { if (d.success) applyAssessment(d.assessment); })
        .catch(() => {});
}

function skipClaim(claim) {
    return fetch(LV.url.learning_skip_claim, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({claim: claim}),
    })
        .then(r => r.json())
        .then(d => { if (d.success) applyAssessment(d.assessment); })
        .catch(() => {});
}

// Both kinds are permanent; the two buckets stay separate because only one of
// them means the knowledge was actually given. Recorded server-side rather
// than hidden client-side — it has to survive a reload, and the Export gate
// reads that same payload.
function closeGap(gap, answered) {
    return fetch(LV.url.learning_skip_gap, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({gap: gap, answered: !!answered}),
    })
        .then(r => r.json())
        .then(d => { if (d.success) applyAssessment(d.assessment); })
        .catch(() => {});
}

// An overlay that covers the transcript has to get out of the way the same
// way every other popover here does.
document.addEventListener('click', function (e) {
    if (!_openItemsOpen) return;
    const strip = document.getElementById('openItemsStrip');
    if (strip && !strip.contains(e.target)) {
        _openItemsOpen = null;
        renderOpenItems(currentAssessment);
    }
});

// Answering a specific item only helps if the answer says WHICH item it is —
// the chat is free-form, and "yes, roaming-evaluation only" three messages
// later is unattributable. So a clicked item becomes a real question card in
// the transcript, the same shape as the LLM's own follow-ups: answer it (with
// an attachment if that's the easier evidence) or skip it, and the prompt
// itself stays in the history next to the answer.
function openAskCard(opts) {
    const box = document.getElementById('chatBox');
    if (!box) return;
    const previous = document.getElementById('gapAnswerCard');
    if (previous) previous.remove();          // one open card at a time

    const card = el('div', 'chat-question-card mb-2');
    card.id = 'gapAnswerCard';
    card.appendChild(el('div', 'chat-q-progress', opts.tag));
    card.appendChild(el('div', 'chat-q-text', opts.text));

    // forceTag 'all': these are about the round as a whole, not whatever step
    // the step-context selector happens to be pointing at.
    const answerBox = buildAnswerBox({
        hint: opts.text,
        placeholder: opts.placeholder,
        lineLookup: opts.lineLookup,
        onSubmit: (answer) => { card.remove(); opts.onSubmit(answer); },
        onSkip: opts.onSkip ? () => { card.remove(); opts.onSkip(); } : undefined,
        skipLabel: opts.skipLabel,
        // The action decides whether the card closes: one that opens a
        // confirmation must survive the engineer cancelling it.
        extraActions: (opts.extraActions || []).map(
            act => Object.assign({}, act, {onClick: () => act.onClick(() => card.remove())})),
    });
    attachAnswerBox(card, false, null, answerBox);

    box.appendChild(card);
    scrollChatToBottom();
    const input = card.querySelector('.chat-q-answer');
    if (input) input.focus();
}

function toggleReadinessPanel(evt) {
    if (evt) evt.stopPropagation();
    const p = document.getElementById('readinessPanel');
    const sp = document.getElementById('spendPanel');
    if (sp) sp.style.display = 'none';
    const opening = (p.style.display === 'none' || !p.style.display);
    // 'flex', not 'block' — the panel's CSS is display:flex (head + scrolling
    // body); an inline style="display:block" would override that class rule
    // (inline beats a class selector) and silently break the whole flex/
    // min-height:0/overflow-y:auto clipping chain, letting content spill
    // straight past max-height instead of scrolling inside its own box.
    p.style.display = opening ? 'flex' : 'none';
    if (opening) {
        // A plain vh-based max-height can't know where this popover happens
        // to sit on the page — cap it to whatever room is actually left
        // above the Skill-building chat card's bottom edge instead, so it
        // never overhangs past it regardless of scroll position.
        const chatCard = document.querySelector('.chat-panel-joined .card');
        if (chatCard) {
            const top = p.getBoundingClientRect().top;
            const chatBottom = chatCard.getBoundingClientRect().bottom;
            const available = Math.min(chatBottom - top - 8, window.innerHeight * 0.6);
            p.style.maxHeight = Math.max(160, available) + 'px';
        }
    }
}
// Now floating popovers — Spending under the session menu (bottom-left),
// Readiness under its trigger in the Chat header. Close both on any click
// outside EITHER anchor: the two triggers no longer share one container, and
// checking only the old one meant the click that opened Readiness immediately
// bubbled up and closed it again.
document.addEventListener('click', function(e) {
    const inAnchor = ['.session-menu', '#chatReadiness'].some(sel => {
        const anchor = document.querySelector(sel);
        return anchor && anchor.contains(e.target);
    });
    if (inAnchor) return;
    const rp = document.getElementById('readinessPanel');
    const sp = document.getElementById('spendPanel');
    if (rp) rp.style.display = 'none';
    if (sp) sp.style.display = 'none';
});

const _COVER_LABELS = {knowledge: 'Knowledge & rules', scope: 'Scope (non-overlapping)', keywords: 'Minimal keywords', evidence: 'Evidence (labeled lines)'};
// `label` names where the claim CAME FROM, not how true it is: "unverified"
// read as "we think this is wrong, defend it", when for expert knowledge the
// log was never going to show it and nothing is owed. Only a contradiction
// asks for a correction.
const _VALID_META = {
    verified:      {icon: '✅', cls: 'v-ok',   label: 'shown in this log',
                    prompt: 'Confirm or correct it.'},
    asserted:      {icon: '⚠️', cls: 'v-warn', label: 'from your knowledge, not this log',
                    prompt: "Point at a line that shows it, or force it in as an expert rule if this log can't."},
    contradiction: {icon: '⛔', cls: 'v-bad',  label: 'conflicts with the log or an earlier answer',
                    prompt: 'Correct it here.'},
};

// The readiness popover: coverage bars and the claim-by-claim check. The gaps
// are NOT here — they are the questions the engineer is about to answer, so
// they live in the strip above the chat where the answer gets typed
// (renderOpenItems).
function renderReadinessPanel(a) {
    const panel = document.getElementById('readinessPanel');
    panel.innerHTML = '';

    const head = el('div', 'readiness-panel-head');
    head.appendChild(el('span', 'readiness-panel-title', 'Readiness'));
    panel.appendChild(head);

    const body = el('div', 'readiness-body');
    panel.appendChild(body);

    if (!a || (!a.readiness && !(a.gaps || []).length && !(a.validation || []).length)) {
        body.appendChild(el('div', 'readiness-empty', 'Log a round or answer a question to assess readiness.'));
        return;
    }
    const cov = a.coverage || {};
    if (Object.keys(cov).length) {
        const box = el('div', 'readiness-cov');
        ['knowledge', 'scope', 'keywords', 'evidence'].forEach(k => {
            const v = typeof cov[k] === 'number' ? cov[k] : 0;
            const row = el('div', 'cov-row');
            row.appendChild(el('span', 'cov-label', _COVER_LABELS[k]));
            const track = el('span', 'cov-track');
            const fill = el('span', 'cov-fill');
            fill.style.width = v + '%';
            fill.classList.add(v >= 70 ? 'is-high' : v >= 35 ? 'is-mid' : 'is-low');
            track.appendChild(fill);
            row.appendChild(track);
            row.appendChild(el('span', 'cov-pct', v + '%'));
            box.appendChild(row);
        });
        body.appendChild(box);
    } else {
        body.appendChild(el('div', 'readiness-empty', 'No coverage breakdown yet.'));
    }

    const validation = a.validation || [];
    if (validation.length) {
        body.appendChild(el('div', 'readiness-h', `Claim check (${validation.length})`));
        const list = el('div', 'readiness-valid');
        validation.forEach(v => {
            const meta = _VALID_META[v.status] || _VALID_META.asserted;
            const item = el('div', 'valid-item ' + meta.cls);
            item.appendChild(el('span', 'valid-icon', meta.icon));
            const vbody = el('div', 'valid-body');
            vbody.appendChild(el('div', 'valid-claim', v.claim));
            const label = v.skipped ? 'set aside by you'
                : v.acknowledged ? 'expert knowledge, confirmed by you' : meta.label;
            vbody.appendChild(el('div', 'valid-note', label + (v.note ? ' — ' + v.note : '')));
            item.appendChild(vbody);
            list.appendChild(item);
        });
        body.appendChild(list);
    }
}

// tiny DOM helper
function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
}

// Explicit, on-demand: the engineer clicks this once they've set the filter
// the way they want and are submitting it as a round of evidence. One LLM
// call does three things at once — analyzes what this round's filter/delta
// actually captures, asks 1-3 grounded follow-up questions (structured, same
// "pick an option or type your own" shape as Claude's own AskUserQuestion),
// and self-scores overall readiness to export. Reusable: change the filter
// and click again to log another round, or just keep typing in the chat box
// without clicking anything — both feed the same chat_history the eventual
// Export Skill step reads from.
// The selected document set is the single source of truth for prior mode:
// one or more checked skills = use only those as read-only professional docs;
// zero = teach from this session alone. Every LLM-triggering call follows it.
function priorMode() {
    return selectedSkillKeys.length > 0;
}

function persistQuestionContext(payload) {
    return fetch(LV.url.learning_set_mode, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(d => {
        if (!d.success) throw new Error(d.message || 'Could not save question context');
        selectedSkillKeys = d.selected_skill_keys || selectedSkillKeys;
        if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
        renderSkillDocPicker();
        updateQuestionContextStatus();
        return d;
    });
}

function toggleSkillDocPicker(force) {
    const menu = document.getElementById('skillDocPickerMenu');
    const button = document.getElementById('skillDocPickerToggle');
    if (!menu || !button) return;
    const open = force !== undefined ? force : menu.hidden;
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) placeAnchoredMenu(menu, button);
}

function positionSkillDocMenu() {
    placeAnchoredMenu(
        document.getElementById('skillDocPickerMenu'),
        document.getElementById('skillDocPickerToggle'),
    );
}

function renderSkillDocPicker() {
    const list = document.getElementById('skillDocPickerList');
    if (!list) return;
    const selected = new Set(selectedSkillKeys);
    if (!availableSkillDocs.length) {
        list.innerHTML = '<div class="skill-doc-picker-empty">Load a WiFi or BT log to choose matching skill documents.</div>';
    } else {
        list.innerHTML = availableSkillDocs.map(skill => `
            <label class="skill-doc-option">
              <input type="checkbox" value="${escapeHtml(skill.key)}" ${selected.has(skill.key) ? 'checked' : ''}
                     onchange="onSkillDocSelectionChange()">
              <span><b>${escapeHtml(skill.name)}</b><small>${escapeHtml(skill.description || 'No description')}</small></span>
            </label>`).join('');
    }
    const count = selectedSkillKeys.length;
    const countEl = document.getElementById('skillDocCount');
    const button = document.getElementById('skillDocPickerToggle');
    if (countEl) countEl.textContent = count;
    if (button) {
        button.classList.toggle('has-docs', count > 0);
        button.title = count
            ? `${count} skill document(s) selected as prior knowledge`
            : 'Choose the existing skill documents Copycat may use as prior knowledge';
    }
}

function onSkillDocSelectionChange() {
    selectedSkillKeys = Array.from(
        document.querySelectorAll('#skillDocPickerList input[type="checkbox"]:checked')
    ).map(input => input.value);
    baselineDone = false;
    setBaselineGate();
    updateQuestionContextStatus();
    persistQuestionContext({selected_skill_keys: selectedSkillKeys}).catch(e => alert(e.message));
}

function clearSelectedSkillDocs() {
    selectedSkillKeys = [];
    baselineDone = false;
    setBaselineGate();
    renderSkillDocPicker();
    updateQuestionContextStatus();
    persistQuestionContext({selected_skill_keys: []}).catch(e => alert(e.message));
}

function persistCaseSummaryNow() {
    return persistQuestionContext({case_summary: caseSummaryText}).catch(e => alert(e.message));
}

function updateQuestionContextStatus() {
    renderSkillDocPicker();
    // The intake card and the transcript's recap note both show a doc count,
    // and either can be on screen when the picker changes. Broadcasting lets
    // them repaint without this function needing to know they exist.
    document.querySelectorAll('#caseIntakeCard, #caseContextNote').forEach(node =>
        node.dispatchEvent(new CustomEvent('copycat:docschanged')));
    const note = document.getElementById('caseContextNote');
    if (note) appendCaseContextNote();
}

function updateDecisionLedger(data) {
    if (!data) return;
    decisionLedger = data;
    interviewMode = 'ask';
    // An icon now, not a labelled button in the (removed) Context row: silent
    // while nothing is unresolved, showing the OPEN count only when there is
    // actually something to go and look at.
    const open = Number(data.open || 0);
    const total = (data.items || []).length;
    const count = document.getElementById('decisionBadgeText');
    if (count) {
        count.textContent = open;
        count.hidden = !open;
    }
    const button = document.getElementById('decisionBadge');
    if (button) {
        button.classList.toggle('has-open', !!open);
        button.title = total
            ? `Specification decisions — ${data.resolved || 0} of ${total} resolved`
            : 'No specification decisions yet';
    }
}

// ---- Case intake, asked in the conversation ------------------------------
// The case summary and the choice of reference documents used to be a
// collapsible "Context" row above the chat: a panel the engineer had to know
// to open, sitting next to the conversation it belonged in. Both are pure
// up-front framing for the baseline read, so they are now ASKED FOR, once, as
// the first thing in the chat — and the answer stays in the transcript, which
// the panel could never do. The value still lands in the same
// state.case_summary and is still fed to every later chat turn and question.
function renderCaseIntakeCard(opts) {
    opts = opts || {};
    if (_caseIntakeOpen) return;
    const box = document.getElementById('chatBox');
    if (!box) return;
    _caseIntakeOpen = true;

    const card = el('div', 'chat-question-card case-intake-card mb-2');
    card.id = 'caseIntakeCard';
    card.appendChild(el('div', 'chat-q-progress', opts.rewrite
        ? '📝 Rewriting the case description'
        : '👋 Before the first read'));
    card.appendChild(el('div', 'chat-q-text',
        'What is this capture about? One line is enough — the symptom, and when it happens.'));

    const textarea = document.createElement('textarea');
    textarea.className = 'form-control form-control-sm case-intake-input';
    textarea.rows = 2;
    textarea.maxLength = 4000;
    textarea.placeholder = 'e.g. Roaming disconnects about 3 seconds after reassociation';
    textarea.value = caseSummaryText;
    card.appendChild(textarea);

    // Reference documents belong to the same "what should Copycat know before
    // it reads?" question, so they are offered here rather than hidden behind
    // a separate header control.
    const docsRow = el('div', 'case-intake-docs');
    const docsBtn = el('button', 'btn btn-sm btn-outline-secondary', '');
    docsBtn.type = 'button';
    const paintDocs = () => {
        const n = selectedSkillKeys.length;
        docsBtn.innerHTML = `<i class="fas fa-book"></i> ${n ? `${n} reference doc${n > 1 ? 's' : ''}` : 'Add reference docs'}`;
        docsBtn.classList.toggle('has-docs', n > 0);
    };
    paintDocs();
    docsBtn.onclick = () => toggleSkillDocPicker(true);
    card.addEventListener('copycat:docschanged', paintDocs);
    // Same attach affordance as every question's answer box (buildAnswerBox) —
    // the case description is exactly the kind of thing an engineer sometimes
    // has as a screenshot of a bug report rather than typed-out prose.
    const attachBtn = el('button', 'btn btn-sm btn-outline-secondary case-intake-attach');
    attachBtn.type = 'button';
    attachBtn.innerHTML = '<i class="fas fa-paperclip"></i>';
    docsRow.appendChild(attachBtn);
    docsRow.appendChild(docsBtn);
    const docsHint = el('span', 'case-intake-hint',
        'Optional. Selected docs guide questions; they never replace log evidence.');

    const attachStatus = el('div', 'chat-q-attach-status');
    attachStatus.hidden = true;
    const attachments = wireAttachments({
        input: textarea, attachBtn, dropZone: card,
        setStatus: (text, kind) => {
            attachStatus.className = 'chat-q-attach-status' + (kind ? ' is-' + kind : '');
            attachStatus.textContent = text || '';
            attachStatus.hidden = !text;
        },
    });
    // Chips live in the button row, not under it: this card is already four
    // stacked rows tall and an attachment shouldn't add a fifth.
    docsRow.appendChild(attachments.pendingRow);
    docsRow.appendChild(docsHint);
    card.appendChild(docsRow);
    card.appendChild(attachStatus);

    const actions = el('div', 'step-explain-actions');
    const saveBtn = el('button', 'btn btn-sm btn-primary', opts.rewrite ? 'Update' : 'Continue');
    const skipBtn = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
    saveBtn.type = 'button';
    skipBtn.type = 'button';
    actions.appendChild(saveBtn);
    actions.appendChild(skipBtn);
    card.appendChild(actions);

    const close = () => { _caseIntakeOpen = false; card.remove(); };
    const commit = (text) => {
        caseSummaryText = (text || '').trim();
        // A changed framing invalidates any baseline read taken under the old
        // one — same rule the textarea's oninput used to enforce.
        baselineDone = false;
        persistCaseSummaryNow().then(() => {
            close();
            appendCaseContextNote();
            setBaselineGate();
        });
    };
    // A staged image is read (one transcription call) only once the engineer
    // commits — same deferred-to-send behaviour as buildAnswerBox, see
    // wireAttachments' resolvePending.
    const doCommit = () => {
        if (attachments.hasPending()) {
            saveBtn.disabled = true;
            textarea.disabled = true;
            attachments.resolvePending()
                .then(() => commit(textarea.value))
                .catch(() => {})
                .finally(() => { saveBtn.disabled = false; textarea.disabled = false; });
            return;
        }
        commit(textarea.value);
    };
    saveBtn.onclick = doCommit;
    skipBtn.onclick = () => { close(); setBaselineGate(); };
    textarea.onkeydown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doCommit(); }
    };

    box.appendChild(card);
    scrollChatToBottom();
    textarea.focus();
}

// The permanent transcript entry for what was collected, with the one way
// back in. Replaces any earlier copy so the log shows the CURRENT framing
// once, not a trail of superseded ones.
function appendCaseContextNote(opts) {
    const box = document.getElementById('chatBox');
    if (!box) return;
    const prev = document.getElementById('caseContextNote');

    const note = el('div', 'case-context-note mb-2');
    note.id = 'caseContextNote';
    const head = el('div', 'case-context-head', '');
    head.innerHTML = '<i class="fas fa-project-diagram"></i> Question context';
    const edit = el('button', 'btn btn-sm case-context-edit', 'Rewrite');
    edit.type = 'button';
    edit.title = 'Ask for the case description again';
    edit.onclick = () => renderCaseIntakeCard({rewrite: true});
    head.appendChild(edit);
    note.appendChild(head);

    const body = el('div', 'case-context-body');
    body.appendChild(caseContextChip('fa-align-left',
        caseSummaryText ? caseSummaryText : 'No case description', !!caseSummaryText));
    const n = selectedSkillKeys.length;
    body.appendChild(caseContextChip('fa-book',
        n ? `${n} reference doc${n > 1 ? 's' : ''}` : 'No reference docs', n > 0));
    if (contextLineCount) {
        const span = contextTimeSpan && contextTimeSpan.first
            ? ` · ${contextTimeSpan.first} → ${contextTimeSpan.last || contextTimeSpan.first}` : '';
        body.appendChild(caseContextChip('fa-stream',
            `${contextLineCount} context lines${span}`, true));
    }
    note.appendChild(body);

    // Update IN PLACE when a note is already in the transcript. Re-appending
    // it would teleport an old entry past everything said since — including
    // below a question that was asked after it — which is exactly the "why is
    // this above that?" confusion the transcript is supposed to avoid. Only a
    // deliberate re-post (the fresh one before a baseline read) moves it.
    if (prev && !(opts && opts.moveToEnd)) {
        prev.replaceWith(note);
        return;
    }
    if (prev) prev.remove();
    box.appendChild(note);
    scrollChatToBottom();
}

function caseContextChip(icon, text, ready) {
    const chip = el('span', 'case-context-chip' + (ready ? ' is-ready' : ''), '');
    chip.innerHTML = `<i class="fas ${icon}"></i> `;
    chip.appendChild(document.createTextNode(text));
    return chip;
}

// Renders the ledger body only — never touches the modal's own display
// style, so it doubles as an in-place refresh after answering/skipping an
// item from inside the modal (see resolveDecisionFromLedger/deferDecision)
// without popping the modal open behind the engineer's back when it's
// actually closed (e.g. a chat card's own Skip button calls deferDecision
// too, with the modal never opened at all).
function renderDecisionLedgerBody() {
    const body = document.getElementById('decisionLedgerBody');
    const items = decisionLedger.items || [];
    if (!items.length) {
        body.innerHTML = '<div class="decision-empty"><i class="fas fa-code-branch"></i><b>No decisions yet</b><span>Questions that affect scope, rules, keywords, or exceptions will appear here.</span></div>';
        return;
    }
    body.innerHTML = items.map((item) => {
        const status = item.status || 'open';
        const answer = item.answer
            ? `<div class="decision-answer"><b>Engineer:</b> ${escapeHtml(item.answer)}</div>` : '';
        // An OPEN choice item whose recommendation is literally one of its
        // options gets the badge on the button instead (see the forEach
        // below) — showing the same sentence again here would be the exact
        // duplication the chat cards had. Resolved/deferred items never grow
        // option buttons at all, so their recommendation only has this one
        // place to appear.
        const hasChoiceOptions = item.type === 'choice' && !!item.options && item.options.length >= 2;
        const recIsOption = status === 'open' && hasChoiceOptions && !!item.recommended_answer
            && item.options.some((o) => o.trim() === item.recommended_answer.trim());
        const rec = (item.recommended_answer && !recIsOption)
            ? `<div class="decision-recommendation"><b>Recommended:</b> ${escapeHtml(item.recommended_answer)}${item.recommendation_reason ? ` — ${escapeHtml(item.recommendation_reason)}` : ''}</div>` : '';
        // Open items get an empty slot filled in below with a real DOM
        // answer box (buildAnswerBox) — this modal used to be view-only,
        // which meant the ONLY way to resolve a decision was to still have
        // its original chat card on screen. Once that card had scrolled out
        // of view (a new question, a page reload) an open item had no
        // reachable answer box left at all.
        const answerSlot = status === 'open'
            ? `<div class="decision-answer-slot" data-decision-id="${escapeHtml(item.id)}"></div>` : '';
        return `<div class="decision-row is-${escapeHtml(status)}">
          <div class="decision-row-head"><span>${escapeHtml(item.id)}</span><b>${escapeHtml(item.source || 'chat')}</b><em>${escapeHtml(status)}</em></div>
          <div class="decision-question">${escapeHtml(item.question)}</div>${rec}${answer}${answerSlot}
        </div>`;
    }).join('');

    items.filter(item => (item.status || 'open') === 'open').forEach((item) => {
        const slot = body.querySelector(`.decision-answer-slot[data-decision-id="${CSS.escape(item.id)}"]`);
        if (!slot) return;
        const submit = (value) => resolveDecisionFromLedger(item.id, value);
        const skip = () => deferDecision(item.id);
        const hasOptions = item.type === 'choice' && !!item.options && item.options.length >= 2;
        const answerBox = buildAnswerBox({placeholder: 'Type your answer…', onSubmit: submit, onSkip: skip});
        if (hasOptions) {
            const optsBox = el('div', 'chat-q-opts');
            renderOptionButtons(optsBox, item.options, item.recommended_answer, submit);
            attachAnswerBox(slot, true, optsBox, answerBox);
        } else {
            attachAnswerBox(slot, false, null, answerBox);
        }
    });
}

function openDecisionLedger() {
    renderDecisionLedgerBody();
    document.getElementById('decisionLedgerModal').style.display = 'flex';
}

function closeDecisionLedger() {
    document.getElementById('decisionLedgerModal').style.display = 'none';
}

// Re-renders the ledger body in place ONLY if the modal is currently open —
// used after resolving/skipping a decision FROM the modal itself, so the row
// just answered immediately drops its answer box instead of waiting for the
// next manual open/close.
function refreshDecisionLedgerIfOpen() {
    const modal = document.getElementById('decisionLedgerModal');
    if (modal && modal.style.display === 'flex') renderDecisionLedgerBody();
}

function deferDecision(decisionId) {
    if (!decisionId) return;
    fetch(LV.url.learning_defer_decision, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({decision_id: decisionId}),
    })
        .then(r => r.json())
        .then(d => {
            if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
            refreshDecisionLedgerIfOpen();
        })
        .catch(() => {});
}

// Answers a decision straight from the ledger modal — no LLM call, same
// contract as answer_focus_clarify's chat-card path (see learning_routes.
// resolve_decision), so an item whose original chat card has scrolled away
// or never existed on this page load can still be resolved.
function resolveDecisionFromLedger(decisionId, answer) {
    if (!decisionId || !answer) return;
    fetch(LV.url.learning_resolve_decision, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({decision_id: decisionId, answer}),
    })
        .then(r => r.json())
        .then(d => {
            if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
            if (d.assessment) applyAssessment(d.assessment);
            refreshDecisionLedgerIfOpen();
        })
        .catch(() => {});
}

// Choice questions used to show the recommendation TWICE: once as its own
// "Recommended" block (readable, but the only thing on the card you couldn't
// click), and again, word-for-word, as a plain option button underneath it —
// clicking the option was the actual one-click way to accept it, the block
// above it was decoration. Folding the badge onto the matching button removes
// the duplicate and makes "click to accept the recommendation" the obvious
// affordance instead of a coincidence the engineer had to notice themselves.
// Returns true if one of the options WAS the recommendation, so the caller
// knows the standalone block is no longer needed.
function renderOptionButtons(optsBox, options, recommendedAnswer, onSelect) {
    const rec = (recommendedAnswer || '').trim();
    let matched = false;
    options.forEach((opt) => {
        const isRec = !!rec && opt.trim() === rec;
        if (isRec) matched = true;
        const btn = el('button', 'chat-q-opt' + (isRec ? ' is-recommended' : ''));
        btn.type = 'button';
        // The badge is a block ABOVE the option text, not inline text before
        // it — inline made every option's sentence start at a different x
        // the moment one of them grew a badge in front of it, undoing the
        // very alignment .chat-q-opt is block-laid-out to give you.
        if (isRec) btn.appendChild(el('div', 'chat-q-opt-badge', 'Recommended'));
        btn.appendChild(document.createTextNode(opt));
        btn.onclick = () => onSelect(opt);
        optsBox.appendChild(btn);
    });
    return matched;
}

// The RECOMMENDED block is the model's own proposed answer, and for a long
// time it was the one thing on the card you could not act on: the engineer
// read a sentence they agreed with, then retyped it underneath. Clicking it
// now loads it into the answer box — deliberately NOT submitting it, because
// its whole job is to be a starting point the engineer corrects or extends
// (adding the table, the exception, the real reason) before it is taught.
// Only reached when there's no option button already carrying the
// "Recommended" badge (see renderOptionButtons) — a free-answer question, or
// a choice question whose recommendation isn't literally one of the options.
function appendRecommendation(card, q) {
    if (!q || !q.recommended_answer) return;
    const row = el('button', 'question-recommendation');
    row.type = 'button';
    const head = el('div', 'question-recommendation-head');
    head.appendChild(el('span', 'question-recommendation-label', 'Recommended'));
    head.appendChild(el('span', 'question-recommendation-use', 'Click to use / edit'));
    row.appendChild(head);
    row.appendChild(el('div', 'question-recommendation-body', q.recommended_answer));
    if (q.recommendation_reason) {
        row.appendChild(el('small', '', q.recommendation_reason));
    }
    row.onclick = () => {
        const box = card.__answerBox;
        if (!box) return;
        box.reveal();
        box.fill(q.recommended_answer);
    };
    card.appendChild(row);
}

// ---- The answer box every question card shares ---------------------------
// The proactive-divergence, clarify, step-ask and batch-follow-up cards each
// carried their own copy of the same input + send + Skip row, which meant the
// two things wrong with it were wrong in four places at once:
//
//   * it was a single-line <input>, for answers that are routinely a rule
//     PLUS its reasoning — or a whole config table lifted out of a spec. Long
//     answers scrolled sideways out of sight and couldn't be re-read before
//     sending. It's now a <textarea> that grows with the content and stays
//     hand-resizable past the ceiling.
//   * there was no way to hand it a file. The evidence for a mapping rule is
//     very often a table in a screenshot or a .csv, and retyping it by hand
//     was the only way in.
//
// Text-file attachments resolve to TEXT INSIDE THIS BOX immediately — there's
// no LLM step involved, so no reason to wait. Images and PDFs are different:
// they are STAGED as a chip when attached and only actually read (one
// transcription call to /learning/transcribe_attachment, see resolvePending
// below) right before the message goes out, same as attaching a photo in a
// chat app — nothing is sent to the model until Send is pressed. Either way,
// what eventually reaches the LLM is still plain text inside this box; no
// downstream consumer (chat history, an operation's `reason`, an exported
// expert_rule) ever has to learn a second, image-shaped payload.
const _ATTACH_TEXT_MAX = 200 * 1024;         // past this it isn't evidence, it's a log
const _ATTACH_IMAGE_MAX = 5 * 1024 * 1024;
// A datasheet page is legitimately heavier than a screenshot; the server
// enforces its own matching ceiling (_MAX_PDF_B64 in learning_routes.py).
const _ATTACH_PDF_MAX = 10 * 1024 * 1024;
const _ATTACH_ACCEPT = '.txt,.csv,.tsv,.md,.markdown,.json,.yaml,.yml,.log,.tat,.pdf,image/*,application/pdf';
const _ATTACH_TITLE = 'Attach a table or notes. Text files are inserted as-is; an image or PDF is '
                    + 'staged and read when you send — like attaching a photo in a chat app. '
                    + 'You can also paste a screenshot straight into the box.';

// Shared by the per-question answer boxes (buildAnswerBox), the case-intake
// card, and the main chat input: file picker + paste + drag-drop. Extracted
// so every attach point gets the exact same behaviour rather than several
// subtly different implementations — a config table screenshotted out of a
// spec is just as often the case description as it is a question's answer.
function wireAttachments(opts) {
    const input = opts.input;
    const setStatus = opts.setStatus || function () {};
    const grow = opts.grow || function () {};

    const insertBlock = (label, body) => {
        const fence = '```';
        const block = `${label}\n${fence}\n${String(body).trim()}\n${fence}`;
        const current = input.value.replace(/\s+$/, '');
        input.value = current ? current + '\n\n' + block : block;
        grow();
        input.focus();
    };

    const picker = document.createElement('input');
    picker.type = 'file';
    picker.accept = _ATTACH_ACCEPT;
    picker.style.display = 'none';

    // Staged images/PDFs, not yet sent anywhere. resolvePending() drains this
    // list right before the answer is actually submitted.
    let pending = [];
    let nextPendingId = 1;
    const pendingRow = document.createElement('div');
    pendingRow.className = 'chat-q-pending-row';
    pendingRow.hidden = true;

    const paintPending = () => {
        pendingRow.innerHTML = '';
        pendingRow.hidden = pending.length === 0;
        pending.forEach((item) => {
            const chip = document.createElement('span');
            chip.className = 'chat-q-pending-chip' + (item.mediaType === 'application/pdf' ? ' is-pdf' : '');
            // The chip IS the "attached, not yet read" indicator; saying the
            // same thing again in the status line below only cost a row.
            chip.title = `${item.name} — read when you send`;
            chip.appendChild(el('span', 'chat-q-pending-name', item.name));
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'chat-q-pending-remove';
            remove.innerHTML = '&times;';
            remove.title = 'Remove this attachment';
            remove.onclick = () => {
                pending = pending.filter((p) => p.id !== item.id);
                paintPending();
                setStatus('', null);
            };
            chip.appendChild(remove);
            pendingRow.appendChild(chip);
        });
    };

    const stageMedia = (file, isPdf) => {
        if (file.size > (isPdf ? _ATTACH_PDF_MAX : _ATTACH_IMAGE_MAX)) {
            setStatus(isPdf ? 'That PDF is too large — attach just the relevant pages.'
                            : 'That image is too large — crop it to just the table.', 'error');
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            const url = String(reader.result || '');
            const comma = url.indexOf(',');
            pending.push({
                id: nextPendingId++,
                name: file.name || 'pasted image',
                mediaType: isPdf ? 'application/pdf' : file.type,
                b64: comma >= 0 ? url.slice(comma + 1) : '',
            });
            paintPending();
            setStatus('', null);
        };
        reader.onerror = () => setStatus('Could not read that file.', 'error');
        reader.readAsDataURL(file);
    };

    // Drag-dropped PDFs sometimes arrive with an empty file.type, so the
    // extension is checked too rather than letting them fall through to the
    // text reader and land in the box as binary garbage.
    const isPdfFile = (file) => (file.type || '').toLowerCase() === 'application/pdf'
                             || /\.pdf$/i.test(file.name || '');

    const takeFile = (file) => {
        if (!file) return;
        if (isPdfFile(file)) { stageMedia(file, true); return; }
        if ((file.type || '').startsWith('image/')) { stageMedia(file, false); return; }
        if (file.size > _ATTACH_TEXT_MAX) {
            setStatus('That file is too big to attach to an answer — paste just the part that matters.', 'error');
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            insertBlock(`From ${file.name}:`, reader.result || '');
            setStatus(`Attached ${file.name}.`, 'ok');
        };
        reader.onerror = () => setStatus('Could not read that file.', 'error');
        reader.readAsText(file);
    };

    picker.onchange = () => {
        takeFile(picker.files && picker.files[0]);
        picker.value = '';                      // so the same file can be re-picked
    };
    if (opts.attachBtn) {
        opts.attachBtn.title = _ATTACH_TITLE;
        opts.attachBtn.onclick = () => picker.click();
    }
    // Pasting a screenshot straight in is the shortest path from "the table is
    // on my other monitor" to "the table is in the answer".
    input.addEventListener('paste', (e) => {
        const items = (e.clipboardData && e.clipboardData.items) || [];
        for (let i = 0; i < items.length; i++) {
            if (items[i].type && items[i].type.startsWith('image/')) {
                const file = items[i].getAsFile();
                if (file) { e.preventDefault(); takeFile(file); return; }
            }
        }
    });
    const zone = opts.dropZone || input;
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('is-dropping'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('is-dropping'));
    zone.addEventListener('drop', (e) => {
        e.preventDefault();
        zone.classList.remove('is-dropping');
        takeFile(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]);
    });

    // Called by the caller's own submit path right before the answer actually
    // goes out — never on a timer, never speculatively. Transcribes every
    // staged image/PDF in order (sequential, not parallel, so the inserted
    // blocks land in the order they were attached) and inserts each as text,
    // exactly like the old immediate-transcribe path did — just deferred to
    // this one moment instead of firing the instant a file was picked. A
    // failure here must stop the caller's submit, not send a message that
    // silently lost an attachment, which is why this rejects instead of
    // swallowing the error.
    const resolvePending = () => {
        if (!pending.length) return Promise.resolve();
        const items = pending;
        pending = [];
        paintPending();
        if (opts.attachBtn) opts.attachBtn.disabled = true;
        setStatus(items.length > 1 ? `Reading ${items.length} attachments…` : `Reading ${items[0].name}…`, 'busy');
        const hint = [opts.hint, input.value.trim()].filter(Boolean).join('\n').slice(0, 500);
        return items.reduce((chain, item, idx) => chain.then(() =>
            fetch(LV.url.learning_transcribe_attachment, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({media_type: item.mediaType, data: item.b64, hint: hint}),
            })
                .then(r => r.json())
                .then(d => {
                    if (!d.success || !d.text) throw new Error(d.message || `Could not read ${item.name}.`);
                    if (d.usage && d.usage.session) updateTokenBadge(d.usage.session);
                    insertBlock(`Transcribed from ${item.name}:`, d.text);
                })
                .catch((err) => {
                    // Re-stage what never made it into the box. The common
                    // failure here is a transient one (proxy quota, dropped
                    // connection), and losing the file to it would mean
                    // finding and re-attaching it by hand to retry.
                    pending = items.slice(idx).concat(pending);
                    paintPending();
                    throw err;
                })
        ), Promise.resolve())
            .then(() => { setStatus('', null); })
            .catch((e) => {
                setStatus(e.message || 'Could not read an attachment.', 'error');
                throw e;
            })
            .finally(() => { if (opts.attachBtn) opts.attachBtn.disabled = false; });
    };

    return {picker, pendingRow, takeFile, resolvePending, hasPending: () => pending.length > 0};
}

function buildAnswerBox(opts) {
    const box = el('div', 'chat-q-custom');
    const input = el('textarea', 'chat-q-answer form-control form-control-sm');
    input.rows = 3;
    input.placeholder = opts.placeholder || 'Type your answer…';

    // Grow with the content, but stop the moment the engineer drags the
    // resize grip themselves — from then on their height wins.
    let autoHeight = 0;
    let manual = false;
    const scrollerOf = () => box.closest('#chatBox, .modal-body, .decision-ledger-body');
    // The chat is a fixed-height dock, so a flat 340px ceiling could leave a
    // pasted answer taller than the panel — question scrolled off the top,
    // Send button below the fold. The real ceiling is whatever is left of the
    // visible area once the rest of the card has taken its share.
    const ceiling = () => {
        const scroller = scrollerOf();
        if (!scroller || !scroller.clientHeight) return 340;
        const card = box.closest('.chat-question-card') || box;
        const others = Math.max(0, card.offsetHeight - input.offsetHeight);
        return Math.max(62, Math.min(340, scroller.clientHeight - others - 24));
    };
    const grow = () => {
        if (manual) return;
        input.style.height = 'auto';
        autoHeight = Math.min(input.scrollHeight + 2, ceiling());
        input.style.height = autoHeight + 'px';
        keepActionsVisible();
    };
    // Typing near the ceiling still walks the action row towards the bottom
    // edge; follow it rather than making the engineer scroll mid-sentence.
    const keepActionsVisible = () => {
        const scroller = scrollerOf();
        if (!scroller) return;
        const over = actions.getBoundingClientRect().bottom - scroller.getBoundingClientRect().bottom + 8;
        if (over > 0) scroller.scrollTop += over;
    };
    if (window.ResizeObserver) {
        new ResizeObserver(() => {
            if (autoHeight && Math.abs(input.offsetHeight - autoHeight) > 4) manual = true;
        }).observe(input);
    }
    input.addEventListener('input', grow);

    const status = el('div', 'chat-q-attach-status');
    status.hidden = true;
    const setStatus = (text, kind) => {
        status.className = 'chat-q-attach-status' + (kind ? ' is-' + kind : '');
        status.textContent = text || '';
        status.hidden = !text;
    };

    const attachBtn = el('button', 'btn btn-sm btn-outline-secondary chat-q-attach');
    attachBtn.type = 'button';
    attachBtn.innerHTML = '<i class="fas fa-paperclip"></i>';
    const attachments = wireAttachments({
        input, attachBtn, setStatus, grow, dropZone: box, hint: opts.hint,
    });

    // A staged image is read (one transcription call) at the moment of
    // Send, not the moment it was attached — see wireAttachments'
    // resolvePending. Disabling the input/buttons for that one round-trip
    // is what stops Enter or a second click from firing a duplicate submit
    // while it's in flight.
    const submit = () => {
        if (attachments.hasPending()) {
            input.disabled = true;
            sendBtn.disabled = true;
            attachments.resolvePending()
                .then(() => {
                    const value = input.value.trim();
                    if (value) opts.onSubmit(value);
                })
                .catch(() => {})   // resolvePending already surfaced the error via setStatus
                .finally(() => { input.disabled = false; sendBtn.disabled = false; });
            return;
        }
        const value = input.value.trim();
        if (value) opts.onSubmit(value);
    };
    // Enter still sends, so a one-line answer is still a one-key answer;
    // Shift+Enter is the newline a multi-line answer now actually needs.
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            submit();
        }
    });

    const sendBtn = el('button', 'btn btn-sm btn-primary');
    sendBtn.type = 'button';
    sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i>';
    sendBtn.onclick = submit;

    const actions = el('div', 'chat-q-actions');
    actions.appendChild(attachBtn);
    actions.appendChild(el('span', 'chat-q-hint', 'Enter sends · Shift+Enter new line'));
    actions.appendChild(sendBtn);
    if (opts.onSkip) {
        const skipBtn = el('button', 'btn btn-sm btn-outline-secondary', opts.skipLabel || 'Skip');
        skipBtn.type = 'button';
        skipBtn.title = opts.skipTitle || '';
        skipBtn.onclick = opts.onSkip;
        actions.appendChild(skipBtn);
    }
    (opts.extraActions || []).forEach(act => {
        const b = el('button', 'btn btn-sm ' + (act.cls || 'btn-outline-secondary'), act.label);
        b.type = 'button';
        b.title = act.title || '';
        b.onclick = act.onClick;
        actions.appendChild(b);
    });

    box.appendChild(input);
    if (opts.lineLookup) box.appendChild(buildLineLookup(box));
    box.appendChild(attachments.pendingRow);
    box.appendChild(actions);
    box.appendChild(status);
    box.appendChild(attachments.picker);

    // Used by the "Other…" option button and by the clickable RECOMMENDED
    // block, both of which have to open this box before they can write to it.
    box.reveal = () => {
        const optsBox = box.parentElement && box.parentElement.querySelector('.chat-q-opts');
        if (optsBox) optsBox.style.display = 'none';
        box.style.display = 'flex';
        grow();
        input.focus();
    };
    box.fill = (text) => {
        const current = input.value.trim();
        input.value = current ? current + '\n' + text : text;
        grow();
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
    };
    return box;
}

// The line number is what the engineer can read off the pane; the line TEXT
// is what the model needs, and typing it out by hand is why claims went
// uncited. Pulls both in, and moves the pane there so the citation can be
// checked before it is sent.
function buildLineLookup(box) {
    const row = el('div', 'chat-q-lineref');
    const field = el('input', 'form-control form-control-sm chat-q-lineno');
    field.type = 'number';
    field.min = '1';
    field.placeholder = 'Line #';
    const note = el('span', 'chat-q-lineref-note');
    const add = el('button', 'btn btn-sm btn-outline-secondary', 'Cite line');
    add.type = 'button';
    add.title = 'Copy that line into the answer and jump the log pane to it.';
    const run = () => {
        const n = parseInt(field.value, 10);
        if (!Number.isFinite(n)) return;
        note.textContent = '';
        fetch(LV.url.log_viewer_row_for_line, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({line_no: n}),
        })
            .then(r => r.json())
            .then(d => {
                if (!d.success || d.index == null) { note.textContent = 'No log loaded.'; return; }
                box.fill(`Line ${d.line_no}: ${d.text || '(no text)'}`);
                scrollLogToIndex(d.index);
                note.textContent = d.exact ? '' : `line ${n} is filtered out — cited ${d.line_no}`;
                field.value = '';
            })
            .catch(() => { note.textContent = 'Could not read that line.'; });
    };
    add.onclick = run;
    field.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); run(); }
    });
    row.appendChild(el('span', 'chat-q-lineref-label', 'Evidence line'));
    row.appendChild(field);
    row.appendChild(add);
    row.appendChild(note);
    return row;
}

// The custom-answer row starts hidden behind an "Other…" button whenever the
// question offers concrete options, and is the only input otherwise.
function attachAnswerBox(card, hasOptions, optsBox, answerBox) {
    card.__answerBox = answerBox;
    if (hasOptions) {
        answerBox.style.display = 'none';
        const otherBtn = el('button', 'chat-q-opt chat-q-opt-other', 'Other…');
        otherBtn.type = 'button';
        otherBtn.onclick = () => answerBox.reveal();
        optsBox.appendChild(otherBtn);
        card.appendChild(optsBox);
    } else {
        answerBox.style.display = 'flex';
    }
    card.appendChild(answerBox);
}

// ---- Baseline read (auto, once per loaded filter set) -------------------
// Replaces the removed "Log Round & Analyze" button. The old flow needed the engineer to
// explicitly submit a "round" before anything was analyzed or scored; now the
// LLM commits to its own read of the default filter the moment one is loaded
// (see /learning/baseline), which both gives the session its first readiness
// assessment without any user action and — more importantly — creates the
// prior that every later edit is compared against (utils/divergence.py).
//
// Fires only on a filter-SET load (pickTat/loadSkill), never on an individual
// edit: re-reading after each toggle would re-baseline against the engineer's
// own change and destroy the comparison. The server additionally caches by
// filter signature, so a repeat call can't spend a second LLM call.
// ---- The baseline gate ---------------------------------------------------
// The first read is an explicit, engineer-pressed action rather than something
// that fires the moment a filter loads. Two reasons it has to be:
//
//   * A baseline formed on a filter that survives NOTHING describes nothing —
//     and it then becomes the thing every later edit is compared against, so a
//     junk first read poisons the whole session's divergence detection.
//   * Auto-firing spent an LLM call on every .tat load, including the ones
//     where the engineer immediately picked a different file.
//
// So: disabled until a log is loaded AND the filter actually matched
// something, glowing once it can be pressed, and the knowledge-feeding
// actions (teaching a step, exporting) stay locked until it has run.
let baselineDone = LV.boot.hasBaseline || false;
// baselineDone goes back to false whenever the thing a baseline was taken
// against changes (filter set, reference docs, framing) so the button asks for
// a re-read. Export must NOT follow it back down: once a read exists there IS
// something to diverge from, and re-locking the export mid-session just to
// re-run an LLM call the engineer didn't ask for is what made it feel stuck.
let baselineEverDone = baselineDone;
let lastMatchCount = 0;

function setBaselineGate() {
    const btn = document.getElementById('baselineBtn');
    if (!btn) return;
    const label = document.getElementById('baselineBtnLabel');
    const hasLog = !!document.getElementById('logPathInput').value.trim();
    const ready = hasLog && filterData.length > 0 && lastMatchCount > 0;

    btn.classList.toggle('is-ready', ready && !baselineDone);
    btn.classList.toggle('is-done', baselineDone);
    btn.disabled = !ready || isBusy();

    if (baselineDone) {
        label.textContent = 'Update baseline';
        btn.title = "The LLM's first read of this filter is recorded. Every later edit is "
                  + 'compared against it — that comparison is what turns your filtering into '
                  + 'taught knowledge.';
        btn.title = 'Re-analyze the current chat knowledge, filter steps, labeled E/X observations, '
                  + 'and surviving log lines. This remains available without the first-time glow.';
    } else if (!hasLog) {
        label.textContent = 'Set baseline';
        btn.title = 'Load a log first.';
    } else if (!filterData.length) {
        label.textContent = 'Set baseline';
        btn.title = 'Load a .tat file or a skill so there is a filter to read.';
    } else if (!lastMatchCount) {
        label.textContent = 'Set baseline';
        btn.title = 'The filter currently matches 0 lines. A first read of a filter that '
                  + 'survives nothing would describe nothing, and everything you teach '
                  + 'afterwards is compared against it — adjust the filter until it hits '
                  + 'something.';
    } else {
        label.textContent = 'Set baseline';
        btn.title = "Record the LLM's first read of this filter. Teaching steps and Export "
                  + 'unlock once it has run.';
    }
    setTeachingLocks();
}

// Everything that feeds knowledge back waits behind the baseline, because
// without a recorded first read there is nothing for an edit to diverge FROM —
// the teaching would have no comparison to be measured against.
function setTeachingLocks() {
    const why = 'Set the comparison baseline first — teaching is measured as divergence '
              + "from the LLM's first read, so there has to be one.";
    document.querySelectorAll('.step-teach-btn').forEach((b) => {
        b.disabled = !baselineDone;
        if (!baselineDone) b.title = why;
    });
    const exportBtn = document.getElementById('exportSkillBtn');
    if (exportBtn) {
        exportBtn.disabled = !baselineEverDone || isBusy();
        exportBtn.title = baselineEverDone
            ? 'Turn the baseline, teaching, and observations into reusable skill(s).'
            : why;
    }
}

function requestBaseline(attempt, forceRefresh) {
    attempt = attempt || 0;
    if (forceRefresh === undefined) forceRefresh = baselineDone;
    if (!filterData.length) return;                       // nothing to read yet
    if (!document.getElementById('logPathInput').value.trim()) return;
    const btn = document.getElementById('baselineBtn');
    if (!attempt) {
        setBusy(true);
        if (btn) {
            btn.disabled = true;
            document.getElementById('baselineBtnLabel').textContent =
                forceRefresh ? 'Updating…' : 'Reading…';
        }
    }
    fetch(LV.url.learning_baseline, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            force: !!forceRefresh,
            case_summary: caseSummaryText,
            selected_skill_keys: selectedSkillKeys,
        }),
    })
        .then(r => r.status === 503 ? {retry: true} : r.json())
        .then(d => {
            // 503 = the LLM key is still being read on the background thread
            // (see configs/set_up_app._configure_llm — key.py lives on a UNC
            // corp share and no longer blocks startup). Loading a .tat within
            // the first seconds is exactly when this lands, so retry rather
            // than leaving the session silently baseline-less, which would
            // disable divergence detection for the whole session with no
            // visible symptom.
            if (d.retry) {
                if (attempt < 8) {
                    setTimeout(() => requestBaseline(attempt + 1, forceRefresh), 2000);
                } else {
                    setBusy(false);
                    setBaselineGate();
                    alert('AI connection is still unavailable. Try Update baseline again shortly.');
                }
                return;
            }
            if (!d.success) {
                setBusy(false);
                setBaselineGate();
                alert(d.message || 'Baseline read failed');
                return;
            }
            baselineDone = true;
            baselineEverDone = true;
            renderStepPanel();          // setup steps fold away now they're history
            setBusy(false);
            if (d.cached) return;                 // already read, nothing new to show
            if (d.usage) updateTokenBadge(d.usage.session);
            if (d.assessment) applyAssessment(d.assessment);
            // Restate what this read was actually given — case description,
            // reference docs, and the now-known context size — immediately
            // before the analysis it produced, so the transcript records the
            // inputs next to the output instead of only the output. This is
            // the one place the note deliberately moves to the end.
            appendCaseContextNote({moveToEnd: true});
            appendMsg('assistant', d.message || `# Baseline analysis\n\n${d.baseline.analysis}`, 'all');
        })
        .catch(() => {
            setBusy(false);
            setBaselineGate();
        });   // never let a failed baseline disrupt filtering
}

// "Teach this step" — USER-LED: clicking a step's 🎓 icon (see
// renderStepPanel) opens a box where the ENGINEER writes first — what key
// thing they noticed, what the problem/reasoning was — instead of the LLM
// firing a question. Submitting sends that straight to /learning/confirm_step,
// which records it as this operation's reason immediately, then the LLM
// condenses it into a confirmable knowledge-core statement + adds its own
// expert perspective (see renderStepConfirmCard). Uses the same
// prior-knowledge toggle as the baseline read (see priorMode()) so PRIOR mode keeps
// the knowledge core distinct from an already-loaded skill's own content.
function openStepExplainBox(seq, anchorBtn) {
    if (isBusy() || anchorBtn.disabled) return;
    // Sync the send-row step-tag button to whatever step is being taught, so
    // anything typed in the main chat box while this box is open (or after
    // "Continue teaching" reopens it) is tagged to the same step by default.
    currentStepTag = seq;
    renderStepTagSelector();
    const box = document.getElementById('chatBox');
    const card = el('div', 'chat-question-card step-explain-card mb-2');
    card.appendChild(el('div', 'chat-q-progress', `🎓 Teaching step ${stepLabel(seq)}`));
    card.appendChild(el('div', 'chat-q-text', 'What key thing did you notice — and what was the problem?'));

    const textarea = document.createElement('textarea');
    textarea.className = 'form-control form-control-sm step-explain-input';
    textarea.rows = 3;
    textarea.placeholder = 'e.g. "Mcc floods the log with periodic housekeeping noise unrelated to roam scoring — excluding it here is safe because..."';

    const actions = el('div', 'step-explain-actions');
    const submitBtn = el('button', 'btn btn-sm btn-primary', 'Submit');
    const cancelBtn = el('button', 'btn btn-sm btn-outline-secondary', 'Cancel');
    actions.appendChild(submitBtn);
    actions.appendChild(cancelBtn);

    cancelBtn.onclick = () => card.remove();
    const submit = () => {
        const explanation = textarea.value.trim();
        if (!explanation) return;
        submitStepExplanation(seq, explanation, card);
    };
    submitBtn.onclick = submit;
    textarea.onkeydown = (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
    };

    card.appendChild(textarea);
    card.appendChild(actions);
    box.appendChild(card);
    scrollChatToBottom();
    textarea.focus();
}

function submitStepExplanation(seq, explanation, card) {
    if (isBusy()) return;
    setBusy(true);
    const submitBtn = card.querySelector('.step-explain-actions .btn-primary');
    submitBtn.disabled = true;
    const label = submitBtn.textContent;
    submitBtn.textContent = 'Submitting…';
    fetch(LV.url.learning_confirm_step, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({seq: seq, explanation: explanation}),
    })
        .then(r => r.json())
        .then(d => {
            setBusy(false);
            // `d.operations` is present whenever the reason was actually
            // saved server-side — annotate_reason runs BEFORE the LLM call,
            // so even an LLM failure still lands here (see confirm_step's
            // except-branch), not the "hard failure" branch below. Only a
            // genuine pre-save failure (unknown operation) skips it.
            if (d.operations) {
                card.remove();
                appendMsg('user', explanation, seq);
                if (d.usage) updateTokenBadge(d.usage.session);
                syncOpsQuiet(d); // updates the step's "✓ why" indicator
                // A recorded reason is real teaching content (readiness's
                // gap-list explicitly tracks unexplained operations) — this
                // used to only refresh on regular chat sends / analysis,
                // which made the badge feel stuck after a 🎓 explanation.
                refreshAssessment();
                if (!d.success) {
                    appendMsg('assistant', `⚠️ Reason saved, but couldn't get a knowledge-core summary: ${d.message}`, seq);
                    return;
                }
                if (d.llm_unavailable) return; // reason saved; nothing more to show
                if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
                renderStepConfirmCard(
                    seq, d.knowledge_core, d.expert_note, d.follow_up_question,
                    d.follow_up_decision_id
                );
                return;
            }
            // Nothing was saved (e.g. the operation vanished mid-edit) — keep
            // the card open with the engineer's text intact so they can retry
            // instead of losing what they wrote.
            submitBtn.disabled = false; submitBtn.textContent = label;
            alert(d.message);
        })
        .catch(e => {
            setBusy(false);
            submitBtn.disabled = false; submitBtn.textContent = label;
            alert('Failed: ' + e);
        });
}

// Mirrors the exact text /learning/confirm_step already appends to
// state.chat_history server-side (see learning_routes.py confirm_step) — used
// so dismissing the interactive confirm card leaves the SAME permanent bubble
// behind that a page reload would replay, instead of the response just
// vanishing from view while still sitting in server-side history.
function stepKnowledgeCoreText(seq, knowledgeCore, expertNote, followUp) {
    const parts = [`**Step #${seq} — knowledge core:** ${knowledgeCore}`];
    if (expertNote) parts.push(`*Expert note:* ${expertNote}`);
    if (followUp) parts.push(`*(optional follow-up: ${followUp})*`);
    return parts.join('\n\n');
}

// The LLM's response to a step explanation: a condensed, CONFIRMABLE
// knowledge-core statement (✓ Confirm just acknowledges it — the underlying
// reason is already saved) plus the LLM's own expert_note (a second opinion,
// shown read-only), and an optional follow-up question the engineer can
// choose to address by adding more (never forced — this flow is user-led).
function renderStepConfirmCard(seq, knowledgeCore, expertNote, followUp, decisionId) {
    const box = document.getElementById('chatBox');
    const card = el('div', 'chat-question-card step-confirm-card mb-2');
    card.appendChild(el('div', 'chat-q-progress', `🧠 Knowledge core — step ${stepLabel(seq)}`));
    card.appendChild(el('div', 'step-confirm-core', knowledgeCore));
    if (expertNote) {
        const note = el('div', 'step-confirm-note');
        note.appendChild(el('span', 'step-confirm-note-label', '💭 Expert note: '));
        note.appendChild(document.createTextNode(expertNote));
        card.appendChild(note);
    }

    // Primary actions: confirm done, or reopen explain box to keep teaching
    const actions = el('div', 'step-explain-actions');
    const confirmBtn = el('button', 'btn btn-sm btn-success', '✓ Looks right');
    const continueBtn = el('button', 'btn btn-sm btn-outline-primary', '＋ Continue teaching');
    const persist = () => appendMsg('assistant', stepKnowledgeCoreText(seq, knowledgeCore, expertNote, followUp), seq);
    confirmBtn.onclick = () => { persist(); card.remove(); };
    continueBtn.onclick = () => { persist(); card.remove(); openStepExplainBox(seq, {disabled: false}); };
    actions.appendChild(confirmBtn);
    actions.appendChild(continueBtn);
    card.appendChild(actions);

    // Optional LLM follow-up question — interactive: answer sends to chat with
    // this step's tag; skip just dismisses the sub-section (card stays).
    if (followUp) {
        const fuSection = el('div', 'step-confirm-followup-section');
        fuSection.appendChild(el('div', 'step-confirm-note-label', '❓ Follow-up:'));
        fuSection.appendChild(el('div', 'chat-q-text', followUp));
        const fuBox = buildAnswerBox({
            placeholder: 'Answer… or skip',
            hint: followUp,
            onSubmit: (ans) => {
                persist();
                card.remove();
                sendMsg(ans, undefined, seq, false, decisionId);
            },
            onSkip: () => { deferDecision(decisionId); fuSection.remove(); },
        });
        fuBox.style.display = 'flex';
        card.__answerBox = fuBox;
        fuSection.appendChild(fuBox);
        card.appendChild(fuSection);
    }

    box.appendChild(card);
    scrollChatToBottom();
}

// Offer ONE clarifying question when the engineer's last edit contradicted
// the baseline read (see /learning/clarify). This does auto-fire an LLM call
// off a filter action — the pattern this app deliberately removed once — but
// the gate is now categorically different: it isn't "an edit happened", it's
// "a MEASURED, still-unexplained edit reversed a stance the model had
// committed to in writing beforehand", which utils/divergence.py decides with
// no model involved. In practice that's rare; ordinary filter building
// produces none. Two backstops keep it that way:
//   • the server refuses to re-ask a divergence already put to the engineer
//     (state.clarified_seqs), so a double-fire can't double-charge;
//   • nothing fires at all without contradictions, and the request is skipped
//     client-side in that case so the common path costs literally nothing.
function maybeClarify() {
    if (interviewMode === 'quiet') return;
    const pending = (divergenceData.contradictions || []).length > 0
        || (divergenceData.focus && !divergenceData.focus_clarified);
    if (!pending) return;                       // no round-trip on the common path
    if (document.querySelector('.clarify-card')) return;  // one open question at a time
    fetch(LV.url.learning_clarify, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    })
        .then(r => r.json())
        .then(d => { if (d.success && d.question) renderClarifyCard(d); })
        .catch(() => {});   // a failed clarification must never disrupt filtering
}

// The clarifying-question card. Contradictions reuse submitStepAnswer, whose
// existing behaviour is exactly right here: the answer becomes that
// operation's `reason`, which both captures the knowledge for export AND
// drops the edit out of the unexplained set so the same divergence can't
// come back. Focus questions have no operation to attach to and go to
// /learning/answer_focus_clarify instead.
function renderClarifyCard(d) {
    const q = d.question;
    const box = document.getElementById('chatBox');
    const card = el('div', 'chat-question-card clarify-card mb-2');
    card.appendChild(el('div', 'chat-q-progress',
        d.kind === 'focus' ? '🎯 About the issue time you selected'
        : d.kind === 'omission' ? `💡 New knowledge about "${d.keyword}"`
                           : `🔍 About "${d.keyword}"`));
    card.appendChild(el('div', 'chat-q-text', q.question));

    const submit = (answer) => {
        if (d.kind === 'focus') {
            fetch(LV.url.learning_answer_focus_clarify, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({question: q.question, answer, decision_id: d.decision_id || ''}),
            }).then(r => r.json()).then(result => {
                if (result.decision_ledger) updateDecisionLedger(result.decision_ledger);
                if (result.assessment) applyAssessment(result.assessment);
                card.remove();
                appendMsg('assistant', `❓ ${q.question}`, 'all');
                appendMsg('user', answer, 'all');
            });
        } else {
            submitStepAnswer(d.seq, q.question, answer, card, d.decision_id);
        }
    };

    // Skipping is a real answer ("I don't want to explain this"), and the
    // server has already recorded the divergence as asked — so it won't
    // reappear on the next filter run.
    const skip = () => { deferDecision(d.decision_id); card.remove(); };
    const hasOptions = q.type === 'choice' && !!q.options && q.options.length > 0;
    const optsBox = el('div', 'chat-q-opts');
    const recommendedIsOption = hasOptions && renderOptionButtons(optsBox, q.options, q.recommended_answer, submit);
    if (!recommendedIsOption) appendRecommendation(card, q);
    if (d.captures) card.appendChild(el('div', 'clarify-captures', d.captures));
    attachAnswerBox(card, hasOptions, optsBox, buildAnswerBox({
        hint: q.question,
        onSubmit: submit,
        onSkip: skip,
    }));
    if (hasOptions) card.appendChild(buildSkipRow(skip));
    box.appendChild(card);
    scrollChatToBottom();
}

// A choice question hides the answer box (and its Skip) behind "Other…", so
// Skip needs its own row alongside the options to stay reachable.
function buildSkipRow(onSkip) {
    const skipRow = el('div', 'step-ask-skip-row');
    const btn = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
    btn.type = 'button';
    btn.onclick = onSkip;
    skipRow.appendChild(btn);
    return skipRow;
}

// Records one answer as the step's `reason` — shared by the clarify card and
// by 🎓's optional follow-up question.
function submitStepAnswer(seq, question, answer, card, decisionId) {
    if (isBusy()) return;
    setBusy(true);
    fetch(LV.url.learning_answer_step_question, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            seq: seq, question: question, answer: answer,
            decision_id: decisionId || '',
        }),
    })
        .then(r => r.json())
        .then(d => {
            setBusy(false);
            card.remove();
            if (!d.success) { alert(d.message); return; }
            appendMsg('assistant', `❓ ${question}`, seq);
            appendMsg('user', answer, seq);
            if (d.usage && d.usage.session) updateTokenBadge(d.usage.session);
            if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
            if (d.assessment) applyAssessment(d.assessment);
            syncOpsQuiet(d); // updates the step's "✓ why" indicator (only if it was previously empty)
        })
        .catch(e => { setBusy(false); alert('Failed: ' + e); });
}

// ---- Export a skill from the rounds/chat gathered so far, open the Edit Skill modal ----
// Pre-export 防呆 gate: if readiness is still low, or the assessment flagged
// claims that aren't log-verified (or open contradictions), spell that out and
// let the engineer decide — export is a hint-gated action, never hard-blocked.
//
// /learning/converge can return MORE THAN ONE draft when the conversation
// actually covered several mutually-exclusive scenarios (see
// SYNTHESIS_SYS_PROMPT's split rule) — draftQueue/draftTotal step the
// engineer through each one's Edit Skill review/save in turn instead of only
// ever handling a single draft.
let draftQueue = [];
let draftTotal = 0;
// Notices about the export as a whole (depth refusal, multi-draft split).
// Shown as banners inside every draft's modal rather than as alert() dialogs
// fired before it opens.
let exportNotices = [];

// Single export — it follows the SESSION conversation mode already chosen via
// the header prior-knowledge toggle (state.prior_knowledge server-side), so there's no
// mode switch here. Always creates brand-new skills; splits into several when
// the chat spans distinct knowledge domains.
function exportSkill() {
    const btn = document.getElementById('exportSkillBtn');
    // Checks the GLOBAL lock so Export can't start while a baseline read, a
    // chat send, or a step teach/ask is still in flight, and vice versa.
    if (btn.disabled || isBusy()) return;

    // Two separate concerns, one dialog. Asked-but-unanswered decisions are
    // the export's business regardless of interview mode (optional follow-ups
    // opt out via blocking=false, see decision_ledger); readiness/claims come
    // from the last assessment. Splitting them across two stacked confirm()s
    // meant the first was gone from the screen before the second could be
    // weighed against it.
    const sections = [];
    const blockingDecisions = (decisionLedger.items || []).filter(
        item => item.status === 'open' && item.blocking
    );
    if (blockingDecisions.length) {
        sections.push({
            head: `Unresolved specification decisions (${blockingDecisions.length})`,
            note: 'They stay visible in the Skill Spec review.',
            items: blockingDecisions.map(item => item.question),
        });
    }
    if (currentAssessment) {
        const score = (currentAssessment.readiness || {}).score;
        const {conflicts, unconfirmed, filed, setAside} = claimBuckets(currentAssessment);
        const gaps = currentAssessment.gaps || [];
        if (typeof score === 'number' && score < 60) {
            sections.push({head: `Readiness is only ${score}%`,
                           note: 'The skill may be thin.', items: []});
        }
        if (conflicts.length) {
            sections.push({
                head: `Contradictions and open items (${conflicts.length})`,
                note: 'These conflict with the stats or with another answer.',
                items: conflicts.map(v => v.claim),
            });
        }
        if (unconfirmed.length) {
            sections.push({
                head: `Claims this log doesn't show (${unconfirmed.length})`,
                note: 'These export as domain knowledge, not proven fact.',
                items: unconfirmed.map(v => v.claim),
            });
        }
        if (gaps.length) {
            sections.push({head: `Open items still unanswered (${gaps.length})`,
                           items: gaps});
        }
        // Only alongside something else. Repeating what the engineer already
        // confirmed, on its own, would make acknowledging pointless — the
        // dialog would open every time regardless.
        if (filed.length && sections.length) {
            sections.push({
                head: `Expert knowledge you confirmed (${filed.length})`,
                note: 'Exporting as asserted, not proven — no action needed.',
                items: filed.map(v => v.claim),
            });
        }
        // Same rule: skipping is allowed to stop the nag, not to make the
        // claim disappear from the last look before it becomes a skill.
        if (setAside.length && sections.length) {
            sections.push({
                head: `Claims you set aside (${setAside.length})`,
                note: 'Never checked, and not filed as expert knowledge.',
                items: setAside.map(v => v.claim),
            });
        }
    }
    if (sections.length) { openExportGuard(sections); return; }
    runExport();
}

// The one dialog that used to be two confirm()s. Non-blocking, so the
// double-click race the old code had to guard against twice can't happen:
// runExport re-checks the lock at the moment it actually fires.
function openExportGuard(sections) {
    const body = document.getElementById('exportGuardBody');
    body.innerHTML = '';
    sections.forEach(sec => {
        body.appendChild(el('div', 'readiness-h', sec.head));
        if (sec.note) body.appendChild(el('div', 'export-guard-sub', sec.note));
        if (sec.items.length) {
            const ul = el('ul', 'readiness-gaps');
            sec.items.forEach(t => ul.appendChild(el('li', null, t)));
            body.appendChild(ul);
        }
    });
    const total = sections.reduce((n, s) => n + (s.items.length || 1), 0);
    document.getElementById('exportGuardSubtitle').textContent =
        `${total} thing${total > 1 ? 's' : ''} worth a second look before this becomes a skill`;
    document.getElementById('exportGuardGo').onclick = () => {
        closeExportGuard();
        runExport();
    };
    document.getElementById('exportGuardModal').style.display = 'flex';
}

function closeExportGuard() {
    document.getElementById('exportGuardModal').style.display = 'none';
}

function runExport() {
    const btn = document.getElementById('exportSkillBtn');
    if (btn.disabled || isBusy()) return;
    const label = btn.innerHTML;
    btn.disabled = true;
    setBusy(true);
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Exporting…';
    const restore = () => { btn.disabled = false; setBusy(false); btn.innerHTML = label; };
    fetch(LV.url.learning_converge, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({use_prior_knowledge: priorMode()}),
    })
        .then(r => r.json())
        .then(d => {
            restore();
            if (!d.success) { alert(d.message); return; }
            if (d.usage) updateTokenBadge(d.usage.session);
            if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
            draftQueue = (d.drafts || []).slice();
            draftTotal = draftQueue.length;
            if (!draftTotal) { alert('Nothing to export.'); return; }
            // A skill was loaded, so the draft was rebuilt on its framework
            // (server-side, see /learning/converge). Say what was separated
            // out BEFORE showing the result — a draft that silently grew from
            // 3 keywords to 21 looks like a bug unless the inheritance is
            // stated. With no skill loaded this block never runs and Export
            // behaves exactly as before.
            // Notices that belong to the EXPORT as a whole rather than to one
            // draft — carried into every draft's modal instead of being fired
            // as alert() dialogs before it opens.
            exportNotices = [];

            // A skill WAS loaded, but the chain had already reached its depth
            // limit, so this export deliberately came out standalone. Without
            // saying so it looks identical to having loaded nothing.
            if (d.lineage_blocked) {
                const lb = d.lineage_blocked;
                exportNotices.push({
                    kind: 'warn', icon: '⛔', title: `Not inherited from "${lb.parent_name}".`,
                    body: `That skill is already ${lb.parent_lineage.length} generation(s) deep and ` +
                          `this would make ${lb.child_depth} (limit ${lb.max_depth}). Each generation ` +
                          `copies all of its parent's keywords, so by this depth the filter matches ` +
                          `most of the log and stops narrowing anything down. Your knowledge is still ` +
                          `being exported, as a new standalone skill — if it really belongs to that ` +
                          `family, edit "${lb.parent_name}" directly instead.`,
                });
            }
            const inheritance = d.inheritance_summary || null;
            if (inheritance && inheritance.enabled && inheritance.parent_name && !d.lineage_blocked) {
                if (inheritance.inherited_drafts > 0) {
                    exportNotices.push({
                        kind: 'info', icon: '🌿', title: `Related knowledge inherits from "${inheritance.parent_name}".`,
                        body: `${inheritance.inherited_drafts} related draft(s) are new flattened children for Avatar; ` +
                              `${inheritance.standalone_drafts} unrelated knowledge-domain draft(s) remain standalone. ` +
                              'Each draft shows its own inherited-versus-new breakdown below.',
                    });
                } else {
                    exportNotices.push({
                        kind: 'info', icon: '↗️', title: `No draft inherited from "${inheritance.parent_name}".`,
                        body: 'Load skills was enabled and its document was considered, but the exported knowledge ' +
                              'was not an additive extension of that loaded skill, so it stays standalone.',
                    });
                }
            }
            if (draftTotal > 1) {
                exportNotices.push({
                    kind: 'info', icon: '🗂️', title: `${draftTotal} mutually-exclusive scenarios.`,
                    body: 'This session covered more than one, so they are reviewed and saved one at a time.',
                });
            }
            openNextDraft();
        })
        .catch(e => { restore(); alert('Export failed: ' + e); });
}

// Pop the next queued draft into the Edit Skill modal. Every draft is a
// brand-new skill (standalone or a new flattened child) — its `domain` (WiFi/BT) tells
// /learning/save which local file to file it in. When the LLM split the
// conversation into several distinct-domain skills, they're reviewed/saved
// one at a time via draftQueue.
function openNextDraft() {
    if (!draftQueue.length) return;
    const idx = draftTotal - draftQueue.length + 1;
    const draft = draftQueue.shift();
    currentDraft = draft;

    // Phase 2: each draft carries a `judge` field from learning_service.
    // route_draft — an ADVISORY add/merge/discard suggestion, never applied
    // without the engineer seeing and confirming it here. `source` tells
    // them WHY: "continuity" = this continues the skill they explicitly
    // loaded this session; "retrieval" = it matched an existing skill found
    // by the general domain-wide search.
    const judge = draft.judge || null;
    const countLabel = draftTotal > 1 ? ` (${idx} of ${draftTotal})` : '';
    let title, subtitle;
    // Export-wide notices first, then whatever is specific to THIS draft.
    const notices = exportNotices.slice();

    if (judge && judge.action === 'merge' && judge.target_skill_key) {
        title = 'Merge into Existing Skill' + countLabel;
        subtitle = `${judge.target_skill_name} (existing, v${judge.target_skill_version})`;
        const why = judge.source === 'continuity'
            ? 'Continues the skill you loaded this session'
            : 'Matched an existing skill';
        notices.push({
            kind: 'info', icon: '🔗', title: `${why}:`,
            body: `${judge.reason} New keywords/rules are highlighted green below — everything ` +
                  `else from "${judge.target_skill_name}" is kept. Prefer a fresh skill instead? ` +
                  `Clear the "Skill key" field before saving.`,
        });
    } else if (judge && judge.action === 'discard') {
        title = 'New Skill' + countLabel;
        subtitle = draft.name + ' — ⚠ possibly redundant, review before saving';
        notices.push({
            kind: 'warn', icon: '⚠️', title: 'This may duplicate an existing skill:',
            body: `${judge.reason} You can still save it as new if you disagree.`,
        });
    } else {
        title = 'New Skill' + countLabel;
        subtitle = draft.name + ' — review before saving';
    }
    // Phase 3: keywords/excludes the merge held BACK because this session's
    // actual filter stats measured zero marginal contribution (unique_hits/
    // dropped == 0) — never silently unioned in, but also never silently
    // dropped; the engineer sees exactly what was left out and why, and can
    // still add any of them back by hand via the modal's own "+" chip input.
    const heldBack = [...(draft.low_value_keywords || []), ...(draft.low_value_exclusive || [])];
    if (heldBack.length) {
        notices.push({
            kind: 'info', icon: 'ℹ️', title: 'Not auto-added — matched 0 unique lines this session.',
            body: 'Add any of these by hand below if you still want them:', list: heldBack,
        });
    }
    // Siblings this skill reads too much like. Avatar's agent chooses between
    // skills on the description line ALONE — it never sees keywords or rules at
    // selection time — so two that read alike get picked between arbitrarily.
    // Worth saying before Save, while the description is still one edit away.
    const siblings = draft.sibling_conflicts || [];
    if (siblings.length) {
        notices.push({
            kind: 'warn', icon: '🔀',
            title: 'Reads much like ' + (siblings.length === 1 ? 'an existing skill'
                                                              : siblings.length + ' existing skills'),
            body: 'Avatar picks between skills on the description alone, so these '
                + 'would compete. Say what this one covers that they do not — '
                + 'a runtime "stop if the other is running" rule cannot work.',
            list: siblings.map(s => `${s.name} (${Math.round(s.score * 100)}% alike): ${s.description}`),
        });
    }
    // `note` (if present) explains what distinct scenario a multi-draft
    // split isolates — surfaced alongside the judge's reasoning, if any.
    if (draft.note) notices.push({kind: 'info', icon: '📝', body: draft.note});

    SkillEditor.open(draft, {
        title: title,
        subtitle: subtitle,
        notices: notices,
        saveUrl: LV.url.learning_save,
        approvalMode: true,
        onSaved: function (d2) {
            // The exact file, not just "saved": which COPY of Copycat was
            // launched decides where this lands, and an engineer running a
            // second copy has no other way to notice.
            const more = draftQueue.length ? `\n\nNow reviewing the next one (${draftQueue.length} left)…` : '';
            if (d2.saved_to) {
                if (confirm(`Skill saved to:\n${d2.saved_to}\n\nOpen that folder?${more}`)) {
                    fetch('/skills/data_location/open', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({path: ''}),
                    }).then(r => r.json()).then(function (d3) {
                        if (!d3.success) alert(d3.message || 'Could not open that folder');
                    }).catch(function (e) { alert('Could not open that folder: ' + e); });
                }
            } else {
                alert('Skill saved to the local skill library.' + more);
            }
            // The just-saved skill becomes the session's baseline server-side
            // (learning_routes.save), but its keywords are NOT loaded into the
            // filter table — so this is exactly the divergent case the badge
            // exists for.
            activeSkillKey = d2.skill_key;
            activeSkillName = (currentDraft && currentDraft.name) || d2.skill_key;
            renderExportBaselineBadge();
            if (draftQueue.length) {
                openNextDraft();
                return;
            }
            // Export clears the still-missing list server-side; the readiness
            // score and the claim check survive it, so mirror that instead of
            // blanking the badge until a reload puts it back.
            applyAssessment(currentAssessment
                ? Object.assign({}, currentAssessment, {gaps: []}) : null);
            document.getElementById('readinessPanel').style.display = 'none';
        },
    });
}

// Close the step-tag dropdown when clicking outside the btn/dropdown area.
document.addEventListener('click', function(e) {
    const btn = document.getElementById('stepTagBtn');
    const dd = document.getElementById('stepTagDropdown');
    if (dd && btn && !btn.contains(e.target) && !dd.contains(e.target)) {
        dd.style.display = 'none';
    }
    const skillPicker = document.getElementById('skillDocPicker');
    if (skillPicker && !skillPicker.contains(e.target)) toggleSkillDocPicker(false);
    const loadPicker = document.getElementById('skillLoadPicker');
    if (loadPicker && !loadPicker.contains(e.target)) toggleSkillLoadPicker(false);
});

// A fixed-position menu doesn't travel with its anchor, so anything that moves
// the button has to be answered. Capture phase: the workbench's scrolling
// happens inside panes, not on the window, and those events don't bubble.
//
// Scrolling INSIDE the menu is the one case that must NOT close it — the skill
// list is taller than the popup and has its own scrollbar, so a plain capture
// listener made the menu vanish the moment you reached for a skill further
// down. Only movement of the anchor closes it.
document.addEventListener('scroll', (e) => {
    const menu = document.getElementById('skillLoadMenu');
    if (!menu || menu.hidden) return;
    if (e.target instanceof Node && menu.contains(e.target)) return;
    toggleSkillLoadPicker(false);
}, true);
window.addEventListener('resize', positionSkillLoadMenu);

document.addEventListener('scroll', (e) => {
    const menu = document.getElementById('skillDocPickerMenu');
    if (!menu || menu.hidden) return;
    if (e.target instanceof Node && menu.contains(e.target)) return;
    toggleSkillDocPicker(false);
}, true);
window.addEventListener('resize', positionSkillDocMenu);

renderFilters();
renderStepTagSelector();
renderStepPanel(); // now a permanent card (left column), not a toggled overlay
refreshSkillList(LV.boot.logDomain || 'wifi');
// Restore the event-log panel from server state on reload (BT only).
updateEvtSection(LV.boot.logDomain, LV.boot.hasEventLog);
// A reload of an in-progress session already has its files — start folded, the
// same as the moment right after picking them.
setLogBarCollapsed(true);
document.getElementById('dateSynthWarn').style.display = LV.boot.hasDateAnchor ? 'inline' : 'none';
// Log row clicked → highlight + scroll to the nearest event (reverse sync);
// only meaningful while the event panel is open. Delegated so it survives every
// renderLogRows() re-render.
document.getElementById('previewBox').addEventListener('click', function (e) {
    if (!_evtOpen) return;
    const row = e.target.closest('.tat-log-row[data-ms]');
    if (!row) return;
    jumpEvtToMs(+row.dataset.ms);
});
// The log pane only holds the rows on screen — scrolling (or resizing the
// pane) is what brings the rest in. See renderVisibleLogRows.
document.getElementById('previewBox').addEventListener('scroll', function () {
    renderVisibleLogRows(false);
});
window.addEventListener('resize', function () { renderVisibleLogRows(true); });
// Keyboard navigation, because the scrollbar cannot be precise at this scale
// (see scrollLogToRowTop). Only acts when the pane itself has focus, so these
// keys keep working normally everywhere else on the page — in particular
// PageUp/PageDown inside the chat box and Home/End inside a text field.
(function wireLogKeyboardNav() {
    const box = document.getElementById('previewBox');
    if (!box) return;
    box.addEventListener('keydown', (e) => {
        if (e.ctrlKey || e.altKey || e.metaKey || !vlog.rowH || !vlog.total) return;
        const rowsPerPage = Math.max(1, Math.floor(box.clientHeight / vlog.rowH) - 1);
        const top = Math.floor(box.scrollTop / vlog.rowH);
        let target = null;
        switch (e.key) {
            case 'ArrowDown':  target = top + 1; break;
            case 'ArrowUp':    target = top - 1; break;
            case 'PageDown':   target = top + rowsPerPage; break;
            case 'PageUp':     target = top - rowsPerPage; break;
            case 'Home':       target = 0; break;
            case 'End':        target = vlog.total - 1; break;
            default: return;
        }
        e.preventDefault();     // otherwise the PAGE scrolls as well as the pane
        scrollLogToRowTop(target);
    });
})();
(function wireLogGoto() {
    const input = document.getElementById('logGotoInput');
    if (!input) return;
    const go = () => {
        if (!input.value.trim()) return;
        gotoLogRow(input.value);
        // Focus moves to the pane so the arrows/PageUp keep working from where
        // you landed, instead of the next keystroke editing the number again.
        document.getElementById('previewBox').focus();
    };
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); go(); }
    });
    input.addEventListener('change', go);
})();
// The main chat box gets the same attach/paste/drop support the per-question
// answer boxes have — a config table screenshotted out of a spec is just as
// often taught as a free-form message as it is as an answer to a question.
// The returned object is read by sendMsg() (see _mainChatAttachments above
// it, near the top of the file) to resolve any staged image right before the
// message actually goes out.
(function wireMainChatAttachments() {
    const input = document.getElementById('chatInput');
    const attachBtn = document.getElementById('chatAttachBtn');
    const status = document.getElementById('chatAttachStatus');
    if (!input || !attachBtn || !status) return;
    const footer = input.closest('.card-footer') || input.parentElement;
    _mainChatAttachments = wireAttachments({
        input,
        attachBtn,
        dropZone: footer,
        grow: () => autoGrowChatInput(input),
        setStatus: (text, kind) => {
            status.className = 'chat-q-attach-status' + (kind ? ' is-' + kind : '');
            status.textContent = text || '';
            status.hidden = !text;
        },
    });
    footer.appendChild(_mainChatAttachments.picker);
    // Chips go above the input row (with the status line), not inside
    // .chat-input-group — that row is a fixed-height strip of controls
    // (attach/textarea/tag/send), not a place for a wrapping list of chips.
    status.insertAdjacentElement('beforebegin', _mainChatAttachments.pendingRow);
})();
// Scrolling up to re-read the Filtered Log while mid-sentence used to carry
// whatever box the engineer was typing into off the bottom of the screen. Any
// box that holds a text field pins itself to the viewport for as long as it
// has focus; a same-height spacer holds its place in the flow, which is also
// what keeps the "would it be off screen?" test stable and stops this
// oscillating. Delegated rather than wired per node: question cards arrive
// long after load and are removed the moment they're answered.
(function wireTypingDock() {
    const DOCKABLE = '.card-footer, .chat-question-card';
    let node = null;      // the box currently pinned
    let spacer = null;

    function release() {
        if (!node) return;
        node.classList.remove('is-docked');
        node.style.left = node.style.width = '';
        spacer.remove();
        node = spacer = null;
    }

    function sync() {
        if (node && !node.isConnected) release();   // card answered and removed
        const active = document.activeElement;
        const host = active && active.closest ? active.closest(DOCKABLE) : null;
        const isField = active && (active.tagName === 'TEXTAREA' ||
            (active.tagName === 'INPUT' && !/^(hidden|file|checkbox|radio)$/.test(active.type)));
        // Docking only ever STARTS from a text field, but survives focus
        // moving to a button in the same box — otherwise clicking Send would
        // unpin it out from under the click.
        if (!host || (!isField && host !== node)) { release(); return; }
        // Once pinned the node is out of flow, so the spacer is the only thing
        // that still knows where it would have been.
        const flow = (host === node ? spacer : host).getBoundingClientRect();
        if (flow.bottom <= window.innerHeight) { release(); return; }
        if (host !== node) {
            release();
            node = host;
            spacer = document.createElement('div');
            spacer.className = 'typing-dock-spacer';
            node.parentNode.insertBefore(spacer, node);
        }
        // Re-measured every sync: the textarea auto-grows, and dropdowns and
        // option lists open inside these boxes.
        spacer.style.height = node.offsetHeight + 'px';
        const at = spacer.getBoundingClientRect();
        node.classList.add('is-docked');
        node.style.left = at.left + 'px';
        node.style.width = at.width + 'px';
    }
    ['focusin', 'input', 'click'].forEach(ev => document.addEventListener(ev, sync));
    // Defer: activeElement is momentarily <body> between blur and the next focus.
    document.addEventListener('focusout', () => setTimeout(sync, 0));
    document.addEventListener('scroll', sync, {passive: true, capture: true});
    window.addEventListener('resize', sync);
})();
// Restore the stable Question context. Skill options arrive from the
// domain-scoped /skills/list request above. caseSummaryText is already seeded
// from LV.boot at declaration; a RELOAD mid-session must not re-interrogate
// the engineer, so the intake card is only shown when there is a log but no
// framing recorded yet. A session that already has one gets the recap note
// instead, so the transcript still states what Copycat is working from.
renderSkillDocPicker();
updateQuestionContextStatus();
updateDecisionLedger(decisionLedger);
if (LV.boot.logPath) {
    if (caseSummaryText) appendCaseContextNote();
    else renderCaseIntakeCard();
}
// A baseline chosen in the Skill Library survives a reload, so the badge has
// to be evaluated on load too, not only when it changes.
renderExportBaselineBadge();
if (LV.boot.sessionUsage) updateTokenBadge(LV.boot.sessionUsage);
if (LV.boot.hasAssessment) {
applyAssessment({
    readiness: LV.boot.lastReadiness,
    coverage: LV.boot.lastCoverage,
    gaps: LV.boot.lastGaps,
    validation: LV.boot.lastValidation,
    new_gaps: LV.boot.lastNewGaps,
    // Both counters run from the start of the session, so a reload without
    // these would bill the whole session to the next round.
    settled: LV.boot.lastSettled,
    raised: LV.boot.lastRaised,
});
}
// Replay any chat history from a prior page load through appendMsg (rather
// than server-rendering it as plain text) so it gets the same markdown
// rendering + step-tag badge as live messages. Older sessions saved before
// message tagging existed have no `step` field — appendMsg's stepTag param
// is `undefined` for those (not "all"), which correctly renders NO badge
// rather than mislabeling old messages as general knowledge they were never
// actually confirmed to be.
(LV.boot.chatHistory).forEach(m => appendMsg(m.role, m.content, m.step));

// The log rows are NOT part of the boot payload — sending a 400k-line preview
// through the page would be absurd — so a reload used to leave the pane at
// "Log (0 of 0)" and blank even though the session still had a log and a full
// filter set, which read as the log having been lost. Re-run the filter once
// on load to repaint it from the server's own state. Deliberately does NOT
// trigger a baseline read: that is now an explicit, engineer-pressed action
// (see setBaselineGate).
if (LV.boot.logPath && filterData.length) {
    applyFilter();
} else if (LV.boot.logPath) {
    showAllLog();
}
setBaselineGate();
