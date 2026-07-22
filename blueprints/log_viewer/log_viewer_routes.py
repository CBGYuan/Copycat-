import os
from datetime import date

from flask import Blueprint, render_template, request, jsonify

from configs.global_configs import app_config
from services import session_store, event_log_service
from utils import file_picker, tat_parser, helpers, operation_journal

log_viewer_bp = Blueprint("log_viewer", __name__, url_prefix="/log_viewer")


def _filters_from_skill(skill) -> list:
    """Synthesize TAT-style filter dicts from a skill's plain keywords/exclusive
    lists (used when the skill has no .tat_path of its own)."""
    filters = [{
        "text": k, "enabled": True, "excluding": False,
        "case_sensitive": False, "regex": False,
        "fore_color": None, "back_color": None,
    } for k in (skill.keywords or [])]
    filters += [{
        "text": t, "enabled": True, "excluding": True,
        "case_sensitive": False, "regex": False,
        "fore_color": None, "back_color": "#ffe0e0",
    } for t in (skill.exclusive or [])]
    return filters


@log_viewer_bp.route("/")
def index():
    state = session_store.get_state()
    llm_ready = bool(app_config.llm_helper and app_config.llm_helper.is_ready)
    session_usage = app_config.llm_helper.session_usage if llm_ready else None
    return render_template("log_viewer.html", state=state, skills=app_config.skills,
                            llm_ready=llm_ready, session_usage=session_usage)


RAW_PREVIEW_LINES = 500


@log_viewer_bp.route("/pick_log", methods=["POST"])
def pick_log():
    path = file_picker.pick_log_file()
    if not path:
        return jsonify({"success": False, "message": "No file selected"}), 400
    if not os.path.exists(path):
        return jsonify({"success": False, "message": "File not found"}), 400
    state = session_store.get_state()
    state.log_path = path
    state.log_domain = helpers.detect_log_domain(path)

    # For a BT capture, auto-discover the System Event Log sitting next to the
    # driver log so the collapsible event panel can light up without a manual
    # pick (the engineer can still override via /pick_event_log). WiFi logs
    # don't ship one, so only look when BT was detected.
    state.event_log_path = (
        event_log_service.find_event_log_near(path) if state.log_domain == "bt" else ""
    )

    # Some BT/WiFi driver-log exports carry only a time-of-day, no date at all
    # (BT's dateless HCI export, WiFi's DDD-player export) — a date must be
    # synthesized so every line still gets the canonical leading timestamp the
    # rest of the app reads (see helpers.read_log_file / _canonicalize_log_
    # line). Anchor on the loaded System Event Log's earliest event when one's
    # available — that's the actual capture day, keeping the log pane and the
    # event panel's click-sync meaningful — else fall back to the log file's
    # own mtime. Stored on state so /apply_filter's later full-log read uses
    # the SAME anchor (never re-derived, so the two reads can't disagree).
    date_synthesized = helpers.needs_date_synthesis(path)
    anchor_date = None
    if date_synthesized:
        if state.event_log_path:
            anchor_date = event_log_service.peek_event_log_date(state.event_log_path)
        if anchor_date is None:
            anchor_date = helpers.file_mtime_date(path)
    state.log_date_anchor = anchor_date.isoformat() if anchor_date else ""

    # Show the log right away — no reason to make the engineer wait until a
    # .tat/skill is loaded and a filter runs just to see the file loaded.
    # Uncoloured (no filter has run yet), capped so a 100k-line file doesn't
    # get pushed into the DOM before any filtering has happened.
    raw_preview = []
    total_lines = 0
    try:
        log_lines = helpers.read_log_file(path, fallback_date=anchor_date)
        total_lines = len(log_lines)
        raw_preview = [
            {"line_no": i, "text": line.rstrip("\n"), "back_color": None, "fore_color": None}
            for i, line in enumerate(log_lines[:RAW_PREVIEW_LINES], start=1)
        ]
    except Exception as e:
        print(f"⚠️  Could not build raw preview for {path}: {e}")

    return jsonify({
        "success": True,
        "log_path": path,
        "domain": state.log_domain,
        "total_lines": total_lines,
        "preview": raw_preview,
        "event_log_path": state.event_log_path,
        "event_log_available": bool(state.event_log_path),
        # True when this log's own lines had no date (dateless BT HCI / WiFi
        # DDD export) and the leading timestamps shown are synthesized —
        # the UI surfaces this so the engineer knows the date component (not
        # the time-of-day) is an estimate, not something the log itself proved.
        "date_synthesized": date_synthesized,
    })


