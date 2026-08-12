import json

from flask import Blueprint, Response, request, jsonify, stream_with_context

from configs.global_configs import app_config
from services import session_store, decision_ledger
from utils.json_utils import parse_json_loose

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

PROACTIVE_RESPONSE_SCHEMA = """
The current comparison baseline is included below. Treat the engineer's newest
message as potential teaching and compare it against that baseline. When
prior-knowledge mode is ON, also compare it against the explicitly loaded
skill knowledge.

Return ONLY one JSON object:
{
  "reply": "your concise normal response",
  "clarification": {
    "detected": true,
    "basis": "baseline | loaded_skill | both",
    "summary": "what is new, contradictory, or outside prior knowledge",
    "question": "one highest-information follow-up question",
    "type": "choice | open",
    "options": ["2-4 short mutually exclusive choices when type is choice"],
    "recommended_answer": "your evidence-grounded recommendation, or empty",
    "recommendation_reason": "one short evidence-grounded reason, or empty"
  }
}

Set clarification.detected=false and the remaining clarification fields to
empty values when the newest message only agrees with known knowledge or
contains no teachable claim. Ask at most ONE question. Use "choice" only when
there are genuinely finite alternatives; use "open" for reasons, diagnostic
rules, thresholds, or missing context. A difference is not automatically an
error: it is candidate expert knowledge whose boundary should be clarified.
"""


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


def _selected_skill_documents(state, domain: str = "wifi") -> str:
    """Full read-only docs chosen in the Question context picker."""
    pool = _skill_pool(domain)
    keys = [key for key in state.selected_skill_keys if key in pool]
    if not keys and state.prior_knowledge:  # legacy session/API compatibility
        keys = [state.active_skill_key] if state.active_skill_key in pool else list(pool)
    rows = []
    for key in keys:
        sk = pool[key]
        rules = str(sk.expert_rules or "").strip()
        if len(rules) > 1800:
            rules = rules[:1800].rstrip() + "\n  ... (document truncated)"
        rows.append(
            f"[SKILL DOC {key}] {sk.name}\n"
            f"  Scope: {sk.description}\n"
            f"  Triggers: {'; '.join(sk.triggers or []) or '(none)'}\n"
            f"  Keywords: {', '.join(sk.keywords or []) or '(none)'}\n"
            f"  Rules:\n{rules or '  (none)'}"
        )
    return "\n\n".join(rows)


def _chronological_sample(lines: list, limit: int = 60) -> list:
    """Bounded head+tail context that never changes the source order."""
    if len(lines) <= limit:
        return lines
    head_n = limit * 2 // 3
    return lines[:head_n] + ["... (chronological middle omitted) ..."] + lines[-(limit - head_n):]


def _build_system_prompt(state) -> str:
    domain = (state.log_domain or "wifi").lower()
    parts = [BASE_SYS_PROMPT]
    if state.case_summary:
        parts.append(
            "=== ENGINEER-PROVIDED CASE SUMMARY ===\n"
            + state.case_summary
            + "\nUse this to orient the case, but verify it against the chronological "
              "log evidence. If summary and log disagree, ask about that difference."
        )
    if state.has_current_baseline():
        baseline = state.baseline or {}
        key_rows = "\n".join(
            f"  - {row.get('text')}: {row.get('why') or '(no reason)'}"
            for row in (baseline.get("expected_key_keywords") or [])
        )
        noise_rows = "\n".join(
            f"  - {row.get('text')}: {row.get('why') or '(no reason)'}"
            for row in (baseline.get("expected_noise_keywords") or [])
        )
        unknowns = "\n".join(f"  - {u}" for u in (baseline.get("open_unknowns") or []))
        parts.append(
            "=== COMMITTED COMPARISON BASELINE ===\n"
            f"Scenario: {baseline.get('expected_scenario') or '(not identified)'}\n"
            f"Interpretation: {baseline.get('analysis') or '(none)'}\n"
            f"Expected key signals:\n{key_rows or '  - (none)'}\n"
            f"Expected noise:\n{noise_rows or '  - (none)'}\n"
            f"Open unknowns:\n{unknowns or '  - (none)'}"
        )
        if state.interview_mode != "quiet":
            parts.append(PROACTIVE_RESPONSE_SCHEMA)
        if state.interview_mode == "ask":
            parts.append(
                "INTERVIEW MODE — ASK: interrupt only for meaningful new or "
                "divergent knowledge; ordinary agreement produces no question. "
                "When you do ask, take the single highest-impact unresolved "
                "branch — the one that would change scope, triggers, keywords, "
                "exclusions, or an expert rule. A material omission may warrant "
                "clarification even when it is not a contradiction. Ask only ONE."
            )
        else:
            parts.append(
                "INTERVIEW MODE — QUIET: answer and analyze normally, but do not "
                "ask a follow-up question. Missing decisions stay passive."
            )

    # The picker is authoritative: zero documents means session-only; checked
    # documents are the exact prior professional knowledge allowed here.
    if state.prior_knowledge:
        docs = _selected_skill_documents(state, domain)
        if docs:
            parts.append(
                "=== SELECTED READ-ONLY SKILL DOCUMENTS ===\n" + docs +
                "\nThese documents are prior knowledge, not log evidence. Do not re-ask "
                "what they already cover; use the current log and case summary to ask only "
                "about a meaningful gap, conflict, boundary, or new rule."
            )
    stats_summary = _stats_summary(state)
    if stats_summary:
        parts.append("=== Operation pattern (tool-computed, not the raw log) ===\n" + stats_summary)
    if state.filtered_preview:
        sample = "\n".join(_chronological_sample(state.filtered_preview))
        parts.append(
            "=== CHRONOLOGICAL FILTERED LOG EVIDENCE (bounded head + tail) ===\n"
            + sample
        )
    # Mirrors the Log Round / Teach-Step conversation mode (the "Load skills"
    # document picker, or auto-enabled when a named skill is loaded — see
    # log_viewer_routes.load_skill) — without this, a natural follow-up typed
    # here after answering a teach-step question could re-ask about knowledge
    # an existing skill above already covers, defeating the whole point of
    # PRIOR mode.
    if state.prior_knowledge:
        parts.append(
            "CONVERSATION MODE — WITH PRIOR KNOWLEDGE: treat everything the "
            "selected skill documents above already cover as GIVEN — do not ask the "
            "engineer to re-explain it. Only ask about knowledge that's "
            "genuinely NEW beyond what those skills already hold."
        )
    return "\n\n".join(parts)


