/*
 * Shared "Edit Skill" modal — shows what the currently-converging SKILL looks
 * like, styled after wireless_ce_avatar/IntelAvatar's Log Chatbot
 * "Edit Skills Configuration" popup (#skill-editor-modal): a card with
 * name / description (+ strength meter) / keywords & exclusive as removable
 * chips / expert_rules as a preamble + numbered rule blocks.
 *
 * Any page can call SkillEditor.open(data, options) to show it; this file is
 * included once from base.html so the same popup is reachable from
 * Log Viewer ("view current skill while filtering"), Teach Skill (review the
 * freshly-synthesized draft), and Skills (edit/create).
 */
(function () {
  "use strict";

  let state = {
    keywords: [],
    exclusive: [],
    // Boundary conditions under which this skill applies. Edited as chips
    // here, compiled onto the description server-side on save.
    triggers: [],
    rules: [],       // array of raw rule-block strings (each may be multi-line)
    preamble: "",
    diff: null,      // {new_keywords, new_exclusive, rules_added_text} from an extend-draft, or null
    // {parent_name, inherited_keywords, inherited_exclusive, ...} when this
    // draft was rebuilt on a LOADED skill's framework (see
    // /learning/converge + utils.skill_dedup.build_extension_skill). Drives
    // the blue "INH" vs green "NEW" split below.
    lineageInfo: null,
    // Caller-supplied banners about this draft (see renderNotices).
    notices: [],
    teachingEvidence: null,
    domain: "wifi",
    // Ancestry of whatever was opened. Held verbatim and sent straight back on
    // save: the editor never sets or reasons about it, it only makes sure a
    // trip through this modal doesn't erase it.
    parent: null,
    lineage: [],
    options: {},
  };

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  // The boundary build_extension_skill writes between inherited rules and the
  // ones added this session. Matched loosely (marker text only, not the whole
  // generated sentence) so a re-worded label doesn't break the split.
  const INHERIT_MARKER_RE = /^#\s*─*\s*Extension:/;

  // Split "expert_rules" free text into a preamble (everything before the
  // first top-level numbered item) + a list of rule blocks. Sub-numbering
  // like "2-1." / "3-2." does NOT start a new block (it stays nested inside
  // the block it follows) — only "<digits>. " at the start of a line does.
  // The inheritance marker also starts a block, so it becomes its own entry
  // and can be drawn as a divider instead of being swallowed by the last
  // inherited rule (which is where it would otherwise land).
  function splitExpertRules(text) {
    const lines = (text || "").split("\n");
    const topLevelRe = /^\d+\.\s/;
    let preambleLines = [];
    let blocks = [];
    let current = null;
    let inPreamble = true;
    for (const line of lines) {
      if (INHERIT_MARKER_RE.test(line)) {
        if (current !== null) blocks.push(current.join("\n").replace(/\s+$/, ""));
        current = [line];
        inPreamble = false;
      } else if (topLevelRe.test(line)) {
        if (current !== null) blocks.push(current.join("\n").replace(/\s+$/, ""));
        current = [line];
        inPreamble = false;
      } else if (inPreamble) {
        preambleLines.push(line);
      } else {
        current.push(line);
      }
    }
    if (current !== null) blocks.push(current.join("\n").replace(/\s+$/, ""));
    return {
      preamble: preambleLines.join("\n").replace(/^\s+|\s+$/g, ""),
      rules: blocks.length ? blocks : (text || "").trim() ? [(text || "").trim()] : [],
    };
  }

  function joinExpertRules() {
    const parts = [];
    if (state.preamble.trim()) parts.push(state.preamble.trim());
    for (const r of state.rules) {
      if (r.trim()) parts.push(r.trim());
    }
    return parts.join("\n\n");
  }

  function descStrength(desc) {
    const len = (desc || "").trim().length;
    if (len === 0) return { pct: 0, label: "", cls: "" };
    if (len < 20) return { pct: 25, label: "Weak — add more detail", cls: "weak" };
    if (len < 60) return { pct: 50, label: "Fair", cls: "fair" };
    if (len < 120) return { pct: 75, label: "Good", cls: "good" };
    return { pct: 100, label: "Strong", cls: "strong" };
  }

  // `inheritedSet` is only non-null for a draft built on a LOADED parent. When
  // it is, EVERY chip gets an explicit category — blue INH (came from the
  // parent) or green NEW (taught this session) — rather than new chips being
  // green and everything else unmarked. With 19 inherited and 1 new, "unmarked"
  // reads as "the default", which is the opposite of what it means here.
  function renderChips(containerId, list, newSet, inheritedSet) {
    const box = document.getElementById(containerId);
    box.innerHTML = list
      .map((val, i) => {
        let cls = "";
        if (inheritedSet) cls = inheritedSet.has(val) ? " chip-inherited" : " chip-new";
        else if (newSet && newSet.has(val)) cls = " chip-new";
        return `<span class="chip${cls}">${escapeHtml(val)}<button type="button" class="chip-x" data-list="${containerId}" data-idx="${i}">&times;</button></span>`;
      })
      .join("");
  }

  function renderRules() {
    const box = document.getElementById("skm-rules");
    if (!state.rules.length) {
      box.innerHTML = '<div class="text-muted small">No rules yet — click "Add rule" below.</div>';
      return;
    }
    // A rule block counts as "new" if ANY of its non-blank lines appear in
    // the diff's rules_added_text — this project's expert_rules are always
    // numbered blocks (see learning_service._STYLE_EXEMPLAR), so a whole
    // added rule shows up this way without needing a real structural diff.
    const addedLines = state.diff && state.diff.rules_added_text
      ? new Set(state.diff.rules_added_text.split("\n").map((l) => l.trim()).filter(Boolean))
      : null;
    // On an inheritance draft the marker block IS the boundary — everything
    // before it came from the parent, everything after was added this session.
    // No text comparison needed: build_extension_skill guarantees that order.
    const markerIdx = state.lineageInfo
      ? state.rules.findIndex((r) => INHERIT_MARKER_RE.test(r.split("\n")[0] || ""))
      : -1;

    let ruleNo = 0;
    box.innerHTML = state.rules
      .map((r, i) => {
        if (i === markerIdx) {
          // Not an editable rule — a labelled separator. It stays in
          // state.rules so joinExpertRules writes it back out unchanged.
          return `<div class="rule-divider"><span>added in this session</span></div>`;
        }
        ruleNo += 1;
        let cls = "";
        let tag = "";
        if (markerIdx >= 0) {
          const inherited = i < markerIdx;
          cls = inherited ? " rule-point-inherited" : " rule-point-new";
          tag = inherited
            ? '<span class="rule-point-inh-tag">INH</span>'
            : '<span class="rule-point-new-tag">NEW</span>';
        } else if (addedLines && r.split("\n").some((l) => addedLines.has(l.trim()))) {
          cls = " rule-point-new";
          tag = '<span class="rule-point-new-tag">NEW</span>';
        }
        return `
      <div class="rule-point${cls}">
        <span class="rule-badge">${ruleNo}</span>
        <textarea class="form-control form-control-sm rule-text" data-idx="${i}" rows="${Math.min(6, Math.max(2, r.split("\n").length))}">${escapeHtml(r)}</textarea>
        ${tag}
        <div class="rule-actions">
          <button type="button" class="btn btn-sm btn-outline-secondary" data-act="up" data-idx="${i}" title="Move up">&uarr;</button>
          <button type="button" class="btn btn-sm btn-outline-secondary" data-act="down" data-idx="${i}" title="Move down">&darr;</button>
          <button type="button" class="btn btn-sm btn-outline-danger" data-act="del" data-idx="${i}" title="Delete">&times;</button>
        </div>
      </div>`;
      })
      .join("");
  }

  // Summary strip above the form when this draft extends an existing skill —
  // spells out counts so the highlighted chips/rules below aren't the only
  // signal that something is being added rather than freshly created.
  // Caller-supplied notices about this draft, rendered as stacked banners
  // instead of the chain of native alert() dialogs this used to be. Each is
  // {kind: "info"|"warn"|"lineage", icon, title, body, list}.
  function renderNotices() {
    const box = document.getElementById("skm-notices");
    if (!box) return;
    const notices = state.notices || [];
    if (!notices.length) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = notices
      .map((n) => {
        const list = (n.list || []).length
          ? `<ul class="skm-notice-list">${n.list.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>`
          : "";
        const title = n.title ? `<b>${escapeHtml(n.title)}</b> ` : "";
        return `<div class="skm-notice skm-notice-${escapeHtml(n.kind || "info")}">
          <span class="skm-notice-icon">${escapeHtml(n.icon || "")}</span>
          <div>${title}${escapeHtml(n.body || "")}${list}</div>
        </div>`;
      })
      .join("");
  }

  function renderDiffBanner() {
    const el = document.getElementById("skm-diff-banner");
    // An inheritance draft has its own banner: it must state the legend for
    // the two colours, because here almost everything on screen is inherited
    // and the handful of new items is what the engineer came to review.
    const li = state.lineageInfo;
    if (li) {
      const dropped = li.removed_as_duplicate
        ? ` <span class="skm-legend-note">${li.removed_as_duplicate} duplicate(s) already dropped.</span>` : "";
      el.innerHTML = `<i class="fas fa-code-branch mt-1"></i><div>
        <b>Built on &ldquo;${escapeHtml(li.parent_name)}&rdquo;</b> — the loaded skill's content is carried over in full so this
        skill still runs standalone.${dropped}
        <div class="skm-legend">
          <span class="chip chip-inherited skm-legend-chip">inherited from parent</span>
          <span class="chip chip-new skm-legend-chip">new in this skill</span>
        </div></div>`;
      el.style.display = "flex";
      return;
    }
    const d = state.diff;
    if (!d || (!d.new_keywords.length && !d.new_exclusive.length && !d.rules_added_text)) {
      el.style.display = "none";
      el.innerHTML = "";
      return;
    }
    const ruleCount = d.rules_added_text ? d.rules_added_text.split("\n").filter((l) => l.trim()).length : 0;
    const parts = [];
    if (d.new_keywords.length) parts.push(`${d.new_keywords.length} new keyword(s)`);
    if (d.new_exclusive.length) parts.push(`${d.new_exclusive.length} new noise term(s)`);
    if (ruleCount) parts.push(`new rule text (${ruleCount} line(s))`);
    el.innerHTML = `<i class="fas fa-code-branch mt-1"></i><div><b>Extending an existing skill</b> — ${parts.join(", ")}, highlighted in green below. Everything else is unchanged from the existing skill.</div>`;
    el.style.display = "flex";
  }

  function renderVerificationBanner() {
    const el = document.getElementById("skm-verification-banner");
    const evidence = state.teachingEvidence;
    if (!el || !evidence) {
      if (el) el.style.display = "none";
      return;
    }
    const checks = (evidence.checks || []).map((check) =>
      `<div><b>${escapeHtml(check.name)}</b>: ${escapeHtml(check.status)} - ${escapeHtml(check.note)}</div>`
    ).join("");
    const externalLine = `<div class="mt-1"><b>External validation</b>: not run — run this skill in wireless_ce_avatar to measure real TP/FP/FN.</div>`;
    el.innerHTML = `<i class="fas fa-clipboard-list mt-1"></i><div><b>Teaching evidence</b> - ${escapeHtml(evidence.summary)}${checks}${externalLine}</div>`;
    el.style.display = "flex";
  }

  // Show the sentence the downstream agent will actually select on, since
  // that is the description PLUS the compiled trigger clause — not what the
  // description field alone displays.
  function renderTriggerPreview() {
    const el = document.getElementById("skm-trg-preview");
    if (!el) return;
    if (!state.triggers.length) { el.textContent = ""; return; }
    const desc = (document.getElementById("skm-desc").value || "").trim();
    el.textContent = "Saved as: " + desc + " Applies when: " + state.triggers.join("; ") + ".";
  }

  function renderDescMeter() {
    const desc = document.getElementById("skm-desc").value;
    const s = descStrength(desc);
    const bar = document.getElementById("skm-desc-bar");
    bar.style.width = s.pct + "%";
    bar.className = "desc-strength-bar " + s.cls;
    document.getElementById("skm-desc-label").textContent = s.label;
    renderDescWarning(desc);
    renderTriggerPreview();
  }

  // A child inherits every keyword its parent has, so downstream (Avatar's
  // agent picks a skill from `name: description` alone) this one line is the
  // only thing that can tell the two apart. Re-checked live as the engineer
  // types, so rewriting it clears the warning immediately instead of only
  // being re-judged on the next export.
  function renderDescWarning(desc) {
    const el = document.getElementById("skm-desc-warn");
    if (!el) return;
    const conflict = state.lineageInfo && state.lineageInfo.description_conflict;
    if (!conflict || !conflict.parent_description) {
      el.style.display = "none";
      return;
    }
    // A trigger the parent does not declare IS the discriminator, and it ends
    // up inside the saved description — so adding one resolves this live.
    // Warning anyway would train the engineer to ignore the warning.
    const parentTriggers = new Set((state.lineageInfo.parent_triggers || [])
      .map((t) => String(t).trim().toLowerCase()));
    const distinguishing = state.triggers
      .map((t) => String(t).trim())
      .filter((t) => t && !parentTriggers.has(t.toLowerCase()));

    if (similarEnough(desc, conflict.parent_description) && !distinguishing.length) {
      el.innerHTML =
        '<i class="fas fa-triangle-exclamation"></i> <b>Too close to the parent\'s description.</b> ' +
        "This skill inherits all of the parent's keywords, so this sentence is the only thing " +
        "that distinguishes them — as written, whoever picks between them is guessing. " +
        "Either rewrite it, or add a trigger condition above that the parent does not have.<br>" +
        `<span class="skm-desc-warn-parent">Parent: &ldquo;${escapeHtml(conflict.parent_description)}&rdquo;</span>`;
      el.style.display = "block";
    } else {
      el.style.display = "none";
    }
  }

  // Same measure the server used (difflib's ratio is a normalised longest-
  // matching-block score); reimplemented here only so the warning can update
  // per keystroke without a round-trip. The server's verdict is what gets
  // recorded — this is the live echo of it.
  function similarEnough(a, b) {
    const norm = (s) => (s || "").trim().toLowerCase().replace(/\s+/g, " ");
    const x = norm(a), y = norm(b);
    if (!x || !y) return false;
    if (x === y) return true;
    // Token overlap (Dice coefficient) — cheap, and for two one-sentence
    // descriptions it tracks the server's ratio closely enough for a warning.
    const tx = new Set(x.split(/[^a-z0-9]+/).filter(Boolean));
    const ty = new Set(y.split(/[^a-z0-9]+/).filter(Boolean));
    if (!tx.size || !ty.size) return false;
    let shared = 0;
    tx.forEach((t) => { if (ty.has(t)) shared += 1; });
    return (2 * shared) / (tx.size + ty.size) >= 0.75;
  }

  function open(data, options) {
    data = data || {};
    options = options || {};
    state.keywords = (data.keywords || []).slice();
    state.exclusive = (data.exclusive || []).slice();
    state.triggers = (data.triggers || []).slice();
    const split = splitExpertRules(data.expert_rules || "");
    state.preamble = split.preamble;
    state.rules = split.rules;
    // Present only on an extend-draft from /learning/converge
    // (learning_service.compute_skill_diff) — drives the green "NEW"
    // highlighting on chips/rules below plus the summary banner.
    state.diff = data.diff || null;
    state.lineageInfo = data.lineage_info || null;
    state.notices = options.notices || [];
    state.teachingEvidence = data.teaching_evidence || null;
    state.domain = data.domain || "wifi";
    state.parent = data.parent || null;
    state.lineage = (data.lineage || []).slice();
    state.options = {
      skillKey: data.skill_key || "",
      domain: state.domain,
      saveUrl: options.saveUrl,
      deleteUrl: options.deleteUrl || null,
      extraPayload: options.extraPayload || {},
      onSaved: options.onSaved || null,
      // Kept short on purpose — a long "New skill "X" — review before
      // saving" sentence wraps awkwardly in the header. The skill name goes
      // in the (smaller) subtitle line instead.
      title: options.title || (data.skill_key ? "Edit Skill" : "New Skill Draft"),
      subtitle: options.subtitle !== undefined ? options.subtitle : (data.name || ""),
    };

    document.getElementById("skillModalTitle").textContent = state.options.title;
    document.getElementById("skillModalSubtitle").textContent = state.options.subtitle;
    document.getElementById("skm-key").value = state.options.skillKey;
    document.getElementById("skm-name").value = data.name || "";
    document.getElementById("skm-desc").value = data.description || "";
    document.getElementById("skm-preamble").value = state.preamble;
    document.getElementById("skm-delete-btn").style.display = state.options.deleteUrl ? "inline-block" : "none";

    renderNotices();
    renderDiffBanner();
    renderVerificationBanner();
    renderChips("skm-keywords", state.keywords, newKwSet(), inhKwSet());
    renderChips("skm-exclusive", state.exclusive, newExSet(), inhExSet());
    renderChips("skm-triggers", state.triggers, null, null);
    renderTriggerPreview();
    renderRules();
    renderDescMeter();

    document.getElementById("skillEditorModal").style.display = "flex";
  }


  function close() {
    document.getElementById("skillEditorModal").style.display = "none";
  }

  function newKwSet() {
    return state.diff ? new Set(state.diff.new_keywords) : null;
  }
  function newExSet() {
    return state.diff ? new Set(state.diff.new_exclusive) : null;
  }
  // Null unless this draft inherits — renderChips only switches to the
  // two-colour scheme when it gets one of these.
  function inhKwSet() {
    return state.lineageInfo ? new Set(state.lineageInfo.inherited_keywords || []) : null;
  }
  function inhExSet() {
    return state.lineageInfo ? new Set(state.lineageInfo.inherited_exclusive || []) : null;
  }

  function collect() {
    state.preamble = document.getElementById("skm-preamble").value;
    document.querySelectorAll(".rule-text").forEach((el) => {
      state.rules[Number(el.dataset.idx)] = el.value;
    });
    return Object.assign(
      {
        skill_key: document.getElementById("skm-key").value || null,
        domain: state.domain,
        name: document.getElementById("skm-name").value.trim(),
        description: document.getElementById("skm-desc").value.trim(),
        keywords: state.keywords,
        exclusive: state.exclusive,
        triggers: state.triggers,
        expert_rules: joinExpertRules(),
        parent: state.parent,
        lineage: state.lineage,
      },
      state.options.extraPayload
    );
  }

  function save() {
    if (!state.options.saveUrl) {
      console.error("SkillEditor: no saveUrl configured");
      return;
    }
    const payload = collect();
    if (!payload.name) {
      alert("Please give the skill a name before saving.");
      return;
    }
    fetch(state.options.saveUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) {
          alert(d.message || "Save failed");
          return;
        }
        close();
        if (state.options.onSaved) state.options.onSaved(d);
      })
      .catch((e) => alert("Save failed: " + e));
  }

  function del() {
    if (!state.options.deleteUrl) return;
    if (!confirm("Delete this skill? This cannot be undone.")) return;
    fetch(state.options.deleteUrl, { method: "POST" })
      .then((r) => r.json())
      .then((d) => {
        if (!d.success) {
          alert("Delete failed");
          return;
        }
        close();
        if (state.options.onSaved) state.options.onSaved(d);
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    const modal = document.getElementById("skillEditorModal");
    if (!modal) return; // modal markup not present on this page

    modal.addEventListener("click", function (e) {
      if (e.target === modal) close(); // click on overlay backdrop
    });

    document.getElementById("skm-close").addEventListener("click", close);
    document.getElementById("skm-save-btn").addEventListener("click", save);
    document.getElementById("skm-delete-btn").addEventListener("click", del);

    document.getElementById("skm-desc").addEventListener("input", renderDescMeter);

    document.getElementById("skm-trg-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && this.value.trim()) {
        e.preventDefault();
        state.triggers.push(this.value.trim());
        this.value = "";
        renderChips("skm-triggers", state.triggers, null, null);
        renderTriggerPreview();
      }
    });
    document.getElementById("skm-triggers").addEventListener("click", function (e) {
      const btn = e.target.closest(".chip-x");
      if (!btn) return;
      state.triggers.splice(Number(btn.dataset.idx), 1);
      renderChips("skm-triggers", state.triggers, null, null);
      renderTriggerPreview();
    });

    document.getElementById("skm-kw-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && this.value.trim()) {
        e.preventDefault();
        state.keywords.push(this.value.trim());
        this.value = "";
        renderChips("skm-keywords", state.keywords, newKwSet(), inhKwSet());
      }
    });
    document.getElementById("skm-ex-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && this.value.trim()) {
        e.preventDefault();
        state.exclusive.push(this.value.trim());
        this.value = "";
        renderChips("skm-exclusive", state.exclusive, newExSet(), inhExSet());
      }
    });

    // Delegated handlers: chip removal + rule reorder/delete (elements are
    // re-rendered on every change, so a single listener on the container
    // avoids re-binding after every render()).
    document.getElementById("skm-keywords").addEventListener("click", function (e) {
      const btn = e.target.closest(".chip-x");
      if (!btn) return;
      state.keywords.splice(Number(btn.dataset.idx), 1);
      renderChips("skm-keywords", state.keywords, newKwSet(), inhKwSet());
    });
    document.getElementById("skm-exclusive").addEventListener("click", function (e) {
      const btn = e.target.closest(".chip-x");
      if (!btn) return;
      state.exclusive.splice(Number(btn.dataset.idx), 1);
      renderChips("skm-exclusive", state.exclusive, newExSet(), inhExSet());
    });

    document.getElementById("skm-add-rule").addEventListener("click", function () {
      // Persist any in-progress edits before mutating the array + re-rendering.
      document.querySelectorAll(".rule-text").forEach((el) => {
        state.rules[Number(el.dataset.idx)] = el.value;
      });
      state.rules.push("");
      renderRules();
    });

    document.getElementById("skm-rules").addEventListener("click", function (e) {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      document.querySelectorAll(".rule-text").forEach((el) => {
        state.rules[Number(el.dataset.idx)] = el.value;
      });
      const idx = Number(btn.dataset.idx);
      const act = btn.dataset.act;
      if (act === "del") {
        state.rules.splice(idx, 1);
      } else if (act === "up" && idx > 0) {
        [state.rules[idx - 1], state.rules[idx]] = [state.rules[idx], state.rules[idx - 1]];
      } else if (act === "down" && idx < state.rules.length - 1) {
        [state.rules[idx + 1], state.rules[idx]] = [state.rules[idx], state.rules[idx + 1]];
      }
      renderRules();
    });
  });

  window.SkillEditor = { open: open, close: close };
})();