@log_viewer_bp.route("/pick_event_log", methods=["POST"])
def pick_event_log():
    """Manually choose the System Event Log (.evtx/.evt) — overrides/supplies
    what auto-discovery (in /pick_log) couldn't find."""
    path = file_picker.pick_event_log_file()
    if not path:
        return jsonify({"success": False, "message": "No file selected"}), 400
    if not os.path.exists(path):
        return jsonify({"success": False, "message": "File not found"}), 400
    state = session_store.get_state()
    state.event_log_path = path
    return jsonify({"success": True, "event_log_path": path, "event_log_available": True})


@log_viewer_bp.route("/parse_event_log", methods=["POST"])
def parse_event_log():
    """A filtered page of the loaded System Event Log for the collapsible panel
    above the log. Reads state.event_log_path (set by /pick_log auto-discovery
    or /pick_event_log). Never 500s on a missing pywin32 / unreadable file —
    event_log_service returns an inline {'error': ...} the panel shows."""
    data = request.get_json(silent=True) or {}
    state = session_store.get_state()
    if not state.event_log_path:
        return jsonify({"error": "No event log loaded."})
    result = event_log_service.get_paged_events(
        state.event_log_path,
        offset=int(data.get("offset", 0) or 0),
        limit=int(data.get("limit", 0) or 0),
        source_filter=data.get("source_filter", "all"),
        level_filter=data.get("level_filter", "warning_error"),
    )
    return jsonify(result)


@log_viewer_bp.route("/pick_tat", methods=["POST"])
def pick_tat():
    path = file_picker.pick_tat_file()
    if not path:
        return jsonify({"success": False, "message": "No file selected"}), 400
    if not os.path.exists(path):
        return jsonify({"success": False, "message": "File not found"}), 400
    state = session_store.get_state()
    state.tat_path = path
    try:
        state.filters = tat_parser.parse_filter_file(path)
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to parse .tat: {e}"}), 400
    operation_journal.record(state, "load_tat", text=path, label=os.path.basename(path))
    return jsonify({"success": True, "tat_path": path, "filters": state.filters,
                    "operations": operation_journal.payload(state)})


@log_viewer_bp.route("/load_skill", methods=["POST"])
def load_skill():
    data = request.get_json(silent=True) or {}
    skill_key = data.get("skill_key", "")
    skill = app_config.skills.get(skill_key) or app_config.bt_skills.get(skill_key)
    if not skill:
        return jsonify({"success": False, "message": "Skill not found"}), 404

    state = session_store.get_state()
    state.active_skill_key = skill_key
    if skill.tat_path and os.path.exists(skill.tat_path):
        state.tat_path = skill.tat_path
        state.filters = tat_parser.parse_filter_file(skill.tat_path)
    else:
        state.tat_path = ""
        state.filters = _filters_from_skill(skill)

    operation_journal.record(state, "load_skill", text=skill_key, label=skill.name)

    # Loading a named skill as the starting point means the engineer now has a
    # concrete baseline in mind — switch the conversation into PRIOR-knowledge
    # mode so the interview treats this skill's own content as already known
    # instead of asking about it again, and the eventual export stays scoped
    # to genuinely NEW knowledge instead of duplicating what's loaded (see
    # learning_service._INTERVIEW_PRIOR_CLAUSE). A bare .tat file (no skill
    # identity to compare descriptions/keywords against) does NOT do this.
    state.prior_knowledge = True

    return jsonify({
        "success": True,
        "tat_path": state.tat_path,
        "filters": state.filters,
        "expert_rules": skill.expert_rules,
        "description": skill.description,
        "operations": operation_journal.payload(state),
        "prior_knowledge": state.prior_knowledge,
    })


@log_viewer_bp.route("/toggle_filter", methods=["POST"])
def toggle_filter():
    data = request.get_json(silent=True) or {}
    idx = data.get("index")
    enabled = bool(data.get("enabled"))
    state = session_store.get_state()
    if idx is None or not (0 <= idx < len(state.filters)):
        return jsonify({"success": False, "message": "Invalid filter index"}), 400
    state.filters[idx]["enabled"] = enabled
    f = state.filters[idx]
    operation_journal.record(state, "toggle_on" if enabled else "toggle_off",
                             text=f["text"], excluding=f["excluding"])
    return jsonify({"success": True, "operations": operation_journal.payload(state)})


@log_viewer_bp.route("/add_filter", methods=["POST"])
def add_filter():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"success": False, "message": "Empty filter text"}), 400
    excluding = bool(data.get("excluding"))
    state = session_store.get_state()
    state.filters.append({
        "text": text, "enabled": True, "excluding": excluding,
        "case_sensitive": False, "regex": False,
        "fore_color": None, "back_color": "#ffe0e0" if excluding else "#e0f0ff",
    })
    operation_journal.record(state, "add_exclude" if excluding else "add_include",
                             text=text, excluding=excluding)
    return jsonify({"success": True, "filters": state.filters,
                    "operations": operation_journal.payload(state)})


