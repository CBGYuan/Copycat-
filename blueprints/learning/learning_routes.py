from flask import Blueprint, request, jsonify

from configs.global_configs import app_config
from services import session_store, learning_service, skill_service
from utils import operation_journal

# No standalone page: the "Teach This Scenario" flow lives inline inside the
# combined Log Viewer workbench (templates/log_viewer.html) — this blueprint
# only exposes the /start, /submit_answers, /save APIs that page calls.
learning_bp = Blueprint("learning", __name__, url_prefix="/learning")


def _skill_pool(domain: str) -> dict:
    """The right skill dict for a domain ("wifi"/"bt") — the single place
    every route below goes through so WiFi and BT skills never get compared
    against or saved into each other's pool."""
    return app_config.bt_skills if domain == "bt" else app_config.skills


def _other_skill_descriptions(exclude_key: str = "", domain: str = "wifi") -> list:
    """name/description/keywords of the OTHER already-saved skills IN THE
    SAME DOMAIN — fed to the LLM so it can (a) keep a new skill's description
    mutually exclusive from them, the way Avatar's SKILL_DESCRIPTIONS map
    keeps each skill's scope distinct, and (b) spot literal keyword overlap
    to decide whether this scenario should extend one of them instead of
    duplicating it. Scoped to `domain` so a WiFi log is never compared
    against (or routed to extend) a Bluetooth skill, or vice versa."""
    out = []
    for key, sk in _skill_pool(domain).items():
        if key == exclude_key:
            continue
        out.append({"key": key, "name": sk.name, "description": sk.description, "keywords": sk.keywords})
    return out


def _store_assessment(state, result: dict) -> None:
    """Fan a learning_service assessment result out into the session fields
    that back the live badge + details panel."""
    state.last_readiness = result.get("readiness") or {}
    state.last_coverage = result.get("coverage") or {}
    state.last_gaps = result.get("gaps") or []
    state.last_validation = result.get("validation") or []


def _assessment_payload(state) -> dict:
    """The assessment shape the frontend consumes (badge + panel + export gate)."""
    return {
        "readiness": state.last_readiness,
        "coverage": state.last_coverage,
        "gaps": state.last_gaps,
        "validation": state.last_validation,
    }


def _baseline_skill(state):
    """The skill currently loaded as a starting point, if any — WiFi or BT."""
    if not state.active_skill_key:
        return None
    return app_config.skills.get(state.active_skill_key) or app_config.bt_skills.get(state.active_skill_key)


def compute_operation_delta(state) -> dict:
    """Generic, LLM-free diff between the currently-enabled filters and the
    loaded baseline skill's own keywords/exclusive (if any skill is loaded).

    This is the compact, skill-agnostic signal this whole interview should be
    grounded in: whatever the engineer just added/removed IS the new
    knowledge they're teaching, regardless of which skill or .tat they
    started from. Feeding just the delta (typically 1-5 keywords) instead of
    re-dumping a skill's full keyword list (connection_flow alone has 22)
    is what keeps the interview prompt small no matter how big the baseline
    skill already is — and it works identically for every skill type, not
    just this one, since it never looks at skill-specific content.
    """
    baseline = _baseline_skill(state)
    baseline_kw = set(baseline.keywords) if baseline else set()
    baseline_excl = set(baseline.exclusive) if baseline else set()

    current_incl = {f["text"] for f in state.filters if f["enabled"] and not f["excluding"]}
    current_excl = {f["text"] for f in state.filters if f["enabled"] and f["excluding"]}

    hits_by_text = {pf["text"]: pf["hits"] for pf in state.filter_stats.get("per_filter", [])}

    return {
        "baseline_skill_key": state.active_skill_key or None,
        "baseline_skill_name": baseline.name if baseline else None,
        "added_keywords": [
            {"text": t, "hits": hits_by_text.get(t, 0)} for t in sorted(current_incl - baseline_kw)
        ],
        "added_exclusive": sorted(current_excl - baseline_excl),
        "removed_keywords": sorted(baseline_kw - current_incl),
    }


