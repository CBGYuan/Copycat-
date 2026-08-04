import os
from datetime import date, datetime

from flask import Blueprint, render_template, request, jsonify

from configs.global_configs import app_config
from services import session_store, skill_memory, event_log_service, decision_ledger
from utils import file_picker, tat_parser, helpers, operation_journal, divergence

log_viewer_bp = Blueprint("log_viewer", __name__, url_prefix="/log_viewer")


# ---- Read+canonicalize cache ------------------------------------------
# /apply_filter used to call helpers.read_log_file() fresh on EVERY request —
# a full re-read + re-canonicalize (a regex pass per line trying each of the
# BT/WiFi timestamp formats) of the whole file, every single checkbox toggle.
# On a multi-million-line capture that's a multi-second stall for something
# that should feel instant. Cached here keyed by (path, mtime, fallback_date)
# so a changed file or a different date anchor invalidates it; only the most
# recently loaded file is kept, same bounded-memory idea as
# event_log_service._get_cached_raw_rows. The timestamp index (for the focus
# window below) is built lazily — most sessions never use Focus, so paying
# for it up front on every load would be wasted work for them.
_LOG_CACHE: dict = {}


def _cached_log_lines(path: str, fallback_date) -> list:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    entry = _LOG_CACHE.get(path)
    if not entry or entry["mtime"] != mtime or entry["fallback_date"] != fallback_date:
        entry = {
            "mtime": mtime, "fallback_date": fallback_date,
            "lines": helpers.read_log_file(path, fallback_date=fallback_date),
            "ts_index": None,
        }
        _LOG_CACHE.clear()
        _LOG_CACHE[path] = entry
    return entry["lines"]


def _cached_timestamp_index(path: str, lines: list) -> list:
    entry = _LOG_CACHE.get(path)
    if entry is None:
        # Shouldn't happen in practice (lines always come from the cache
        # above first) — build it uncached rather than crash.
        return tat_parser.build_timestamp_index(lines)
    if entry.get("ts_index") is None:
        entry["ts_index"] = tat_parser.build_timestamp_index(lines)
    return entry["ts_index"]


def _parse_focus_time(raw_time: str, lines: list):
    """Parse a typed "issue time" into a datetime. Accepts a full canonical
    timestamp ("MM/DD/YYYY-HH:MM:SS[.mmm]") or a bare time-of-day
    ("HH:MM[:SS[.mmm]]"), the latter combined with the loaded log's own
    first parseable line's date so the engineer doesn't have to type it —
    they're looking at a log full of bare times, typing one back should just
    work. Returns None if nothing parses."""
    raw_time = raw_time.strip()
    for fmt in ("%m/%d/%Y-%H:%M:%S.%f", "%m/%d/%Y-%H:%M:%S"):
        try:
            return datetime.strptime(raw_time, fmt)
        except ValueError:
            pass
    t = None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(raw_time, fmt).time()
            break
        except ValueError:
            continue
    if t is None:
        return None
    first_ts = None
    for line in lines:
        ts = tat_parser._leading_timestamp(line)
        if ts:
            first_ts = ts
            break
    if not first_ts:
        return None
    base_date = datetime.strptime(first_ts, tat_parser._TS_DT_FMT).date()
    return datetime.combine(base_date, t)


# Skills carry no color info of their own (only a .tat file does, straight
# from TextAnalysisTool.NET's own foreColor/backColor attributes) — without
# an assigned palette, skill-loaded filters rendered as plain uncolored rows
# in both the filter table AND the highlighted lines in the log pane below,
# losing the whole point of the TAT-style coloring for the (very common)
# case of loading a plain skill instead of a .tat file. Cycled by keyword
# order, same "auto-assign since nothing was specified" behavior TAT itself
# falls back to.
#
# Alternates two TREATMENTS of the same hue instead of making every single
# filter a full solid block — a wall of a dozen saturated background bars
# stacked on top of each other read as "too much color" even though each one
# individually was legible. "solid" = white text on the full color (loud,
# stands out at a glance); "text" = just the color on the log's own
# background (quieter — still instantly distinguishable by color, without
# adding to the block-of-color feel). Both still carry the SAME hue per
# palette slot, so a filter's identity color is consistent between its
# row in the table above and every line it highlights below regardless of
# which treatment it drew.
_SKILL_FILTER_PALETTE = [
    {"hue": "#3B82C4", "style": "solid"},  # blue
    {"hue": "#3FA772", "style": "text"},   # green
    {"hue": "#DD6B6B", "style": "solid"},  # coral red
    {"hue": "#8266C9", "style": "text"},   # violet
    {"hue": "#D98F3B", "style": "solid"},  # amber
    {"hue": "#33A6A1", "style": "text"},   # teal
    {"hue": "#C55FA0", "style": "solid"},  # magenta
    {"hue": "#6B7BAE", "style": "text"},   # slate blue
]