def _parse_chat_response(raw, expect_structured: bool):
    """Tolerant structured-chat parser.

    A provider returning ordinary prose must still work; it simply produces
    no proactive clarification card for that turn.
    """
    text = str(raw or "").strip()
    if not expect_structured:
        return text, None
    parsed = parse_json_loose(text)
    if not parsed:
        return text, None
    reply = str(parsed.get("reply") or "").strip() or text
    item = parsed.get("clarification") or {}
    question = str(item.get("question") or "").strip()
    if not item.get("detected") or not question:
        return reply, None
    options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()][:4]
    qtype = "choice" if item.get("type") == "choice" and len(options) >= 2 else "open"
    basis = str(item.get("basis") or "baseline").strip().lower()
    if basis not in {"baseline", "loaded_skill", "both"}:
        basis = "baseline"
    return reply, {
        "question": question,
        "type": qtype,
        "options": options if qtype == "choice" else [],
        "basis": basis,
        "summary": str(item.get("summary") or "").strip(),
        "recommended_answer": str(item.get("recommended_answer") or "").strip(),
        "recommendation_reason": str(item.get("recommendation_reason") or "").strip(),
    }


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

    state = session_store.get_state()
    if not state.has_current_baseline() and data.get("allow_without_baseline") is not True:
        return jsonify({
            "success": False,
            "baseline_required": True,
            "message": "Set the comparison baseline first, or explicitly allow this one message.",
        }), 409

    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({
            "success": False,
            "message": "LLM is not configured yet. See README.md → 'Set up LLM'.",
        }), 503

    state.chat_history.append({"role": "user", "content": message, "step": step_tag})
    # Resolve before the model call: the engineer's answer remains captured
    # even if the provider is temporarily unavailable. Fall back to the
    # latest open decision for this step when the client sent no decision_id
    # (a plain reply typed into the main chat box, not a question card's own
    # answer box) — otherwise that answer never reaches the ledger.
    decision_ledger.resolve(
        state,
        data.get("decision_id") or (decision_ledger.latest_open(state, step_tag) or {}).get("id"),
        data.get("decision_answer") or message,
    )
    try:
        # Strip the "step" tag before handing history to the API — it's UI
        # metadata for this app's own message-tagging (see chat_history.step
        # above), not part of the OpenAI-compatible message schema; passing
        # it through as an extra key on each message dict risks a 400 from a
        # strict-schema provider.
        api_messages = [{"role": m["role"], "content": m["content"]} for m in state.chat_history[-20:]]
        raw_reply = llm_helper.chat(
            messages=api_messages,
            system_content=_build_system_prompt(state),
        )
        reply, clarification = _parse_chat_response(
            raw_reply,
            expect_structured=(
                state.has_current_baseline() and state.interview_mode != "quiet"
            ),
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500

    # The reply inherits the SAME tag as the message that prompted it — e.g.
    # if the engineer selected step #5 before asking, the LLM's answer is
    # tagged #5 too, not "all".
    state.chat_history.append({"role": "assistant", "content": reply, "step": step_tag})
    if clarification:
        item = decision_ledger.record_question(
            state,
            source="chat",
            question=clarification["question"],
            qtype=clarification["type"],
            options=clarification["options"],
            basis=clarification["basis"],
            summary=clarification["summary"],
            recommended_answer=clarification["recommended_answer"],
            recommendation_reason=clarification["recommendation_reason"],
            step=step_tag,
        )
        if item:
            clarification["decision_id"] = item["id"]
    return jsonify({"success": True, "reply": reply, "clarification": clarification,
                    "decision_ledger": decision_ledger.payload(state),
                    "step_tag": step_tag, "usage": {
        "last": llm_helper.last_usage, "session": llm_helper.session_usage,
    }})


@chatbot_bp.route("/send_stream", methods=["POST"])
def send_stream():
    """Same contract as /send, but as a cancellable streaming response: the
    engineer's Stop button aborts the fetch, which disconnects this request,
    which unwinds llm_helper.chat_stream()'s generator and closes the
    provider connection early — an actual stop, not just the UI giving up on
    waiting for an answer it discards. The frontend doesn't render partial
    text (the JSON-clarification schema below can't be shown mid-stream);
    it only uses the ability to end the request early. See sendMsg() /
    stopChatSend() in log_viewer.js.

    Streamed as one SSE-style `event: done` frame carrying the exact same
    JSON shape /send returns, so the two share one response-handling path
    on the frontend."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "message": "Empty message"}), 400
    step_tag = data.get("step_tag")
    if not isinstance(step_tag, int) and step_tag != "all":
        step_tag = "all"

    state = session_store.get_state()
    if not state.has_current_baseline() and data.get("allow_without_baseline") is not True:
        return jsonify({
            "success": False,
            "baseline_required": True,
            "message": "Set the comparison baseline first, or explicitly allow this one message.",
        }), 409

    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({
            "success": False,
            "message": "LLM is not configured yet. See README.md → 'Set up LLM'.",
        }), 503

    state.chat_history.append({"role": "user", "content": message, "step": step_tag})
    # See /send above: fall back to the latest open decision for this step
    # when no decision_id was sent, so a plain chat reply still resolves it.
    decision_ledger.resolve(
        state,
        data.get("decision_id") or (decision_ledger.latest_open(state, step_tag) or {}).get("id"),
        data.get("decision_answer") or message,
    )
    api_messages = [{"role": m["role"], "content": m["content"]} for m in state.chat_history[-20:]]
    system_content = _build_system_prompt(state)
    expect_structured = state.has_current_baseline() and state.interview_mode != "quiet"

    def generate():
        chunks = []
        try:
            for delta in llm_helper.chat_stream(messages=api_messages, system_content=system_content):
                chunks.append(delta)
                yield ": keep-alive\n\n"  # no partial text is rendered; this just keeps the connection open/flushed
        except GeneratorExit:
            # The engineer clicked Stop (or navigated away). Best-effort:
            # keep whatever text had already arrived rather than silently
            # dropping it, clearly marked as cut short.
            partial = "".join(chunks).strip()
            if partial:
                state.chat_history.append({
                    "role": "assistant", "content": partial + "\n\n_(stopped early)_", "step": step_tag,
                })
            raise
        except Exception as e:
            yield f"event: done\ndata: {json.dumps({'success': False, 'message': f'LLM call failed: {e}'})}\n\n"
            return

        raw_reply = "".join(chunks)
        reply, clarification = _parse_chat_response(raw_reply, expect_structured=expect_structured)
        state.chat_history.append({"role": "assistant", "content": reply, "step": step_tag})
        if clarification:
            item = decision_ledger.record_question(
                state,
                source="chat",
                question=clarification["question"],
                qtype=clarification["type"],
                options=clarification["options"],
                basis=clarification["basis"],
                summary=clarification["summary"],
                recommended_answer=clarification["recommended_answer"],
                recommendation_reason=clarification["recommendation_reason"],
                step=step_tag,
            )
            if item:
                clarification["decision_id"] = item["id"]
        meta = {
            "success": True, "reply": reply, "clarification": clarification,
            "decision_ledger": decision_ledger.payload(state),
            "step_tag": step_tag,
            "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
        }
        yield f"event: done\ndata: {json.dumps(meta)}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@chatbot_bp.route("/reset", methods=["POST"])
def reset():
    """Explicit "start over" — the engineer clicked Clear. Wipes the chat AND
    the readiness/round/operation-journal state that a skill Save no longer
    clears on its own (see WorkingState.reset_teaching_progress)."""
    state = session_store.get_state()
    state.chat_history = []
    state.reset_teaching_progress()
    return jsonify({"success": True})