def _sample_lines(lines: list, limit: int = 40, tail_fraction: float = 0.4) -> list:
    """Pick a HEAD + TAIL sample instead of always the chronological prefix.

    `lines` is already time-ordered (state.filtered_preview). Taking only
    `lines[:limit]` means once a filter survives more than `limit` lines, the
    LLM only ever sees how the scenario STARTS — never the failure/root-cause
    lines that typically cluster near the END of a survivor set (a final
    disconnect, the actual error line, ...). That skews both the interview
    questions (never asked about what happens near the end) and the
    verified/asserted validation (a true claim about the tail gets flagged
    "asserted" purely because its evidence wasn't in the sample, not because
    it's actually unverified).
    """
    if len(lines) <= limit:
        return lines
    tail_n = max(1, int(limit * tail_fraction))
    head_n = limit - tail_n
    return lines[:head_n] + ["... (middle lines omitted) ..."] + lines[-tail_n:]


def _context_from_state(state, exclude_skill_key: str = "", include_existing: bool = True) -> dict:
    domain = (state.log_domain or "wifi").lower()
    including = [f["text"] for f in state.filters if f["enabled"] and not f["excluding"]]
    excluding = [f["text"] for f in state.filters if f["enabled"] and f["excluding"]]
    return {
        "domain": domain,
        "tat_path": state.tat_path,
        "keywords": including,
        "excluding_terms": excluding,
        # Compact stats instead of raw log — keeps the clarifying-question
        # prompt cheap even when the source log is 100k+ lines. per-filter
        # hit counts + the intersection/overlap count + the co-occurrence
        # pairs (the "operation pattern") are enough for the LLM to ask a
        # good "why did you filter on X together with Y" question without
        # ever seeing the full file.
        "filter_stats": {
            "total_lines": state.filter_stats.get("total_lines"),
            "surviving_count": state.filter_stats.get("surviving_count"),
            "overlap_count": state.filter_stats.get("overlap_count"),
            "time_span": state.filter_stats.get("time_span"),
            "co_occurrence": state.filter_stats.get("co_occurrence", []),
            "per_filter": [
                {"text": f["text"], "excluding": f["excluding"], "hits": f["hits"]}
                for f in state.filter_stats.get("per_filter", [])
            ],
        } if state.filter_stats else {},
        "operation_delta": compute_operation_delta(state),
        # The edit-by-edit journal (what the engineer did, its measured effect,
        # and their stated reason if any) + the still-unexplained material
        # edits — this is what lets the interview ask a grounded "why did you
        # add/exclude X?" and capture the tacit judgment behind the filter.
        "operation_journal": operation_journal.compact(state),
        "unreasoned_ops": [
            {"seq": op["seq"], "text": op["text"], "action": op["action"],
             "effect_phrase": operation_journal._effect_phrase(op)}
            for op in operation_journal.unreasoned_material_ops(state)
        ],
        # Same-domain existing skills — fed for the interview + the PRIOR-mode
        # export (mutual-exclusion). The FRESH export passes include_existing=
        # False so no prior skill is consulted at all (the "no prior knowledge"
        # button), keeping that path token-cheap and guaranteeing it never
        # compares against anything already saved.
        "existing_skills": _other_skill_descriptions(exclude_skill_key, domain) if include_existing else [],
        "sample_lines": _sample_lines(state.filtered_preview, limit=40),
        "chat_history": state.chat_history,
    }