def _filters_from_skill(skill) -> list:
    """Synthesize TAT-style filter dicts from a skill's plain keywords/exclusive
    lists (used when the skill has no .tat_path of its own)."""
    filters = []
    for i, k in enumerate(skill.keywords or []):
        slot = _SKILL_FILTER_PALETTE[i % len(_SKILL_FILTER_PALETTE)]
        if slot["style"] == "solid":
            fore, back = "#ffffff", slot["hue"]
        else:
            fore, back = slot["hue"], None
        filters.append({
            "text": k, "enabled": True, "excluding": False,
            "case_sensitive": False, "regex": False,
            "fore_color": fore, "back_color": back,
        })
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
    # Name of the skill the next Export inherits from, resolved across BOTH
    # pools: the baseline can be set from the Skill Library, which lists WiFi
    # and BT together, so looking it up in only one pool would silently render
    # an empty badge for a BT baseline.
    baseline = (app_config.skills.get(state.active_skill_key)
                or app_config.bt_skills.get(state.active_skill_key))
    return render_template("log_viewer.html", state=state, skills=app_config.skills,
                            llm_ready=llm_ready, session_usage=session_usage,
                            baseline_skill_name=baseline.name if baseline else "",
                            # The RENDERED journal, not state.operations. The raw
                            # entries carry `action` but none of the display
                            # fields payload() derives (`verb`, `effect_phrase`,
                            # the label fallback) — seeding the Steps panel from
                            # them is why every step read `undefined "..."` after
                            # a reload while looking correct during the session.
                            operations=operation_journal.payload(state),
                            decision_ledger=decision_ledger.payload(state),
                            has_baseline=state.has_current_baseline())


RAW_PREVIEW_LINES = 500


def _raw_preview(path: str, anchor_date, focus_dt=None, focus_window_min: int = 5):
    """Uncoloured (no filter applied) preview of a log file — shared by
    /pick_log (right after a file is chosen), /show_all (jumping back to it
    from a filtered view without re-opening the file dialog), and /set_focus
    (jumping to a ±focus_window_min slice around an issue time). `total_lines`
    is always the TRUE full-file count regardless of focus, so the UI can
    still show "N of <full total>"."""
    log_lines = _cached_log_lines(path, anchor_date)
    total_lines = len(log_lines)
    start_idx = 0
    window = log_lines
    if focus_dt is not None:
        ts_index = _cached_timestamp_index(path, log_lines)
        start_idx, window = tat_parser.slice_by_focus_window(log_lines, ts_index, focus_dt, focus_window_min)
    preview = [
        {"line_no": start_idx + i, "text": line.rstrip("\n"), "back_color": None, "fore_color": None}
        for i, line in enumerate(window[:RAW_PREVIEW_LINES], start=1)
    ]
    return total_lines, preview


