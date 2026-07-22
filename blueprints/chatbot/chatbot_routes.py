from flask import Blueprint, request, jsonify

from configs.global_configs import app_config
from services import session_store

# No standalone page: the chat panel lives inside the combined Log Viewer
# workbench (templates/log_viewer.html) — this blueprint only exposes the
# /send and /reset APIs that page calls.
chatbot_bp = Blueprint("chatbot", __name__, url_prefix="/chatbot")

BASE_SYS_PROMPT = (
    "You are an expert Wi-Fi/Bluetooth log analyst AND a knowledge-base builder, "
    "working alongside an engineer in a log-triage tool. You do two things:\n"
    "1. Analyze the filtered log excerpt — cite exact log lines/timestamps as "
    "evidence; if the excerpt lacks evidence for a claim, say so instead of "
    "guessing.\n"
    "2. Help the engineer turn this filtering session into a reusable analysis "
    "\"skill\" (same idea as IntelAvatar's log-analysis agent skills). When the "
    "engineer's intent is unclear, ask targeted questions that surface the "
    "three things a good skill needs but you lack: (a) the key domain knowledge "
    "& hard diagnostic rules behind these lines, (b) a one-sentence scope "
    "description that does NOT overlap the other existing skills listed below, "
    "and (c) the minimal set of key filter keywords that still isolates this "
    "scenario. Ground your questions in the operation-pattern stats (hit counts "
    "and which keywords co-fire), not vague generalities. Keep replies concise."
)


def _stats_summary(state) -> str:
    """Compact operation-pattern summary (never the raw 100k-line log) so the
    chat is grounded in what the filter actually matched."""
    stats = state.filter_stats or {}
    if not stats:
        return ""
    parts = []
    total = stats.get("total_lines")
    survived = stats.get("surviving_count")
    overlap = stats.get("overlap_count")
    span = stats.get("time_span") or {}
    span_txt = f" Time span {span.get('first')} → {span.get('last')}." if span.get("first") else ""
    parts.append(
        f"{survived}/{total} lines survived; {overlap} matched 2+ include-keywords."
        + span_txt
    )
    per_filter = [pf for pf in stats.get("per_filter", []) if pf.get("hits")]
    if per_filter:
        rows = "\n".join(
            f"  - {'[EXCLUDING] ' if pf['excluding'] else ''}\"{pf['text']}\": {pf['hits']} hits"
            for pf in per_filter
        )
        parts.append("Per-keyword hits:\n" + rows)
    co = stats.get("co_occurrence") or []
    if co:
        rows = "\n".join(f"  - \"{c['a']}\" + \"{c['b']}\": {c['count']} lines" for c in co)
        parts.append("Keyword co-occurrence (operation pattern):\n" + rows)
    return "\n".join(parts)


def _skill_pool(domain: str) -> dict:
    return app_config.bt_skills if domain == "bt" else app_config.skills


def _other_skill_descriptions(active_key: str, domain: str = "wifi") -> str:
    rows = [
        f"  - {sk.name}: {sk.description}"
        for key, sk in _skill_pool(domain).items() if key != active_key
    ]
    return "\n".join(rows)


def _build_system_prompt(state) -> str:
    domain = (state.log_domain or "wifi").lower()
    parts = [BASE_SYS_PROMPT]
    others = _other_skill_descriptions(state.active_skill_key, domain)
    if others:
        parts.append("=== Existing skills (keep any new skill's scope distinct from these) ===\n" + others)
    skill = _skill_pool(domain).get(state.active_skill_key) if state.active_skill_key else None
    if skill and skill.expert_rules:
        parts.append(f"=== Expert Rules for the loaded skill '{skill.name}' ===\n{skill.expert_rules}")
    stats_summary = _stats_summary(state)
    if stats_summary:
        parts.append("=== Operation pattern (tool-computed, not the raw log) ===\n" + stats_summary)
    if state.filtered_preview:
        sample = "\n".join(state.filtered_preview[:200])
        parts.append(f"=== Filtered Log Excerpt (first 200 surviving lines) ===\n{sample}")
    # Mirrors the Log Round / Teach-Step conversation mode (the "Load skills"
    # header toggle, or auto-enabled when a named skill is loaded — see
    # log_viewer_routes.load_skill) — without this, a natural follow-up typed
    # here after answering a teach-step question could re-ask about knowledge
    # an existing skill above already covers, defeating the whole point of
    # PRIOR mode.
    if state.prior_knowledge:
        parts.append(
            "CONVERSATION MODE — WITH PRIOR KNOWLEDGE: treat everything the "
            "existing skills above already cover as GIVEN — do not ask the "
            "engineer to re-explain it. Only ask about knowledge that's "
            "genuinely NEW beyond what those skills already hold."
        )
    return "\n\n".join(parts)


@chatbot_bp.route("/send", methods=["POST"])
def send():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "message": "Empty message"}), 400
    # Which step (or "all" for general/session-wide knowledge) this message is
    # about — set by the step-context selector above the chat input, or
    # implicitly "all" for a bare typed question. See log_viewer.html's
    # currentStepTag / appendMsg's tag badge.
    step_tag = data.get("step_tag")
    if not isinstance(step_tag, int) and step_tag != "all":
        step_tag = "all"

    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({
            "success": False,
            "message": "LLM is not configured yet. See README.md → 'Set up LLM'.",
        }), 503

    state = session_store.get_state()
    state.chat_history.append({"role": "user", "content": message, "step": step_tag})
    try:
        # Strip the "step" tag before handing history to the API — it's UI
        # metadata for this app's own message-tagging (see chat_history.step
        # above), not part of the OpenAI-compatible message schema; passing
        # it through as an extra key on each message dict risks a 400 from a
        # strict-schema provider.
        api_messages = [{"role": m["role"], "content": m["content"]} for m in state.chat_history[-20:]]
        reply = llm_helper.chat(
            messages=api_messages,
            system_content=_build_system_prompt(state),
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500

    # The reply inherits the SAME tag as the message that prompted it — e.g.
    # if the engineer selected step #5 before asking, the LLM's answer is
    # tagged #5 too, not "all".
    state.chat_history.append({"role": "assistant", "content": reply, "step": step_tag})
    return jsonify({"success": True, "reply": reply, "step_tag": step_tag, "usage": {
        "last": llm_helper.last_usage, "session": llm_helper.session_usage,
    }})


@chatbot_bp.route("/reset", methods=["POST"])
def reset():
    state = session_store.get_state()
    state.chat_history = []
    return jsonify({"success": True})
