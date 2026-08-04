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
// What the next Export inherits from. NOT the same thing as the skill in the
// dropdown above the filter table (that one is filter_skill_key — whose
// keywords are actually on screen). They coincide when a skill is loaded from
// the Log Viewer, and diverge when a baseline is picked from the Skill
// Library, which deliberately leaves the filters alone.
let activeSkillKey = LV.boot.activeSkillKey;
let activeSkillName = LV.boot.baselineSkillName;
let currentQuestions = [];
let currentDraft = null;
let decisionLedger = LV.boot.decisionLedger || {mode: 'ask', items: [], open: 0, resolved: 0, deferred: 0, blocking: 0};
let interviewMode = LV.boot.interviewMode || 'ask';
// Fixed UTC offset (minutes) of the machine that captured the current BT log
// (see event_log_service.find_capture_utc_offset_minutes) — null means none
// was found, so the event<->log click-sync applies no correction. Updated
// whenever a log or event log is (re)picked — see pickLog()/pickEventLog().
let captureUtcOffsetMin = LV.boot.captureUtcOffsetMin;

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

function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
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
function renderStepTagSelector() {
    const dropdown = document.getElementById('stepTagDropdown');
    const btn = document.getElementById('stepTagBtn');
    if (!dropdown || !btn) return;
    if (currentStepTag !== 'all' && !operationData.some(o => o.seq === currentStepTag)) {
        currentStepTag = 'all';
    }
    btn.textContent = currentStepTag === 'all' ? 'All' : '#' + currentStepTag;

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
    operationData.forEach(o => {
        if (o.action === 'load_skill' || o.action === 'load_tat') return;
        dropdown.appendChild(mkPill(o.seq, '#' + o.seq, `Tag as about step #${o.seq} (${o.verb} "${o.label || o.text}")`));
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

    const customBox = el('div', 'chat-q-custom');
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control form-control-sm';
    input.placeholder = 'Type your answer… (or Dismiss if not relevant)';
    const sendBtn = el('button', 'btn btn-sm btn-primary');
    sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i>';
    const dismissBtn = el('button', 'btn btn-sm btn-outline-secondary', 'Dismiss');

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
    input.onkeypress = (e) => { if (e.key === 'Enter' && input.value.trim()) submit(input.value.trim()); };
    sendBtn.onclick = () => { if (input.value.trim()) submit(input.value.trim()); };
    dismissBtn.onclick = () => { card.remove(); if (opts.afterSubmit) opts.afterSubmit(); };

    customBox.appendChild(input);
    customBox.appendChild(sendBtn);
    customBox.appendChild(dismissBtn);
    card.appendChild(customBox);
    box.appendChild(card);
    box.scrollTop = box.scrollHeight;
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
    operationData.forEach((o, i) => {
        const from = o.chat_index;
        const to = (i + 1 < operationData.length) ? operationData[i + 1].chat_index : chatHistoryMirror.length;
        const count = Math.max(0, to - from);
        const item = el('button', 'step-item' + (o.excluding ? ' is-exclude' : ''));
        item.type = 'button';
        item.appendChild(el('span', 'step-seq', '#' + o.seq));
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
        if (o.action !== 'load_skill' && o.action !== 'load_tat') {
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
        body.appendChild(row);
    });
}

function jumpToStep(fromIdx, toIdx) {
    const box = document.getElementById('chatBox');
    // Clear any previous highlight.
    box.querySelectorAll('.step-highlight').forEach(n => n.classList.remove('step-highlight'));
    const kids = box.children;
    if (fromIdx >= kids.length) {
        // Nothing was said yet at this point — scroll to end so it's obvious.
        box.scrollTop = box.scrollHeight;
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

// Repopulate the skill <select> for the given domain ('wifi' | 'bt') and
// show a small badge so it's clear which skill set is in play.
function refreshSkillList(domain) {
    fetch(`/skills/list?domain=${domain}`).then(r => r.json()).then(d => {
        if (!d.success) return;
        const sel = document.getElementById('skillSelect');
        const current = sel.value;
        sel.innerHTML = '<option value="">-- or load a learned skill --</option>' +
            d.skills.map(s => `<option value="${s.key}">${escapeHtml(s.name)}</option>`).join('');
        if (d.skills.some(s => s.key === current)) sel.value = current;

        const badge = document.getElementById('domainBadge');
        badge.textContent = domain === 'bt' ? 'BT' : 'WiFi';
        badge.className = 'domain-badge ' + (domain === 'bt' ? 'domain-bt' : 'domain-wifi');
        badge.style.display = 'inline-block';
    });
}

// Render the log (raw, before any filter — or filtered, once one has run)
// as TAT-style rows: sticky line-number gutter + column-aligned, optionally
// colored text.
function renderLogRows(preview, emptyMessage) {
    const box = document.getElementById('previewBox');
    // Size the line-number gutter to the WIDEST line number in this preview so
    // every row's gutter is identical and the timestamp column starts at the
    // same x on every line (4- vs 5- vs 6-digit numbers no longer drift the
    // row). +0.5ch of slack, floored at 4ch so short files still look right.
    const maxDigits = preview.reduce(
        (m, p) => Math.max(m, p.line_no != null ? String(p.line_no).length : 0), 0);
    box.style.setProperty('--lineno-w', Math.max(4, maxDigits + 0.5) + 'ch');
    // Rows sit inside .tat-log-inner (display:inline-block) instead of
    // directly in the scroll container — an inline-block shrink-wraps to
    // whatever the WIDEST row's content actually needs, and every row inside
    // it is width:100% of THAT shared width. Without this wrapper each row's
    // own width:auto only ever resolved against the viewport, so a short
    // line's colored .tat-log-text span (flex:1 0 auto now, see CSS) had
    // nothing wide to grow into and its color band stopped right where the
    // text did — full-length color bars only ever appeared for the single
    // longest line on screen.
    const rowsHtml = preview.map(p => {
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
    }).join('');
    box.innerHTML = rowsHtml
        ? `<div class="tat-log-inner">${rowsHtml}</div>`
        : `<div class="tat-log-empty">${emptyMessage || 'Empty file'}</div>`;
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
// SHIFTED by captureUtcOffsetMin so it lands in the same frame as
// parseLogTimeMs()'s customer-local driver-log timestamps (e.g. +480 for a
// UTC+08:00 capture) — without this shift the two were compared as raw
// numbers despite being genuinely different moments, silently landing
// "nearest" on a plausible-looking but wrong line. null offset (not found)
// applies no correction, same as before.
function parseEvtTimeMs(text) {
    const m = String(text).match(/^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})/);
    if (!m) return null;
    let ms = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime();
    if (typeof captureUtcOffsetMin === 'number') ms += captureUtcOffsetMin * 60000;
    return ms;
}

function pickLog() {
    fetch(LV.url.log_viewer_pick_log, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (d.success) {
                baselineDone = false;
                document.getElementById('logPathInput').value = d.log_path;
                showPathStatus('logStatus', '✔ Log loaded', 'ok');
                if (d.domain) refreshSkillList(d.domain);

                // Show the log immediately — don't make the engineer wait
                // for a .tat/skill to be loaded just to see the file.
                document.getElementById('logPaneTitle').textContent = 'Log';
                lastMatchCount = d.preview.length;
            document.getElementById('matchCount').textContent = d.preview.length;
                document.getElementById('totalLines').textContent = d.total_lines;
                document.getElementById('statsSummary').textContent =
                    d.total_lines > d.preview.length
                        ? `no filter applied yet — first ${d.preview.length} of ${d.total_lines} lines`
                        : 'no filter applied yet';
                renderLogRows(d.preview);
                // A new log always clears any focus window server-side (see
                // /pick_log) — an issue time from the OLD file's frame could
                // silently slice this one down to zero lines. Reset the UI too.
                document.getElementById('focusTimeInput').value = '';
                setFocusUiState(null);
                // BT capture → reveal the event panel (auto-discovered file
                // enables it; WiFi/none hides it). Reset any open state first.
                captureUtcOffsetMin = d.capture_utc_offset_min;
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
            lastMatchCount = d.preview.length;
            document.getElementById('matchCount').textContent = d.preview.length;
            document.getElementById('totalLines').textContent = d.total_lines;
            document.getElementById('statsSummary').textContent =
                d.total_lines > d.preview.length
                    ? `no filter applied — first ${d.preview.length} of ${d.total_lines} lines`
                    : '';
            renderLogRows(d.preview);
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
            const first = document.querySelector('#previewBox .tat-log-text');
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
    const first = document.querySelector('#previewBox .tat-log-text');
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
                lastMatchCount = d.preview.length;
            document.getElementById('matchCount').textContent = d.preview.length;
                document.getElementById('totalLines').textContent = d.total_lines;
                renderLogRows(d.preview);
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
    const timePart = focus.center.split('-').pop(); // HH:MM:SS(.mmm) portion
    badge.textContent = `🎯 ±${focus.window_min || 5}m around ${timePart}`;
    badge.title = 'Only this window is being scanned. Click to clear.';
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
    updateEvtTzBadge();
    // A freshly (re)loaded log invalidates any cached events.
    _evtLoaded = false; _evtData = []; _evtOffset = 0; _evtAutoPending = true;
    if (_evtOpen && !available) _closeEvtPanel();
}

// Reflects whether the event<->log click-sync has a UTC-offset correction
// (see captureUtcOffsetMin / parseEvtTimeMs) — makes it visible when the
// "nearest" jump can actually be trusted vs. when it's an uncorrected raw
// comparison between UTC and customer-local time.
function updateEvtTzBadge() {
    const badge = document.getElementById('evtTzBadge');
    if (!badge) return;
    if (typeof captureUtcOffsetMin === 'number') {
        const sign = captureUtcOffsetMin >= 0 ? '+' : '-';
        const abs = Math.abs(captureUtcOffsetMin);
        const hhmm = `${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`;
        badge.textContent = `🌐 synced UTC${sign}${hhmm}`;
        badge.title =
            `Event times are UTC (Windows stores TimeCreated/@SystemTime in UTC regardless of `
            + `what Event Viewer displays). They are shifted by the capture machine's own fixed `
            + `offset UTC${sign}${hhmm}, read from systeminfo.txt beside the log, which puts them `
            + `in the same frame as the driver log's timestamps.

`
            + `Assumption: the driver log carries CAPTURE-MACHINE local time. That is established `
            + `for BT captures. For WiFi it is currently assumed, not verified — if a WiFi log `
            + `turns out to be in the analysing engineer's timezone instead, the jump will be off `
            + `by the difference between the two machines.`;
        badge.classList.add('evt-tz-ok');
        badge.classList.remove('evt-tz');
    } else {
        badge.textContent = '⚠ times are UTC';
        badge.title = 'Event times are UTC; the driver log below is customer-local and no capture timezone was found (no systeminfo.txt near the log), so the jump lands on the nearest line with no offset correction and may be off.';
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
            captureUtcOffsetMin = d.capture_utc_offset_min;
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
    if (typeof captureUtcOffsetMin !== 'number') return '';
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
        <col style="width:150px;"><col style="width:26px;"><col style="width:90px;"><col style="width:48px;">
      </colgroup><thead><tr>
        <th>${escapeHtml(timeHeader)}</th><th>Lv</th><th>Source</th><th>ID</th>
      </tr></thead><tbody>`;
    for (let i = 0; i < _evtData.length; i++) {
        const ev = _evtData[i];
        const dot = (ev.level === 'Error' || ev.level === 'Critical') ? '🔴'
            : ev.level === 'Warning' ? '🟡' : '🟢';
        const localLabel = _evtLocalTimeLabel(ev.time);
        html += `<tr data-evt-idx="${i}">
            <td>${escapeHtml(ev.time)}${localLabel ? `<div class="evt-time-local">→ ${escapeHtml(localLabel)} local</div>` : ''}</td>
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
// view within the log pane, and flash it. Returns that row (or null).
function jumpLogToMs(ms) {
    const box = document.getElementById('previewBox');
    let best = null, bestDiff = Infinity;
    box.querySelectorAll('.tat-log-row[data-ms]').forEach(function (row) {
        const diff = Math.abs(+row.dataset.ms - ms);
        if (diff < bestDiff) { bestDiff = diff; best = row; }
    });
    if (!best) return null;
    // Scroll within the pane only (not the whole page).
    box.scrollTop = best.offsetTop - box.clientHeight / 2 + best.offsetHeight / 2;
    box.querySelectorAll('.tat-log-row.is-time-hit').forEach(r => r.classList.remove('is-time-hit'));
    best.classList.add('is-time-hit');
    return best;
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
    if (localLabel) h += `<tr><th>Local</th><td>${escapeHtml(localLabel)} — capture machine / driver-log frame</td></tr>`;
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

// The export baseline is only worth showing when it DIFFERS from what the
// dropdown is already showing — i.e. it was picked in the Skill Library,
// which sets the baseline without touching the filters. When the two agree
// (the normal case: a skill loaded right here), the dropdown already says it
// and a second indicator would just be noise.
function renderExportBaselineBadge() {
    const badge = document.getElementById('exportBaselineBadge');
    if (!badge) return;
    const onScreen = document.getElementById('skillSelect').value || '';
    if (!activeSkillKey || activeSkillKey === onScreen) {
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

// Only ever fires from the skillSelect dropdown's own onchange — and that
// dropdown is itself marked data-busy-lock, so it's physically un-clickable
// while anything else is in flight. The isBusy() guard here is defense in
// depth against a change event that somehow lands anyway (e.g. a keyboard-
// driven selection racing a fetch); either way the skill choice can only ever
// change from a deliberate, isolated user action, never mid-flight.
function loadSkill(key) {
    if (!key || isBusy()) return;
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
            //
            // Set the dropdown explicitly rather than assuming it already
            // holds `key`. That is only true when this ran from its own
            // onchange; any other caller would leave it stale, and the badge
            // decides what to show by comparing against exactly this value.
            const sel = document.getElementById('skillSelect');
            if (sel.value !== key) sel.value = key;
            activeSkillKey = key;
            activeSkillName = (sel.options[sel.selectedIndex] || {}).textContent || key;
            renderExportBaselineBadge();
            // Loading a named skill auto-switches the conversation into
            // PRIOR-knowledge mode server-side (see log_viewer_routes.
            // load_skill) — sync the header toggle so the UI matches, and the
            // engineer sees why the interview stops asking about content this
            // skill already covers.
            if (d.prior_knowledge) {
                const t = document.getElementById('priorToggle');
                if (t && !t.checked) t.checked = true;
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
    box.innerHTML = '<div class="tat-log-empty">Running filter…</div>';
    return fetch(LV.url.log_viewer_apply_filter, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (!d.success) { box.innerHTML = ''; alert(d.message); return; }
            document.getElementById('logPaneTitle').textContent = 'Filtered Log';
            lastMatchCount = d.total_matched;
            document.getElementById('matchCount').textContent = d.total_matched;
            document.getElementById('totalLines').textContent = d.total_lines;
            document.getElementById('statsSummary').textContent =
                `${d.overlap_count} lines matched 2+ keywords` +
                (d.focus ? ` — scanned ${d.total_lines} of ${d.full_total_lines} lines in file` : '');
            setFocusUiState(d.focus);

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
            renderLogRows(d.preview, 'No lines matched');
            // Last, so a clarifying question never delays the filter result
            // the engineer is actually waiting to look at.
            maybeClarify();
        });
}

// Debounce rapid successive toggles (e.g. clicking through several
// checkboxes, or "All"/"None" which flips dozens at once) into a single re-run.
let _filterDebounceTimer = null;
function debounceApplyFilter() {
    if (!document.getElementById('logPathInput').value.trim()) return; // nothing to filter yet
    if (_filterDebounceTimer) clearTimeout(_filterDebounceTimer);
    _filterDebounceTimer = setTimeout(() => applyFilter(), 350);
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
    const seq = m[1];
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
    head.innerHTML = `<i class="fas fa-brain"></i> Step #${seq} knowledge core`;
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
function appendMsg(role, content, stepTag) {
    const box = document.getElementById('chatBox');
    const isUser = (role === 'user');
    const row = el('div', 'chat-row ' + (isUser ? 'chat-row-user' : 'chat-row-assistant'));
    const bubble = el('div', 'chat-bubble ' + (isUser ? 'chat-bubble-user' : 'chat-bubble-assistant'));

    const tag = (stepTag === undefined) ? null : stepTag;
    if (tag !== null) {
        const meta = el('div', 'chat-bubble-meta');
        const isAll = (tag === 'all');
        const badge = el('span', 'chat-step-badge' + (isAll ? ' is-all' : ''), isAll ? 'All' : ('#' + tag));
        badge.title = isAll ? 'General / session-wide knowledge' : `About step #${tag}`;
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
    box.scrollTop = box.scrollHeight;
    chatHistoryMirror.push({role, content, step: tag});
    return row;
}

// presetMsg: sent to the LLM. displayMsg: optional short label shown in the
// bubble instead of a long preset instruction (keeps the auto-kickoff tidy).
// Small, always-visible session token counter (see services/llm_service.py
// LLM_helper._record_usage — same numbers also print to the terminal on
// every call). Session-cumulative, not per-call, since that's the more
// useful "is this getting expensive" signal while iterating on a skill.
// Claude Sonnet 6 bills input (prompt) and output (completion) tokens at
// DIFFERENT US-dollar rates — adjust these two constants if the contract
// rate changes. Units: USD per 1,000,000 tokens.
const USD_PER_MTOK_IN = 3.0;    // input / prompt tokens
const USD_PER_MTOK_OUT = 15.0;  // output / completion tokens
// Last session usage {prompt_tokens, completion_tokens, total_tokens, calls},
// kept so the Spending popover can recompute the USD breakdown on demand
// without a refetch.
let lastSessionUsage = null;

function updateTokenBadge(sessionUsage) {
    if (!sessionUsage) return;
    lastSessionUsage = sessionUsage;
    const badge = document.getElementById('tokenBadge');
    const usd = spendUsd(sessionUsage);
    badge.textContent = `🪙 $${usd.total.toFixed(4)}`;
    badge.title = `${sessionUsage.total_tokens} tokens across ${sessionUsage.calls} LLM call(s) this session — click for the breakdown`;
    // Keep the spending popover live if it's currently open.
    const sp = document.getElementById('spendPanel');
    if (sp && sp.style.display !== 'none') renderSpendPanel();
}

// tiny token formatter (1234 -> "1,234")
function fmtTok(n) { return (n || 0).toLocaleString('en-US'); }

// Shared USD math so the badge and the popover never drift apart.
function spendUsd(u) {
    const inTok = (u && u.prompt_tokens) || 0;
    const outTok = (u && u.completion_tokens) || 0;
    const usdIn = inTok / 1e6 * USD_PER_MTOK_IN;
    const usdOut = outTok / 1e6 * USD_PER_MTOK_OUT;
    return {inTok, outTok, usdIn, usdOut, total: usdIn + usdOut};
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

// Spending popover — token spend split into input vs output (each at its own
// Claude Sonnet 6 USD rate, shown as the actual conversion formula) plus the
// combined total. Built via the DOM so it stays consistent with the
// readiness popover's building style.
function renderSpendPanel() {
    const p = document.getElementById('spendPanel');
    p.innerHTML = '';
    const u = lastSessionUsage || {prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, calls: 0};
    const {inTok, outTok, usdIn, usdOut, total} = spendUsd(u);
    const totTok = u.total_tokens || (inTok + outTok);

    p.appendChild(el('div', 'readiness-panel-title', 'Spending'));
    const table = el('div', 'spend-table');
    table.appendChild(spendLine('Input', `${fmtTok(inTok)} tok x $${USD_PER_MTOK_IN.toFixed(2)}/1M`, '$' + usdIn.toFixed(4)));
    table.appendChild(spendLine('Output', `${fmtTok(outTok)} tok x $${USD_PER_MTOK_OUT.toFixed(2)}/1M`, '$' + usdOut.toFixed(4)));
    table.appendChild(el('div', 'spend-divider'));
    table.appendChild(spendLine('Total', `${fmtTok(totTok)} tok`, '$' + total.toFixed(4), 'spend-total'));
    p.appendChild(table);
    p.appendChild(el('div', 'spend-sub', `${u.calls || 0} LLM call(s) this session · Claude Sonnet 6`));
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
// clarification follow-up answers (see showNextQuestionCard), which are always
// about the whole round, not whatever step the selector happens to be on.
let pendingBaselineSend = null;
let baselineGuardReturnFocus = null;

function showBaselineGuard(presetMsg, displayMsg, forceTag, decisionId) {
    pendingBaselineSend = {presetMsg, displayMsg, forceTag, decisionId};
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
    sendMsg(pending.presetMsg, pending.displayMsg, pending.forceTag, true, pending.decisionId);
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

function sendMsg(presetMsg, displayMsg, forceTag, allowWithoutBaseline, decisionId) {
    // chatSendBtn stays enabled while busy (see setBusy) so this same click
    // handler doubles as Stop — everything else with data-busy-lock is
    // disabled, so isBusy() here only ever means "the button itself was
    // clicked again," never a stray programmatic call racing a live request.
    if (isBusy()) { stopChatSend(); return; }
    const input = document.getElementById('chatInput');
    const msg = presetMsg !== undefined ? presetMsg : input.value.trim();
    if (!msg) return;
    if (!baselineDone && !allowWithoutBaseline) {
        showBaselineGuard(presetMsg, displayMsg, forceTag, decisionId);
        return;
    }
    const tag = forceTag !== undefined ? forceTag : currentStepTag;
    appendMsg('user', displayMsg !== undefined ? displayMsg : msg, tag);
    if (presetMsg === undefined) input.value = '';
    setBusy(true);
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
        })
    }).then(r => r.text()).then(text => {
        const d = _parseStreamDone(text);
        if (!d) { appendMsg('assistant', '⚠️ Empty or malformed response from server.', tag); return; }
        if (d.success) {
            appendMsg('assistant', d.reply, d.step_tag !== undefined ? d.step_tag : tag);
            if (d.clarification) renderProactiveClarification(d.clarification, tag);
        } else appendMsg('assistant', '⚠️ ' + d.message, tag);
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
    }).finally(() => { setBusy(false); _activeChatAbort = null; });
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
    appendRecommendation(card, q);

    const submit = (answer) => {
        card.remove();
        const contextualAnswer = `Clarification question: ${q.question}\nEngineer answer: ${answer}`;
        sendMsg(contextualAnswer, answer, stepTag, false, q.decision_id);
    };
    const customBox = el('div', 'chat-q-custom');
    if (q.type === 'choice' && q.options && q.options.length >= 2) {
        customBox.style.display = 'none';
        const optsBox = el('div', 'chat-q-opts');
        q.options.forEach((opt) => {
            const btn = el('button', 'chat-q-opt', opt);
            btn.type = 'button';
            btn.onclick = () => submit(opt);
            optsBox.appendChild(btn);
        });
        const other = el('button', 'chat-q-opt chat-q-opt-other', 'Other…');
        other.type = 'button';
        other.onclick = () => {
            optsBox.style.display = 'none';
            customBox.style.display = 'flex';
            customBox.querySelector('input').focus();
        };
        optsBox.appendChild(other);
        card.appendChild(optsBox);
    } else {
        customBox.style.display = 'flex';
    }

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control form-control-sm';
    input.placeholder = 'Type the missing rule or reason…';
    input.onkeypress = (e) => {
        if (e.key === 'Enter' && input.value.trim()) submit(input.value.trim());
    };
    const send = el('button', 'btn btn-sm btn-primary');
    send.type = 'button';
    send.innerHTML = '<i class="fas fa-paper-plane"></i>';
    send.onclick = () => { if (input.value.trim()) submit(input.value.trim()); };
    const skip = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
    skip.type = 'button';
    skip.onclick = () => { deferDecision(q.decision_id); card.remove(); };
    customBox.appendChild(input);
    customBox.appendChild(send);
    customBox.appendChild(skip);
    card.appendChild(customBox);
    box.appendChild(card);
    box.scrollTop = box.scrollHeight;
}

// Live re-assessment after each chat answer: updates the readiness badge +
// the 防呆 details panel from the whole conversation so far. Cheap, standalone
// (no new analysis/questions), and a no-op until a filter has been run.
function refreshAssessment() {
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
        .catch(() => {}); // a failed background assess shouldn't disrupt the chat
}

function resetChat() {
    return fetch(LV.url.chatbot_reset, {method: 'POST'}).then(() => {
        document.getElementById('chatBox').innerHTML = '';
        // Server-side reset_teaching_progress() clears state.operations too
        // — mirror that here so the Steps panel doesn't go stale against
        // filter edits that no longer exist server-side.
        operationData = [];
        decisionLedger = {mode: interviewMode, items: [], open: 0, resolved: 0, deferred: 0, blocking: 0};
        updateDecisionLedger(decisionLedger);
        renderStepPanel();
    });
}

// The most recent assessment {readiness, coverage, gaps, validation}, kept
// client-side so the Export gate and the details panel can read it without a
// refetch. Updated by the baseline read and by every chat answer.
let currentAssessment = null;

// Readiness score badge next to Export Skill — color band matches the
// readiness guide in services/learning_service.py (_ASSESS_TASKS).
function updateReadinessBadge(readiness) {
    const badge = document.getElementById('readinessBadge');
    if (!readiness || typeof readiness.score !== 'number') {
        badge.textContent = '🎯 Readiness —';
        badge.classList.remove('is-low', 'is-mid', 'is-high');
        return;
    }
    const score = readiness.score;
    badge.textContent = `🎯 Readiness ${score}%`;
    badge.classList.remove('is-low', 'is-mid', 'is-high');
    badge.classList.add(score >= 70 ? 'is-high' : score >= 35 ? 'is-mid' : 'is-low');
}

// Single entry point for a fresh assessment (from /assess or /log_round):
// updates the badge and re-renders the details panel, and flags the badge
// when there are unverified/contradiction items so the engineer notices
// before exporting.
function applyAssessment(a) {
    currentAssessment = a || null;
    updateReadinessBadge(a && a.readiness);
    renderReadinessPanel(a);
    const badge = document.getElementById('readinessBadge');
    const flags = (a && a.validation || []).filter(v => v.status !== 'verified').length;
    badge.textContent = badge.textContent.replace(/ ⚠.*$/, '');
    if (flags) badge.textContent += ` ⚠${flags}`;
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
// Now floating popovers (anchored under the Spending / Readiness columns,
// bottom-left) instead of inline flex siblings of chatBox — close both on
// any click outside the session-menu.
document.addEventListener('click', function(e) {
    const menu = document.querySelector('.session-menu');
    if (!menu || menu.contains(e.target)) return;
    const rp = document.getElementById('readinessPanel');
    const sp = document.getElementById('spendPanel');
    if (rp) rp.style.display = 'none';
    if (sp) sp.style.display = 'none';
});

const _COVER_LABELS = {knowledge: 'Knowledge & rules', scope: 'Scope (non-overlapping)', keywords: 'Minimal keywords', evidence: 'Evidence (labeled lines)'};
const _VALID_META = {
    verified:      {icon: '✅', cls: 'v-ok',   label: 'verified from log'},
    asserted:      {icon: '⚠️', cls: 'v-warn', label: 'stated, not log-verified'},
    contradiction: {icon: '⛔', cls: 'v-bad',  label: 'contradiction / open item'},
};

// The readiness popover, trimmed down: just the per-goal coverage bars and a
// Detail button. Everything fuller (the still-missing gaps and the claim-by-
// claim verified-vs-asserted validation) now lives behind that button in the
// Readiness detail popup (openReadinessDetail) so this popover stays short.
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

    // Detail button — opens the fuller gaps + claim-check popup.
    const n = (a.gaps || []).length + (a.validation || []).length;
    const btn = el('button', 'readiness-detail-btn', n ? `Detail (${n})` : 'Detail');
    btn.type = 'button';
    btn.onclick = (e) => { e.stopPropagation(); openReadinessDetail(); };
    body.appendChild(btn);
}

// Readiness detail popup — the still-missing gaps + claim-by-claim validation,
// moved out of the compact popover behind its Detail button. Reads the latest
// currentAssessment; built via the DOM so LLM-authored claim text can't break
// markup.
function openReadinessDetail() {
    const a = currentAssessment || {};
    const gaps = a.gaps || [];
    const validation = a.validation || [];
    const body = document.getElementById('rdmBody');
    body.innerHTML = '';

    const sub = document.getElementById('rdmSubtitle');
    sub.textContent = (a.readiness && typeof a.readiness.score === 'number')
        ? `${a.readiness.score}% ready` : '';

    if (!gaps.length && !validation.length) {
        body.appendChild(el('div', 'readiness-empty', 'Nothing flagged yet — log a round or answer a question.'));
    }
    if (gaps.length) {
        body.appendChild(el('div', 'readiness-h', `Still missing (${gaps.length})`));
        const ul = el('ul', 'readiness-gaps');
        gaps.forEach(g => ul.appendChild(el('li', null, g)));
        body.appendChild(ul);
    }
    if (validation.length) {
        body.appendChild(el('div', 'readiness-h', `Claim check (${validation.length})`));
        const list = el('div', 'readiness-valid');
        validation.forEach(v => {
            const meta = _VALID_META[v.status] || _VALID_META.asserted;
            const item = el('div', 'valid-item ' + meta.cls);
            item.appendChild(el('span', 'valid-icon', meta.icon));
            const vbody = el('div', 'valid-body');
            vbody.appendChild(el('div', 'valid-claim', v.claim));
            vbody.appendChild(el('div', 'valid-note', meta.label + (v.note ? ' — ' + v.note : '')));
            item.appendChild(vbody);
            list.appendChild(item);
        });
        body.appendChild(list);
    }
    document.getElementById('readinessDetailModal').style.display = 'flex';
}

function closeReadinessDetail() {
    document.getElementById('readinessDetailModal').style.display = 'none';
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
// The header toggle is the single source of truth for the conversation mode:
// checked = load the existing WiFi/BT skills as prior knowledge (interview only
// probes what's NEW beyond them; export stays non-overlapping); unchecked =
// teach from scratch. Every LLM-triggering call (log round, live assess,
// export) sends this value so the whole conversation stays in one mode.
function priorMode() {
    const t = document.getElementById('priorToggle');
    return !!(t && t.checked);
}

// When the toggle flips mid-conversation, tell the server right away so the
// auto-fired background assess (and a page reload) reflect the new mode even
// before the next analysis.
function onPriorToggle() {
    // Switching prerequisite-knowledge mode changes what the baseline is
    // allowed to know. Require one fresh read against that new comparison
    // basis; the button returns to its one-time glow until pressed.
    baselineDone = false;
    setBaselineGate();
    fetch(LV.url.learning_set_mode, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({use_prior_knowledge: priorMode()}),
    }).catch(() => {});
}

function onInterviewModeChange() {
    const select = document.getElementById('interviewMode');
    interviewMode = select ? select.value : 'ask';
    updateChatRefinementSummary();
    fetch(LV.url.learning_set_mode, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({interview_mode: interviewMode}),
    })
        .then(r => r.json())
        .then(d => { if (d.decision_ledger) updateDecisionLedger(d.decision_ledger); })
        .catch(() => {});
}

function updateDecisionLedger(data) {
    if (!data) return;
    decisionLedger = data;
    interviewMode = data.mode || interviewMode;
    const select = document.getElementById('interviewMode');
    if (select) select.value = interviewMode;
    const badge = document.getElementById('decisionBadgeText');
    if (badge) {
        const total = (data.items || []).length;
        badge.textContent = `${data.resolved || 0}/${total} decisions`;
    }
    const button = document.getElementById('decisionBadge');
    if (button) button.classList.toggle('has-open', !!data.open);
    updateChatRefinementSummary();
}

function updateChatRefinementSummary() {
    const summary = document.getElementById('chatRefinementSummary');
    if (!summary) return;
    const total = (decisionLedger.items || []).length;
    const modeLabel = interviewMode.charAt(0).toUpperCase() + interviewMode.slice(1);
    summary.textContent = `${modeLabel} · ${decisionLedger.resolved || 0}/${total}`;
}

function toggleChatRefinement(force) {
    const row = document.getElementById('chatRefinementRow');
    const toggle = document.getElementById('chatRefinementToggle');
    const chevron = document.getElementById('chatRefinementChevron');
    if (!row || !toggle) return;
    const open = force !== undefined ? force : row.hidden;
    row.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    toggle.classList.toggle('is-open', open);
    if (chevron) {
        chevron.classList.toggle('fa-chevron-down', !open);
        chevron.classList.toggle('fa-chevron-up', open);
    }
}

function openDecisionLedger() {
    const body = document.getElementById('decisionLedgerBody');
    const items = decisionLedger.items || [];
    if (!items.length) {
        body.innerHTML = '<div class="decision-empty"><i class="fas fa-code-branch"></i><b>No decisions yet</b><span>Questions that affect scope, rules, keywords, or exceptions will appear here.</span></div>';
    } else {
        body.innerHTML = items.map((item) => {
            const status = item.status || 'open';
            const answer = item.answer
                ? `<div class="decision-answer"><b>Engineer:</b> ${escapeHtml(item.answer)}</div>` : '';
            const rec = item.recommended_answer
                ? `<div class="decision-recommendation"><b>Recommended:</b> ${escapeHtml(item.recommended_answer)}${item.recommendation_reason ? ` — ${escapeHtml(item.recommendation_reason)}` : ''}</div>` : '';
            return `<div class="decision-row is-${escapeHtml(status)}">
              <div class="decision-row-head"><span>${escapeHtml(item.id)}</span><b>${escapeHtml(item.source || 'chat')}</b><em>${escapeHtml(status)}</em></div>
              <div class="decision-question">${escapeHtml(item.question)}</div>${rec}${answer}
            </div>`;
        }).join('');
    }
    document.getElementById('decisionLedgerModal').style.display = 'flex';
}

function closeDecisionLedger() {
    document.getElementById('decisionLedgerModal').style.display = 'none';
}

function deferDecision(decisionId) {
    if (!decisionId) return;
    fetch(LV.url.learning_defer_decision, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({decision_id: decisionId}),
    })
        .then(r => r.json())
        .then(d => { if (d.decision_ledger) updateDecisionLedger(d.decision_ledger); })
        .catch(() => {});
}

function appendRecommendation(card, q) {
    if (!q || !q.recommended_answer) return;
    const row = el('div', 'question-recommendation');
    row.appendChild(el('span', 'question-recommendation-label', 'Recommended'));
    row.appendChild(document.createTextNode(q.recommended_answer));
    if (q.recommendation_reason) {
        row.appendChild(el('small', '', q.recommendation_reason));
    }
    card.appendChild(row);
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
        exportBtn.disabled = !baselineDone || isBusy();
        exportBtn.title = baselineDone
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
        body: JSON.stringify({force: !!forceRefresh}),
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
            setBusy(false);
            if (d.cached) return;                 // already read, nothing new to show
            if (d.usage) updateTokenBadge(d.usage.session);
            if (d.assessment) applyAssessment(d.assessment);
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
    card.appendChild(el('div', 'chat-q-progress', `🎓 Teaching step #${seq}`));
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
    box.scrollTop = box.scrollHeight;
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
    card.appendChild(el('div', 'chat-q-progress', `🧠 Knowledge core — step #${seq}`));
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
        const fuRow = el('div', 'step-explain-actions');
        const fuInput = document.createElement('input');
        fuInput.type = 'text';
        fuInput.className = 'form-control form-control-sm';
        fuInput.placeholder = 'Answer… or skip';
        const fuSend = el('button', 'btn btn-sm btn-primary');
        fuSend.type = 'button';
        fuSend.innerHTML = '<i class="fas fa-paper-plane"></i>';
        const fuSkip = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
        fuSkip.type = 'button';
        const submitFollowUp = () => {
            const ans = fuInput.value.trim();
            if (!ans) return;
            persist();
            card.remove();
            sendMsg(ans, undefined, seq, false, decisionId);
        };
        fuInput.onkeypress = (e) => { if (e.key === 'Enter') submitFollowUp(); };
        fuSend.onclick = submitFollowUp;
        fuSkip.onclick = () => { deferDecision(decisionId); fuSection.remove(); };
        fuRow.appendChild(fuInput);
        fuRow.appendChild(fuSend);
        fuRow.appendChild(fuSkip);
        fuSection.appendChild(fuRow);
        card.appendChild(fuSection);
    }

    box.appendChild(card);
    box.scrollTop = box.scrollHeight;
}

// "Ask about this step" — the LLM-LED counterpart to 🎓's user-led explain
// box: clicking a step's ❓ icon (see renderStepPanel) has the LLM fire ONE
// targeted question about that edit instead of the engineer writing first.
// Always skippable — this is a secondary entry point for whoever would rather
// answer a question than compose free text, never a forced interruption.
function askStepQuestion(seq, btn) {
    if (isBusy() || btn.disabled) return;
    setBusy(true);
    const label = btn.innerHTML;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    fetch(LV.url.learning_ask_step, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({seq: seq}),
    })
        .then(r => r.json())
        .then(d => {
            setBusy(false);
            btn.innerHTML = label;
            if (!d.success) { alert(d.message); return; }
            if (d.usage) updateTokenBadge(d.usage.session);
            if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
            renderStepAskCard(seq, d.question, d.decision_id);
        })
        .catch(e => { setBusy(false); btn.innerHTML = label; alert('Failed: ' + e); });
}

// Renders the ❓ question with the SAME options/custom-answer shape as
// showNextQuestionCard, plus an explicit Skip. Skipping is purely client-side
// (dismiss, no backend call — nothing was asked "out loud" to the model to
// walk back). Answering posts to /learning/answer_step_question, which tags
// both messages with this step and only fills the reason if it's still empty.
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
    appendRecommendation(card, q);
    if (d.captures) card.appendChild(el('div', 'clarify-captures', d.captures));

    const submit = (answer) => {
        if (d.kind === 'focus') {
            fetch(LV.url.learning_answer_focus_clarify, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({question: q.question, answer, decision_id: d.decision_id || ''}),
            }).then(r => r.json()).then(result => {
                if (result.decision_ledger) updateDecisionLedger(result.decision_ledger);
                card.remove();
                appendMsg('assistant', `❓ ${q.question}`, 'all');
                appendMsg('user', answer, 'all');
            });
        } else {
            submitStepAnswer(d.seq, q.question, answer, card, d.decision_id);
        }
    };

    const customBox = el('div', 'chat-q-custom');
    if (q.type === 'choice' && q.options && q.options.length) {
        customBox.style.display = 'none';
        const optsBox = el('div', 'chat-q-opts');
        q.options.forEach(opt => {
            const b = el('button', 'chat-q-opt', opt);
            b.type = 'button';
            b.onclick = () => submit(opt);
            optsBox.appendChild(b);
        });
        const otherBtn = el('button', 'chat-q-opt chat-q-opt-other', 'Other…');
        otherBtn.type = 'button';
        otherBtn.onclick = () => {
            optsBox.style.display = 'none';
            customBox.style.display = 'flex';
            customBox.querySelector('input').focus();
        };
        optsBox.appendChild(otherBtn);
        card.appendChild(optsBox);
    } else {
        customBox.style.display = 'flex';
    }

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control form-control-sm';
    input.placeholder = 'Type your answer…';
    input.onkeypress = (e) => { if (e.key === 'Enter' && input.value.trim()) submit(input.value.trim()); };
    const sendBtn = el('button', 'btn btn-sm btn-primary');
    sendBtn.type = 'button';
    sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i>';
    sendBtn.onclick = () => { if (input.value.trim()) submit(input.value.trim()); };
    // Skipping is a real answer ("I don't want to explain this"), and the
    // server has already recorded the divergence as asked — so it won't
    // reappear on the next filter run.
    const skipBtn = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
    skipBtn.type = 'button';
    skipBtn.onclick = () => { deferDecision(d.decision_id); card.remove(); };
    customBox.appendChild(input);
    customBox.appendChild(sendBtn);
    customBox.appendChild(skipBtn);
    card.appendChild(customBox);
    if (q.type === 'choice' && q.options && q.options.length) {
        const skipRow = el('div', 'step-ask-skip-row');
        const skip2 = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
        skip2.type = 'button';
        skip2.onclick = () => { deferDecision(d.decision_id); card.remove(); };
        skipRow.appendChild(skip2);
        card.appendChild(skipRow);
    }
    box.appendChild(card);
    box.scrollTop = box.scrollHeight;
}

function renderStepAskCard(seq, q, decisionId) {
    const box = document.getElementById('chatBox');
    const card = el('div', 'chat-question-card step-ask-card mb-2');
    card.appendChild(el('div', 'chat-q-progress', `❓ Step #${seq}`));
    card.appendChild(el('div', 'chat-q-text', q.question));
    appendRecommendation(card, q);

    const customBox = el('div', 'chat-q-custom');
    if (q.type === 'choice' && q.options && q.options.length) {
        customBox.style.display = 'none';
        const optsBox = el('div', 'chat-q-opts');
        q.options.forEach(opt => {
            const b = el('button', 'chat-q-opt', opt);
            b.type = 'button';
            b.onclick = () => submitStepAnswer(seq, q.question, opt, card, decisionId);
            optsBox.appendChild(b);
        });
        const otherBtn = el('button', 'chat-q-opt chat-q-opt-other', 'Other…');
        otherBtn.type = 'button';
        otherBtn.onclick = () => {
            optsBox.style.display = 'none';
            customBox.style.display = 'flex';
            customBox.querySelector('input').focus();
        };
        optsBox.appendChild(otherBtn);
        card.appendChild(optsBox);
    } else {
        customBox.style.display = 'flex';
    }

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control form-control-sm';
    input.placeholder = 'Type your answer…';
    input.onkeypress = (e) => {
        if (e.key === 'Enter' && input.value.trim()) submitStepAnswer(seq, q.question, input.value.trim(), card, decisionId);
    };
    const sendBtn = el('button', 'btn btn-sm btn-primary');
    sendBtn.type = 'button';
    sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i>';
    sendBtn.onclick = () => { if (input.value.trim()) submitStepAnswer(seq, q.question, input.value.trim(), card, decisionId); };
    const skipBtn = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
    skipBtn.type = 'button';
    skipBtn.onclick = () => { deferDecision(decisionId); card.remove(); };

    customBox.appendChild(input);
    customBox.appendChild(sendBtn);
    customBox.appendChild(skipBtn);
    card.appendChild(customBox);
    // Choice-type questions show Skip alongside the options too, not only
    // inside the (initially hidden) custom-answer row.
    if (q.type === 'choice' && q.options && q.options.length) {
        const skipRow = el('div', 'step-ask-skip-row');
        const skip2 = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
        skip2.type = 'button';
        skip2.onclick = () => { deferDecision(decisionId); card.remove(); };
        skipRow.appendChild(skip2);
        card.appendChild(skipRow);
    }

    box.appendChild(card);
    box.scrollTop = box.scrollHeight;
}

function submitStepAnswer(seq, question, answer, card, decisionId) {
    if (isBusy()) return;
    setBusy(true);
    fetch(LV.url.learning_answer_step_question, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({seq: seq, question: question, answer: answer, decision_id: decisionId || ''}),
    })
        .then(r => r.json())
        .then(d => {
            setBusy(false);
            card.remove();
            if (!d.success) { alert(d.message); return; }
            appendMsg('assistant', `❓ ${question}`, seq);
            appendMsg('user', answer, seq);
            if (d.decision_ledger) updateDecisionLedger(d.decision_ledger);
            syncOpsQuiet(d); // updates the step's "✓ why" indicator (only if it was previously empty)
        })
        .catch(e => { setBusy(false); alert('Failed: ' + e); });
}

// Whatever number of questions the LLM returns in one batch, show them ONE
// at a time — a card is only for the current question; answering it (click
// an option, or type + send) removes it and reveals the next, rather than
// dumping every question into the chat at once.
function renderQuestionCards(questions) {
    appendMsg('assistant', 'Follow-up — pick an option or type your own:', 'all');
    showNextQuestionCard(questions.slice(), 0, questions.length);
}

function showNextQuestionCard(queue, doneCount, total) {
    if (!queue.length) return;
    const q = queue.shift();
    const box = document.getElementById('chatBox');

    const card = document.createElement('div');
    card.className = 'chat-question-card mb-2';

    const progress = document.createElement('div');
    progress.className = 'chat-q-progress';
    progress.textContent = `Question ${doneCount + 1} of ${total}`;
    card.appendChild(progress);

    const qText = document.createElement('div');
    qText.className = 'chat-q-text';
    qText.textContent = q.question;
    card.appendChild(qText);
    appendRecommendation(card, q);

    const advance = () => showNextQuestionCard(queue, doneCount + 1, total);
    // forceTag 'all': these are clarification follow-ups, about the whole round —
    // not whatever step the step-context selector happens to be set to.
    const finish = (answerText) => {
        card.remove();
        sendMsg(answerText, undefined, 'all', false, q.decision_id);
        advance();
    };

    const customBox = document.createElement('div');
    customBox.className = 'chat-q-custom';

    if (q.type === 'choice' && q.options && q.options.length) {
        customBox.style.display = 'none';
        const optsBox = document.createElement('div');
        optsBox.className = 'chat-q-opts';
        q.options.forEach(opt => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'chat-q-opt';
            b.textContent = opt;
            b.onclick = () => finish(opt);
            optsBox.appendChild(b);
        });
        const otherBtn = document.createElement('button');
        otherBtn.type = 'button';
        otherBtn.className = 'chat-q-opt chat-q-opt-other';
        otherBtn.textContent = 'Other…';
        otherBtn.onclick = () => {
            optsBox.style.display = 'none';
            customBox.style.display = 'flex';
            customBox.querySelector('input').focus();
        };
        optsBox.appendChild(otherBtn);
        card.appendChild(optsBox);
    } else {
        customBox.style.display = 'flex';
    }

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control form-control-sm';
    input.placeholder = 'Type your answer…';
    input.onkeypress = (e) => {
        if (e.key === 'Enter') { const v = input.value.trim(); if (v) finish(v); }
    };
    const sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'btn btn-sm btn-primary';
    sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i>';
    sendBtn.onclick = () => { const v = input.value.trim(); if (v) finish(v); };
    customBox.appendChild(input);
    customBox.appendChild(sendBtn);
    const skipBtn = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
    skipBtn.type = 'button';
    skipBtn.onclick = () => {
        deferDecision(q.decision_id);
        card.remove();
        advance();
    };
    customBox.appendChild(skipBtn);
    card.appendChild(customBox);
    if (q.type === 'choice' && q.options && q.options.length) {
        const skipRow = el('div', 'step-ask-skip-row');
        const visibleSkip = el('button', 'btn btn-sm btn-outline-secondary', 'Skip');
        visibleSkip.type = 'button';
        visibleSkip.onclick = skipBtn.onclick;
        skipRow.appendChild(visibleSkip);
        card.appendChild(skipRow);
    }

    box.appendChild(card);
    box.scrollTop = box.scrollHeight;
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
    // Guard FIRST, before the confirm() dialog even opens — a native
    // confirm() blocks the page but a fast double-click still queues a
    // second click event that fires the instant the dialog closes; checking
    // disabled state up front (rather than only after the dialog) is what
    // actually stops a second /learning/converge call from firing (which is
    // exactly what produced the duplicate skill entries seen earlier). Also
    // checks the GLOBAL lock so Export can't start while a baseline read, a chat
    // send, or a step teach/ask is still in flight, and vice versa.
    if (btn.disabled || isBusy()) return;

    // Unconditional, deliberately: this used to fire only in the old "grill"
    // mode, so the DEFAULT mode exported with unresolved decisions and no
    // warning whatsoever. Whether a question was asked at all is the
    // interview mode's business; whether an ASKED question went unanswered
    // is the export's business, and belongs here regardless of mode. Optional
    // follow-ups opt out via blocking=false (see decision_ledger).
    const blockingDecisions = (decisionLedger.items || []).filter(
        item => item.status === 'open' && item.blocking
    );
    if (blockingDecisions.length &&
        !confirm(
            `${blockingDecisions.length} specification decision(s) are still unresolved:\n\n` +
            blockingDecisions.map(item => `• ${item.question}`).join('\n') +
            '\n\nExport anyway? They will stay visible in the Skill Spec review.'
        )) {
        return;
    }

    if (currentAssessment) {
        const score = (currentAssessment.readiness || {}).score;
        const flagged = (currentAssessment.validation || []).filter(v => v.status !== 'verified');
        const gaps = currentAssessment.gaps || [];
        const warnings = [];
        if (typeof score === 'number' && score < 60)
            warnings.push(`• Readiness is only ${score}% — the skill may be thin.`);
        if (flagged.length)
            warnings.push(`• ${flagged.length} claim(s) are NOT verified from this log (will be exported as domain knowledge, not proven fact):\n` +
                flagged.map(v => `    - ${v.claim}`).join('\n'));
        if (gaps.length)
            warnings.push(`• ${gaps.length} open item(s) still unanswered:\n` + gaps.map(g => `    - ${g}`).join('\n'));
        if (warnings.length &&
            !confirm("Export anyway?\n\n" + warnings.join('\n\n') +
                     "\n\nYou can still edit everything in the next screen before saving.")) {
            return;
        }
    }
    if (btn.disabled || isBusy()) return; // re-check: a queued second click could have landed during confirm()
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
// brand-new skill (no extend/diff routing) — its `domain` (WiFi/BT) tells
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
            alert('Skill saved!' + (draftQueue.length ? ` Now reviewing the next one (${draftQueue.length} left)…` : ''));
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
            applyAssessment(null); // server reset round_count/assessment on save
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
});

renderFilters();
renderStepTagSelector();
renderStepPanel(); // now a permanent card (left column), not a toggled overlay
if (LV.boot.logDomain) refreshSkillList(LV.boot.logDomain);
// Restore the event-log panel from server state on reload (BT only).
updateEvtSection(LV.boot.logDomain, LV.boot.hasEventLog);
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
// Restore the prior-knowledge toggle from server state on reload.
document.getElementById('priorToggle').checked = LV.boot.priorKnowledge;
document.getElementById('interviewMode').value = interviewMode;
updateDecisionLedger(decisionLedger);
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