@log_viewer_bp.route("/pick_log", methods=["POST"])
def pick_log():
    path = file_picker.pick_log_file()
    if not path:
        return jsonify({"success": False, "message": "No file selected"}), 400
    if not os.path.exists(path):
        return jsonify({"success": False, "message": "File not found"}), 400
    state = session_store.get_state()
    prev_domain = state.log_domain
    state.log_path = path
    state.log_domain = helpers.detect_log_domain(path)
    # A focus window from whatever log was previously loaded means nothing
    # for this one — an issue time in the old file's frame could silently
    # slice the new file down to zero lines, which would look identical to
    # "this log failed to load."
    state.focus_center_iso = ""
    # Loading a different log is the OTHER explicit "start over" trigger — a
    # skill Save no longer clears readiness/round/operation-journal state on
    # its own (see WorkingState.reset_teaching_progress), so switching logs
    # is what stops a NEW log's session from opening with stale readiness
    # left over from whatever was previously loaded.
    state.reset_teaching_progress()

    # Switching domains (e.g. a WiFi capture -> a BT one) leaves whatever
    # filter/skill was active pointed at keywords that don't exist in the new
    # log's vocabulary at all. state.filters otherwise survives a log switch
    # on purpose (re-picking the SAME-domain log keeps your filter), but
    # across a domain change that silent carry-over means /apply_filter
    # quietly re-runs the old filter and comes back with "0 lines matched" —
    # which reads exactly like the new log failed to load, not like "your old
    # filter doesn't apply here." Clear it so the frontend shows its honest
    # empty-filter state instead.
    filters_cleared = bool(prev_domain and state.log_domain != prev_domain and state.filters)
    if filters_cleared:
        state.filters = []
        state.filter_stats = {}

    # Auto-discover the System Event Log sitting next to the driver log so the
    # collapsible event panel can light up without a manual pick (the engineer
    # can still override via /pick_event_log). Looked for on BOTH domains:
    # the discovery is purely "is there a System event export beside this
    # capture", which has nothing to do with WiFi vs BT — gating it on BT was
    # an assumption about which captures ship one, and a WiFi capture that
    # does ship one had no way to use it. A capture without one simply gets
    # "" here and the panel stays hidden exactly as before.
    state.event_log_path = event_log_service.find_event_log_near(path)
    # Same relative search for the CAPTURE MACHINE's fixed UTC offset (see
    # find_capture_utc_offset_minutes). This is what puts the two timestamp
    # sources in one frame: Windows stores TimeCreated/@SystemTime in UTC, the
    # driver log carries capture-machine-local time, so event + offset =
    # driver-log frame. Without it the click-sync compares two genuinely
    # different moments as raw numbers and lands on a plausible wrong line.
    state.capture_utc_offset_min = event_log_service.find_capture_utc_offset_minutes(path)

    # Some BT/WiFi driver-log exports carry only a time-of-day, no date at all
    # (BT's dateless HCI export, WiFi's DDD-player export) — a date must be
    # synthesized so every line still gets the canonical leading timestamp the
    # rest of the app reads (see helpers.read_log_file / _canonicalize_log_
    # line). Priority: the loaded System Event Log's earliest event (the
    # actual capture day, keeps the event panel's click-sync meaningful) →
    # the WiFi DDD filename's own encoded date (also the real capture day —
    # see extract_date_from_filename) → the log file's mtime as a last
    # resort, which only reflects when the file was copied/downloaded and can
    # be wrong (e.g. a capture downloaded days after recording). Stored on
    # state so /apply_filter's later full-log read uses the SAME anchor
    # (never re-derived, so the two reads can't disagree).
    date_synthesized = helpers.needs_date_synthesis(path)
    anchor_date = None
    if date_synthesized:
        if state.event_log_path:
            anchor_date = event_log_service.peek_event_log_date(state.event_log_path)
        if anchor_date is None:
            anchor_date = helpers.extract_date_from_filename(path)
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
        total_lines, raw_preview = _raw_preview(path, anchor_date)
    except Exception as e:
        print(f"⚠️  Could not build raw preview for {path}: {e}")

    return jsonify({
        "success": True,
        "log_path": path,
        "domain": state.log_domain,
        "total_lines": total_lines,
        "preview": raw_preview,
        "filters": state.filters,
        "filters_cleared": filters_cleared,
        "event_log_path": state.event_log_path,
        "event_log_available": bool(state.event_log_path),
        "capture_utc_offset_min": state.capture_utc_offset_min,
        # True when this log's own lines had no date (dateless BT HCI / WiFi
        # DDD export) and the leading timestamps shown are synthesized —
        # the UI surfaces this so the engineer knows the date component (not
        # the time-of-day) is an estimate, not something the log itself proved.
        "date_synthesized": date_synthesized,
    })


@log_viewer_bp.route("/show_all", methods=["POST"])
def show_all():
    """Jump the log pane back to the raw, unfiltered, unfocused view — same
    shape as /pick_log's initial preview, but reuses the already-loaded
    log_path/date anchor instead of opening the file dialog again. Also
    clears any active issue-time focus window (see /set_focus) — this is the
    explicit "show me literally everything" escape hatch, so a lingering
    focus window silently narrowing the result would defeat the point.
    Otherwise purely a view reset: does not touch filters, operations, or
    any teaching state, so re-applying the same filter afterward picks up
    exactly where it left off."""
    state = session_store.get_state()
    if not state.log_path or not os.path.exists(state.log_path):
        return jsonify({"success": False, "message": "No log loaded yet"}), 400
    state.focus_center_iso = ""
    anchor_date = date.fromisoformat(state.log_date_anchor) if state.log_date_anchor else None
    try:
        total_lines, raw_preview = _raw_preview(state.log_path, anchor_date)
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to read log: {e}"}), 500
    return jsonify({"success": True, "total_lines": total_lines, "preview": raw_preview})