@log_viewer_bp.route("/remove_filter", methods=["POST"])
def remove_filter():
    data = request.get_json(silent=True) or {}
    idx = data.get("index")
    state = session_store.get_state()
    if idx is None or not (0 <= idx < len(state.filters)):
        return jsonify({"success": False, "message": "Invalid filter index"}), 400
    removed = state.filters.pop(idx)
    operation_journal.record(state, "remove", text=removed["text"], excluding=removed["excluding"])
    return jsonify({"success": True, "filters": state.filters,
                    "operations": operation_journal.payload(state)})


@log_viewer_bp.route("/apply_filter", methods=["POST"])
def apply_filter():
    state = session_store.get_state()
    if not state.log_path or not os.path.exists(state.log_path):
        return jsonify({"success": False, "message": "Please pick a log file first"}), 400

    if not any(f["enabled"] and not f["excluding"] for f in state.filters):
        return jsonify({
            "success": False,
            "message": "No enabled including-filters — pick a .tat file, load a skill, or add a filter",
        }), 400

    try:
        # Reuse the SAME date anchor /pick_log computed (never re-derive it
        # here) so a dateless BT HCI / WiFi DDD log's synthesized timestamps
        # stay identical between the raw preview and every filtered run.
        anchor_date = date.fromisoformat(state.log_date_anchor) if state.log_date_anchor else None
        log_lines = helpers.read_log_file(state.log_path, fallback_date=anchor_date)
        stats = tat_parser.compute_filter_stats(log_lines, state.filters, preview_limit=1000)
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to filter log: {e}"}), 500

    # Keep a plain-text, token-friendly copy for the chat/learning system
    # prompts (services/learning_service.py, blueprints/chatbot) — never the
    # full 100k-line log, just the same capped/deduped preview shown in the UI.
    preview_texts = [p["text"] for p in stats["preview"]]
    processed = tat_parser.preprocess_log_for_llm(preview_texts)
    state.filtered_preview = tat_parser.group_similar_logs(processed)
    state.filter_stats = stats

    # Now that we have fresh per-keyword marginal stats, attach the measured
    # effect (unique hits contributed / noise dropped / survivor delta) to any
    # filter edits logged since the last run — that's what lets the interview
    # ask a grounded "why did you make this edit?" later.
    operation_journal.annotate_effects(state, stats)

    return jsonify({
        "success": True,
        "total_lines": stats["total_lines"],
        "total_matched": stats["surviving_count"],
        "overlap_count": stats["overlap_count"],
        "co_occurrence": stats["co_occurrence"],
        "time_span": stats["time_span"],
        "preview_count": len(stats["preview"]),
        "preview": stats["preview"][:500],
        "per_filter": stats["per_filter"],
        "operations": operation_journal.payload(state),
    })


@log_viewer_bp.route("/annotate_operation", methods=["POST"])
def annotate_operation():
    """Store the engineer's stated reason for one journal entry — from the
    inline 'why?' box next to that edit. This reason then feeds both the
    interview context and the final skill synthesis, so the captured judgment
    isn't lost."""
    data = request.get_json(silent=True) or {}
    seq = data.get("seq")
    reason = data.get("reason", "")
    state = session_store.get_state()
    if not isinstance(seq, int) or not operation_journal.annotate_reason(state, seq, reason):
        return jsonify({"success": False, "message": "Unknown operation"}), 400
    return jsonify({"success": True, "operations": operation_journal.payload(state)})


@log_viewer_bp.route("/answer_red_flag", methods=["POST"])
def answer_red_flag():
    """Answer to a PASSIVELY-triggered question (see operation_journal.
    detect_red_flags — a deterministic, zero-LLM-cost check that fires right
    after a filter run when an edit's effect looks like something worth
    asking about: a redundant keyword, a no-op exclude, a big unexplained
    drop, disabling something load-bearing). No LLM call here either — just
    records the answer as that operation's reason AND drops both the
    question and the answer into chat_history so it reads naturally in the
    conversation and feeds skill synthesis exactly like any other Q&A."""
    data = request.get_json(silent=True) or {}
    seq = data.get("seq")
    question = (data.get("question") or "").strip()
    answer = (data.get("answer") or "").strip()
    if not answer:
        return jsonify({"success": False, "message": "Empty answer"}), 400

    state = session_store.get_state()
    if isinstance(seq, int):
        operation_journal.annotate_reason(state, seq, answer)
    tag = seq if isinstance(seq, int) else "all"
    if question:
        state.chat_history.append({"role": "assistant", "content": f"🚩 {question}", "step": tag})
    state.chat_history.append({"role": "user", "content": answer, "step": tag})
    return jsonify({"success": True, "operations": operation_journal.payload(state)})
