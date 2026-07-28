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
    rules: [],       // array of raw rule-block strings (each may be multi-line)
    preamble: "",
    diff: null,      // {new_keywords, new_exclusive, rules_added_text} from an extend-draft, or null
    teachingEvidence: null,
    domain: "wifi",
    options: {},
  };

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  // Split "expert_rules" free text into a preamble (everything before the
  // first top-level numbered item) + a list of rule blocks. Sub-numbering
  // like "2-1." / "3-2." does NOT start a new block (it stays nested inside
  // the block it follows) — only "<digits>. " at the start of a line does.
  function splitExpertRules(text) {
    const lines = (text || "").split("\n");
    const topLevelRe = /^\d+\.\s/;
    let preambleLines = [];
    let blocks = [];
    let current = null;
    let inPreamble = true;
    for (const line of lines) {
      if (topLevelRe.test(line)) {
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

  function renderChips(containerId, list, newSet) {
    const box = document.getElementById(containerId);
    box.innerHTML = list
      .map((val, i) => {
        const isNew = newSet && newSet.has(val);
        return `<span class="chip${isNew ? " chip-new" : ""}">${escapeHtml(val)}<button type="button" class="chip-x" data-list="${containerId}" data-idx="${i}">&times;</button></span>`;
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
    box.innerHTML = state.rules
      .map((r, i) => {
        const isNew = addedLines && r.split("\n").some((l) => addedLines.has(l.trim()));
        return `
      <div class="rule-point${isNew ? " rule-point-new" : ""}">
        <span class="rule-badge">${i + 1}</span>
        <textarea class="form-control form-control-sm rule-text" data-idx="${i}" rows="${Math.min(6, Math.max(2, r.split("\n").length))}">${escapeHtml(r)}</textarea>
        ${isNew ? '<span class="rule-point-new-tag">NEW</span>' : ""}
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
  function renderDiffBanner() {
    const el = document.getElementById("skm-diff-banner");
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

  function renderDescMeter() {
    const desc = document.getElementById("skm-desc").value;
    const s = descStrength(desc);
    const bar = document.getElementById("skm-desc-bar");
    bar.style.width = s.pct + "%";
    bar.className = "desc-strength-bar " + s.cls;
    document.getElementById("skm-desc-label").textContent = s.label;
  }

  function open(data, options) {
    data = data || {};
    options = options || {};
    state.keywords = (data.keywords || []).slice();
    state.exclusive = (data.exclusive || []).slice();
    const split = splitExpertRules(data.expert_rules || "");
    state.preamble = split.preamble;
    state.rules = split.rules;
    // Present only on an extend-draft from /learning/converge
    // (learning_service.compute_skill_diff) — drives the green "NEW"
    // highlighting on chips/rules below plus the summary banner.
    state.diff = data.diff || null;
    state.teachingEvidence = data.teaching_evidence || null;
    state.domain = data.domain || "wifi";
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

    renderDiffBanner();
    renderVerificationBanner();
    renderChips("skm-keywords", state.keywords, state.diff ? new Set(state.diff.new_keywords) : null);
    renderChips("skm-exclusive", state.exclusive, state.diff ? new Set(state.diff.new_exclusive) : null);
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
        expert_rules: joinExpertRules(),
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

    document.getElementById("skm-kw-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && this.value.trim()) {
        e.preventDefault();
        state.keywords.push(this.value.trim());
        this.value = "";
        renderChips("skm-keywords", state.keywords, newKwSet());
      }
    });
    document.getElementById("skm-ex-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && this.value.trim()) {
        e.preventDefault();
        state.exclusive.push(this.value.trim());
        this.value = "";
        renderChips("skm-exclusive", state.exclusive, newExSet());
      }
    });

    // Delegated handlers: chip removal + rule reorder/delete (elements are
    // re-rendered on every change, so a single listener on the container
    // avoids re-binding after every render()).
    document.getElementById("skm-keywords").addEventListener("click", function (e) {
      const btn = e.target.closest(".chip-x");
      if (!btn) return;
      state.keywords.splice(Number(btn.dataset.idx), 1);
      renderChips("skm-keywords", state.keywords, newKwSet());
    });
    document.getElementById("skm-exclusive").addEventListener("click", function (e) {
      const btn = e.target.closest(".chip-x");
      if (!btn) return;
      state.exclusive.splice(Number(btn.dataset.idx), 1);
      renderChips("skm-exclusive", state.exclusive, newExSet());
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