@log_viewer_bp.route("/set_focus", methods=["POST"])
def set_focus():
    """Narrow the working log to a ±window_min-minute slice around a typed
    "issue time" — the whole point being that every subsequent /apply_filter
    call (toggling a checkbox, adding a keyword, …) then only has to scan
    that slice instead of the entire multi-million-line file. Persists on
    state.focus_center_iso until explicitly cleared (/clear_focus or
    /show_all) or a new log is picked, so it survives repeated filter edits
    the way the filter set itself does."""
    data = request.get_json(silent=True) or {}
    raw_time = str(data.get("time") or "").strip()
    window_min = data.get("window_min")
    state = session_store.get_state()
    if not state.log_path or not os.path.exists(state.log_path):
        return jsonify({"success": False, "message": "No log loaded yet"}), 400
    if not raw_time:
        return jsonify({"success": False, "message": "Enter a time to focus on"}), 400

    anchor_date = date.fromisoformat(state.log_date_anchor) if state.log_date_anchor else None
    try:
        lines = _cached_log_lines(state.log_path, anchor_date)
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to read log: {e}"}), 500
    focus_dt = _parse_focus_time(raw_time, lines)
    if focus_dt is None:
        return jsonify({
            "success": False,
            "message": f"Couldn't parse a time from \"{raw_time}\" — try HH:MM:SS or MM/DD/YYYY-HH:MM:SS",
        }), 400

    state.focus_center_iso = focus_dt.strftime(tat_parser._TS_DT_FMT)[:-3]
    state.focus_window_min = int(window_min) if window_min else 5

    total_lines, preview = _raw_preview(state.log_path, anchor_date, focus_dt, state.focus_window_min)
    return jsonify({
        "success": True,
        "focus_center": state.focus_center_iso,
        "focus_window_min": state.focus_window_min,
        "total_lines": total_lines,
        "window_lines": len(preview),
        "preview": preview,
    })