@learning_bp.route("/log_round", methods=["POST"])
def log_round():
    """The engineer just set the filter the way they want and is submitting
    it as one "round" of evidence. One LLM call does analysis + interview
    questions + a self-rated readiness score together, grounded in the
    operation-delta/stats context so it never needs the raw log. Reusable —
    the engineer can log a second (third, ...) round after changing the
    filter, or just keep chatting via the textbox without logging again.
    Answers still land as plain chat messages (see log_viewer.html), so
    /converge's chat_history-based synthesis needs no special-casing.

    The request body's `use_prior_knowledge` (which of the two Log Round
    buttons was clicked) sets the SESSION-STICKY conversation mode: FRESH
    teaches from scratch, PRIOR shows the same-domain existing skills so the
    interview only probes what's NEW beyond them (never re-asking covered
    knowledge). Once set it governs the per-answer assess + the final export
    too, so the whole conversation stays consistently in one mode."""
    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({"success": False, "message": "LLM is not configured yet."}), 503

    state = session_store.get_state()
    if not state.filtered_preview:
        return jsonify({
            "success": False,
            "message": "Run a filter first so there's an operation pattern to log.",
        }), 400

    data = request.get_json(silent=True) or {}
    state.prior_knowledge = bool(data.get("use_prior_knowledge"))

    state.round_count += 1
    # PRIOR mode shows the same-domain existing skills to the interview so it
    # can steer around them; FRESH omits them entirely.
    context = _context_from_state(state, exclude_skill_key="", include_existing=state.prior_knowledge)
    try:
        result = learning_service.analyze_round(
            llm_helper, context, state.round_count, use_prior_knowledge=state.prior_knowledge)
    except Exception as e:
        state.round_count -= 1
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500

    # result["assessment"] is None on a parse failure — keep whatever was
    # last stored instead of resetting readiness/coverage/gaps/validation to
    # zero (see analyze_round's docstring).
    if result.get("assessment") is not None:
        _store_assessment(state, result["assessment"])

    # Persist a plain-text rendering into chat_history so a page reload still
    # shows what was analyzed/asked (the interactive option buttons are
    # live-only, reconstructed client-side from the JSON response below).
    # step="all": a round spans the whole current filter state, not one edit.
    summary_parts = [f"**Round {state.round_count}:** {result.get('analysis', '')}"]
    if result.get("questions"):
        summary_parts.append("\n".join(f"{i+1}. {q['question']}" for i, q in enumerate(result["questions"])))
    state.chat_history.append({"role": "assistant", "content": "\n\n".join(summary_parts), "step": "all"})

    return jsonify({
        "success": True,
        "round": state.round_count,
        "analysis": result.get("analysis", ""),
        "questions": result.get("questions", []),
        "assessment": _assessment_payload(state),
        "prior_knowledge": state.prior_knowledge,
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


@learning_bp.route("/set_mode", methods=["POST"])
def set_mode():
    """Persist the header prior-knowledge toggle so the auto-fired background
    assess (and a page reload) reflect the current mode even before the next
    Log Round. No LLM call — just stores the flag."""
    data = request.get_json(silent=True) or {}
    state = session_store.get_state()
    state.prior_knowledge = bool(data.get("use_prior_knowledge"))
    return jsonify({"success": True, "prior_knowledge": state.prior_knowledge})


@learning_bp.route("/confirm_step", methods=["POST"])
def confirm_step():
    """'Teach this step' — USER-LED, scoped to ONE specific filter edit,
    triggered by the Steps panel's per-step teach icon (the panel's default
    click stays purely navigational — see toggleStepPanel's docstring in
    log_viewer.html). The engineer writes FIRST, in their own words, what key
    thing they noticed and what the problem/reasoning was — the LLM does not
    fire a question to start the interaction. It only condenses their
    explanation into a confirmable `knowledge_core` + adds its own
    `expert_note` (a genuine second opinion, not a restatement), with an
    OPTIONAL `follow_up_question` only when something is materially unclear.

    The explanation is recorded as this operation's `reason` immediately —
    before the LLM call even runs — so the "why" is captured even if the LLM
    call itself fails; the LLM's job here is purely to condense/confirm/add
    perspective on top of it, never to gate whether it's saved.

    Follows the SAME session conversation mode as Log Round
    (state.prior_knowledge): PRIOR mode is shown the same-domain existing
    skills so the knowledge core stays distinct from what they already
    cover — what keeps a step taught against a loaded baseline skill from
    duplicating that skill's own content in the eventual export."""
    data = request.get_json(silent=True) or {}
    seq = data.get("seq")
    explanation = (data.get("explanation") or "").strip()
    state = session_store.get_state()
    op = next((o for o in state.operations if o["seq"] == seq), None)
    if not isinstance(seq, int) or not op:
        return jsonify({"success": False, "message": "Unknown operation"}), 400
    if not explanation:
        return jsonify({"success": False, "message": "Empty explanation"}), 400

    operation_journal.annotate_reason(state, seq, explanation)
    state.chat_history.append({"role": "user", "content": explanation, "step": seq})

    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        # The reason is already saved above — an unconfigured LLM shouldn't
        # block that, just the condense/confirm/expert-note step on top of it.
        return jsonify({
            "success": True, "seq": seq, "knowledge_core": "", "expert_note": "",
            "follow_up_question": "", "llm_unavailable": True,
            "operations": operation_journal.payload(state),
        })

    domain = (state.log_domain or "wifi").lower()
    op_context = {
        "domain": domain,
        "verb": operation_journal._ACTION_VERB.get(op["action"], op["action"]),
        "target": op.get("label") or op["text"],
        "excluding": op["excluding"],
        "effect_phrase": operation_journal._effect_phrase(op),
        "existing_skills": _other_skill_descriptions("", domain) if state.prior_knowledge else [],
    }
    try:
        result = learning_service.confirm_step_knowledge(
            llm_helper, op_context, explanation, state.prior_knowledge)
    except Exception as e:
        # The reason is ALREADY saved above regardless of this failure —
        # include `operations` so the frontend still reflects the "✓ why"
        # update on this step instead of treating it as a total no-op.
        return jsonify({
            "success": False, "message": f"LLM call failed: {e}",
            "operations": operation_journal.payload(state),
        }), 500

    knowledge_core = result.get("knowledge_core", "")
    expert_note = result.get("expert_note", "")
    follow_up = result.get("follow_up_question", "")
    summary_parts = [f"**Step #{seq} — knowledge core:** {knowledge_core}"]
    if expert_note:
        summary_parts.append(f"*Expert note:* {expert_note}")
    if follow_up:
        summary_parts.append(f"*(optional follow-up: {follow_up})*")
    state.chat_history.append({"role": "assistant", "content": "\n\n".join(summary_parts), "step": seq})

    return jsonify({
        "success": True,
        "seq": seq,
        "knowledge_core": knowledge_core,
        "expert_note": expert_note,
        "follow_up_question": follow_up,
        "operations": operation_journal.payload(state),
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


@learning_bp.route("/ask_step", methods=["POST"])
def ask_step():
    """'Ask about this step' — the LLM-LED counterpart to confirm_step's
    user-led flow, triggered by a step's own ❓ icon (separate from 🎓's
    user-led explain box — the engineer picks whichever entry point suits
    them). Generates ONE targeted question about that single edit; the
    frontend renders it as a skippable question card (see
    learning_service.ASK_STEP_SYS_PROMPT and log_viewer.html's
    askStepQuestion/renderStepAskCard) — answering it goes to
    /learning/answer_step_question below, skipping just dismisses the card
    client-side with no backend call."""
    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({"success": False, "message": "LLM is not configured yet."}), 503

    data = request.get_json(silent=True) or {}
    seq = data.get("seq")
    state = session_store.get_state()
    op = next((o for o in state.operations if o["seq"] == seq), None)
    if not isinstance(seq, int) or not op:
        return jsonify({"success": False, "message": "Unknown operation"}), 400

    domain = (state.log_domain or "wifi").lower()
    op_context = {
        "domain": domain,
        "verb": operation_journal._ACTION_VERB.get(op["action"], op["action"]),
        "target": op.get("label") or op["text"],
        "excluding": op["excluding"],
        "effect_phrase": operation_journal._effect_phrase(op),
        "reason": op["reason"],
        "existing_skills": _other_skill_descriptions("", domain) if state.prior_knowledge else [],
    }
    try:
        question = learning_service.ask_step_question(llm_helper, op_context, state.prior_knowledge)
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500
    if not question:
        return jsonify({"success": False, "message": "Couldn't generate a question — try again."}), 500

    state.chat_history.append({"role": "assistant", "content": f"❓ {question['question']}", "step": seq})
    return jsonify({
        "success": True,
        "seq": seq,
        "question": question,
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


@learning_bp.route("/answer_step_question", methods=["POST"])
def answer_step_question():
    """Answer to a /learning/ask_step question. No LLM call — just records
    the Q&A into chat_history tagged to this step, and ONLY sets the
    operation's `reason` if it doesn't already have one (the 🎓 user-led
    explain box is the authoritative place for that; this shouldn't silently
    clobber a carefully-written explanation with a short answer to a
    secondary follow-up)."""
    data = request.get_json(silent=True) or {}
    seq = data.get("seq")
    question = (data.get("question") or "").strip()
    answer = (data.get("answer") or "").strip()
    state = session_store.get_state()
    op = next((o for o in state.operations if o["seq"] == seq), None)
    if not isinstance(seq, int) or not op:
        return jsonify({"success": False, "message": "Unknown operation"}), 400
    if not answer:
        return jsonify({"success": False, "message": "Empty answer"}), 400

    if not op["reason"]:
        operation_journal.annotate_reason(state, seq, answer)
    if question:
        state.chat_history.append({"role": "assistant", "content": f"❓ {question}", "step": seq})
    state.chat_history.append({"role": "user", "content": answer, "step": seq})
    return jsonify({"success": True, "seq": seq, "operations": operation_journal.payload(state)})


@learning_bp.route("/assess", methods=["POST"])
def assess():
    """Lightweight re-assessment after each chat answer, so the readiness
    badge + 防呆 details panel update live instead of only on Log Round. The
    request may carry the header toggle's current `use_prior_knowledge`; if
    absent it falls back to whatever mode is already stored."""
    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({"success": False, "message": "LLM is not configured yet."}), 503

    state = session_store.get_state()
    # Only meaningful once there's an operation pattern to assess against —
    # a bare chat with no filter run yet has nothing to score.
    if not state.filtered_preview:
        return jsonify({"success": True, "assessment": _assessment_payload(state), "skipped": True})

    data = request.get_json(silent=True) or {}
    if "use_prior_knowledge" in data:
        state.prior_knowledge = bool(data["use_prior_knowledge"])

    # Same conversation mode the header toggle set — so live re-scoring after
    # each answer counts NEW-vs-existing knowledge the same way the round did,
    # and stays consistent with what the interview is probing.
    context = _context_from_state(state, exclude_skill_key="", include_existing=state.prior_knowledge)
    try:
        assessment = learning_service.assess_readiness(
            llm_helper, context, use_prior_knowledge=state.prior_knowledge)
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500

    # assessment is None on a parse failure — that means "couldn't tell this
    # time", NOT "readiness is 0". Keep serving whatever was last stored
    # rather than overwrite it with zeroed defaults (see assess_readiness's
    # docstring — this is what stopped the badge from flashing to 0%).
    if assessment is not None:
        _store_assessment(state, assessment)
    return jsonify({
        "success": True,
        "assessment": _assessment_payload(state),
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


@learning_bp.route("/converge", methods=["POST"])
def converge():
    """Synthesize one or more BRAND-NEW skill drafts from the chat interview +
    operation-pattern stats. The whole back-and-forth in the chat panel IS
    the interview, so no separate Q&A step — the chat history is already
    folded into the context block that synthesize_skill_draft reads.

    It follows the conversation mode from the header prior-knowledge toggle
    (sent as `use_prior_knowledge`, falling back to the stored
    state.prior_knowledge) — the export isn't a separate mode switch:
      • FRESH — no prior-knowledge base is consulted at all. The conversation
        is split into skills purely by the distinct knowledge domains it
        covers; nothing is compared against already-saved skills.
      • PRIOR — the SAME-domain (WiFi vs BT, from state.log_domain) existing
        skills are shown ONLY so the new skills stay mutually exclusive from
        them. Still creates fresh skills — it never edits, extends, or adds
        rules onto an existing skill.

    Either way the LLM may split the conversation into several drafts when it
    genuinely covers more than one mutually-exclusive scenario, and every
    draft is filed as a NEW skill (skill_key=None) — there is no extend/diff
    routing anymore."""
    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({"success": False, "message": "LLM is not configured yet."}), 503

    state = session_store.get_state()
    if not state.filtered_preview and not state.chat_history:
        return jsonify({
            "success": False,
            "message": "Run a filter and chat about it first so there's something to converge into a skill.",
        }), 400

    data = request.get_json(silent=True) or {}
    if "use_prior_knowledge" in data:
        state.prior_knowledge = bool(data["use_prior_knowledge"])
    use_prior = state.prior_knowledge
    domain = (state.log_domain or "wifi").lower()

    # FRESH: don't even load the existing-skills list into the prompt. PRIOR:
    # include the same-domain skills so the synthesis can carve around them.
    context = _context_from_state(state, exclude_skill_key="", include_existing=use_prior)
    try:
        result = learning_service.synthesize_skill_draft(
            llm_helper, context, qa_pairs=[], use_prior_knowledge=use_prior)
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500

    drafts = result.get("drafts") or []
    for draft in drafts:
        # Every draft is a brand-new skill — the save route mints a fresh key.
        draft["skill_key"] = None
        draft["domain"] = domain
        if state.tat_path:
            draft.setdefault("tat_path", state.tat_path)

    state.skill_draft = drafts
    return jsonify({
        "success": True,
        "drafts": drafts,
        "mode": "prior" if use_prior else "fresh",
        "domain": domain,
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


@learning_bp.route("/save", methods=["POST"])
def save():
    data = request.get_json(silent=True) or {}
    state = session_store.get_state()
    domain = (data.get("domain") or state.log_domain or "wifi").lower()
    try:
        skill = skill_service.Skill(
            name=data.get("name", "New_Skill"),
            description=data.get("description", ""),
            keywords=data.get("keywords") or [],
            exclusive=data.get("exclusive") or [],
            tat_path=data.get("tat_path") or state.tat_path or None,
            expert_rules=data.get("expert_rules", ""),
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"Invalid skill data: {e}"}), 400

    skill_key = data.get("skill_key") or (state.active_skill_key if domain != "bt" else None) or None
    # Snapshot the FULL currently-loaded merged view (shared baseline + this
    # engineer's contribution + previous local edits) into the local file,
    # not just this one skill — see skill_service.save_skill's docstring.
    base_pool = app_config.bt_skills if domain == "bt" else app_config.skills
    saved_key = skill_service.save_skill(skill_key, skill, domain=domain, base=base_pool)
    loaded = skill_service.load_shared_skills()
    app_config.set_skills(loaded["wifi"])
    app_config.set_bt_skills(loaded["bt"])

    state.active_skill_key = saved_key
    state.skill_draft = []
    state.learning_questions = []
    state.learning_answers = []
    state.round_count = 0
    state.prior_knowledge = False
    state.last_readiness = {}
    state.last_coverage = {}
    state.last_gaps = []
    state.last_validation = []
    state.operations = []
    state.prev_survivors = None
    return jsonify({"success": True, "skill_key": saved_key})