@log_viewer_bp.route("/clear_focus", methods=["POST"])
def clear_focus():
    """Drop the issue-time focus window without otherwise touching the
    view — filters keep whatever they currently show, just re-scanning the
    full file from here on. (Separate from /show_all, which ALSO discards
    any active filter to show the raw view — this is just "stop narrowing
    by time.")"""
    state = session_store.get_state()
    state.focus_center_iso = ""
    return jsonify({"success": True})


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
    return jsonify({
        "success": True, "event_log_path": path, "event_log_available": True,
        # The UTC-offset correction is about the CAPTURE MACHINE (from
        # systeminfo.txt near the driver log), not this evt file specifically
        # — already computed in /pick_log, just echoed back here so the
        # frontend has it regardless of which of the two picks ran last.
        "capture_utc_offset_min": state.capture_utc_offset_min,
    })


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
        auto_source=bool(data.get("auto_source")),
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
    # A raw .tat file replaces the filter set, so no skill produced what is on
    # screen any more. The teaching baseline (active_skill_key) is left alone —
    # loading a .tat is not a statement about what the export should inherit.
    state.filter_skill_key = ""
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
    # Both, because this route is the one place that does both things: it makes
    # the skill the teaching baseline AND puts its keywords on screen.
    state.active_skill_key = skill_key
    state.filter_skill_key = skill_key
    if skill.tat_path and os.path.exists(skill.tat_path):
        state.tat_path = skill.tat_path
        state.filters = tat_parser.parse_filter_file(skill.tat_path)
    else:
        state.tat_path = ""
        state.filters = _filters_from_skill(skill)

    operation_journal.record(state, "load_skill", text=skill_key, label=skill.name)
    skill_memory.record_load(skill_key)

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
    # An INCLUDE keyword added while a skill is the baseline is that skill
    # failing to cover something, stated by the engineer's hands. Counted
    # across sessions so a keyword added every single time becomes a concrete
    # "this skill should own it" suggestion (services.skill_memory). Excludes
    # are left out: dropping noise is tuning for one capture, not a gap.
    if state.filter_skill_key and not excluding:
        skill_memory.record_added_keyword(state.filter_skill_key, text)
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
        # _cached_log_lines avoids re-reading + re-canonicalizing the WHOLE
        # file on every single checkbox toggle — see its docstring.
        anchor_date = date.fromisoformat(state.log_date_anchor) if state.log_date_anchor else None
        log_lines = _cached_log_lines(state.log_path, anchor_date)
        full_total_lines = len(log_lines)
        start_line_no = 1
        focus_payload = None
        if state.focus_center_iso:
            try:
                focus_dt = datetime.strptime(state.focus_center_iso, tat_parser._TS_DT_FMT)
                ts_index = _cached_timestamp_index(state.log_path, log_lines)
                start_idx, log_lines = tat_parser.slice_by_focus_window(
                    log_lines, ts_index, focus_dt, state.focus_window_min)
                start_line_no = start_idx + 1
                focus_payload = {
                    "center": state.focus_center_iso,
                    "window_min": state.focus_window_min,
                    "window_lines": len(log_lines),
                }
            except ValueError:
                # Corrupted state value (shouldn't happen — only ever written
                # by /set_focus in this exact format) — ignore and run on the
                # full file rather than 500 the whole filter action over it.
                pass
        stats = tat_parser.compute_filter_stats(
            log_lines, state.filters, preview_limit=1000, start_line_no=start_line_no)
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to filter log: {e}"}), 500

    # Keep a plain-text, token-friendly copy for the chat/learning system
    # prompts (services/learning_service.py, blueprints/chatbot) — never the
    # full 100k-line log, just the same capped/deduped preview shown in the UI.
    preview_texts = [p["text"] for p in stats["preview"]]
    processed = tat_parser.preprocess_log_for_llm(preview_texts)
    state.filtered_preview = tat_parser.group_similar_logs(processed)
    state.filter_stats = stats
    # How much of the log this skill's filter set actually reaches, kept per
    # skill across sessions (services.skill_memory). Recorded on every run, so
    # the stored figure is the most recent one rather than whatever the very
    # first unedited load happened to match.
    if state.filter_skill_key:
        skill_memory.record_matched(state.filter_skill_key, stats["surviving_count"])

    # Now that we have fresh per-keyword marginal stats, attach the measured
    # effect (unique hits contributed / noise dropped / survivor delta) to any
    # filter edits logged since the last run — that's what lets the interview
    # ask a grounded "why did you make this edit?" later.
    operation_journal.annotate_effects(state, stats)

    return jsonify({
        "success": True,
        # total_lines is the WINDOW size while a focus is active (matches
        # what this scan actually covered — see stats["total_lines"]);
        # full_total_lines is always the true whole-file count so the UI can
        # still show "N of <full file total>" alongside the focus badge.
        "total_lines": stats["total_lines"],
        "full_total_lines": full_total_lines,
        "focus": focus_payload,
        "total_matched": stats["surviving_count"],
        "overlap_count": stats["overlap_count"],
        "co_occurrence": stats["co_occurrence"],
        "time_span": stats["time_span"],
        "preview_count": len(stats["preview"]),
        "preview": stats["preview"][:500],
        "per_filter": stats["per_filter"],
        "operations": operation_journal.payload(state),
        "annotations": state.log_annotations,
        # What the engineer just did that the baseline read didn't expect
        # (utils.divergence.detect) — computed here, with no LLM call, because
        # it runs on every filter edit. Contradictions are what may later be
        # worth ONE clarifying question; omissions drive the Steps panel's
        # "explain this edit" hints. See utils/divergence.py.
        "divergence": divergence.detect(state),
    })


@log_viewer_bp.route("/annotate_line", methods=["POST"])
def annotate_line():
    """Toggle one engineer-confirmed label (evidence/counterexample) on a
    visible source-log line.

    Every annotation is attributed to the FILTER(S)/keyword(s) that actually
    matched this line (see tat_parser.matched_keywords_for_line), using the
    caller-supplied `matched_filters` (the same per-line "matched" filter-
    index list compute_filter_stats already returns for the preview row).
    This is always unambiguous -- unlike attributing to a historical edit (a
    Step), a filter's keyword+role is present whether it was typed in by hand
    or came in wholesale from a loaded skill/.tat file, so there's no
    "couldn't correlate" state and no manual-correction UI needed.
    """
    data = request.get_json(silent=True) or {}
    line_no = data.get("line_no")
    label = str(data.get("label") or "").strip().lower()
    text = str(data.get("text") or "")
    matched_filters = data.get("matched_filters") or []
    allowed = {"evidence", "counterexample"}
    if not isinstance(line_no, int) or label not in allowed:
        return jsonify({"success": False, "message": "Invalid line annotation"}), 400

    state = session_store.get_state()
    existing = next((item for item in state.log_annotations if item["line_no"] == line_no), None)
    if existing and existing["label"] == label:
        state.log_annotations.remove(existing)
    else:
        matched_keywords = tat_parser.matched_keywords_for_line(state.filters, matched_filters)
        if existing:
            existing.update({"label": label, "text": text, "matched_keywords": matched_keywords})
        else:
            state.log_annotations.append({
                "line_no": line_no, "label": label, "text": text,
                "matched_keywords": matched_keywords,
            })
    return jsonify({"success": True, "annotations": state.log_annotations})


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
