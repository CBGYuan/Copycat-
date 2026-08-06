from flask import Blueprint, request, jsonify

from configs import set_up_app
from configs.global_configs import app_config
from services import session_store, learning_service, skill_service, decision_ledger
from utils import operation_journal, divergence, skill_dedup

# No standalone page: the "Teach This Scenario" flow lives inline inside the
# combined Log Viewer workbench (templates/log_viewer.html) — this blueprint
# only exposes the /start, /submit_answers, /save APIs that page calls.
learning_bp = Blueprint("learning", __name__, url_prefix="/learning")


def _skill_pool(domain: str) -> dict:
    """The right skill dict for a domain ("wifi"/"bt") — the single place
    every route below goes through so WiFi and BT skills never get compared
    against or saved into each other's pool. This is the FULL merged view
    (shared corp baseline + this engineer's contribution + local) — used only
    for READ-ONLY context (the interview's "don't re-ask what's already
    covered" prior-knowledge display and loaded-parent relationship check).
    It is never an in-place merge target during Export."""
    return app_config.bt_skills if domain == "bt" else app_config.skills


def _selected_skill_keys(state, domain: str) -> list:
    """Validated, same-domain document selection for Load skills.

    The explicit list is authoritative. The two fallbacks only preserve
    compatibility with an already-running pre-picker session or an older API
    caller that set `prior_knowledge=True`: prefer its loaded parent, else the
    historical all-skills behaviour.
    """
    pool = _skill_pool(domain)
    chosen = [key for key in state.selected_skill_keys if key in pool]
    if chosen or not state.prior_knowledge:
        return chosen
    if state.active_skill_key in pool:
        return [state.active_skill_key]
    return list(pool)


def _other_skill_descriptions(exclude_key: str = "", domain: str = "wifi",
                              selected_keys=None) -> list:
    """Read-only documents for the OTHER already-saved skills IN THE
    SAME DOMAIN — fed to the LLM so it can (a) keep a new skill's description
    mutually exclusive from them, the way Avatar's SKILL_DESCRIPTIONS map
    keeps each skill's scope distinct, and (b) spot literal keyword overlap
    to decide whether this scenario should extend one of them instead of
    duplicating it. Scoped to `domain` so a WiFi log is never compared
    against (or routed to extend) a Bluetooth skill, or vice versa."""
    out = []
    selected = set(selected_keys) if selected_keys is not None else None
    for key, sk in _skill_pool(domain).items():
        if key == exclude_key:
            continue
        if selected is not None and key not in selected:
            continue
        out.append({
            "key": key,
            "name": sk.name,
            "description": sk.description,
            "triggers": list(sk.triggers or []),
            "keywords": list(sk.keywords or []),
            "exclusive": list(sk.exclusive or []),
            "expert_rules": sk.expert_rules or "",
            "version": sk.version,
            "parent": sk.parent,
            "lineage": list(sk.lineage or []),
        })
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


def _record_questions(state, questions, *, source: str, step="all", source_prefix=""):
    """Attach stable decision IDs without changing the question-card shape."""
    out = []
    for index, question in enumerate(questions or []):
        item = decision_ledger.record_question(
            state,
            source=source,
            question=question.get("question"),
            qtype=question.get("type"),
            options=question.get("options"),
            recommended_answer=question.get("recommended_answer"),
            recommendation_reason=question.get("recommendation_reason"),
            step=step,
            source_key=f"{source_prefix}:{index}" if source_prefix else "",
        )
        enriched = dict(question)
        if item:
            enriched["decision_id"] = item["id"]
        out.append(enriched)
    return out


def _baseline_delta(before: dict, after: dict) -> dict:
    """Small deterministic change-set between two committed baseline reads."""
    def texts(value):
        return {
            str(item.get("text") if isinstance(item, dict) else item).strip()
            for item in (value or [])
            if str(item.get("text") if isinstance(item, dict) else item).strip()
        }

    before_keys = texts(before.get("expected_key_keywords"))
    after_keys = texts(after.get("expected_key_keywords"))
    before_noise = texts(before.get("expected_noise_keywords"))
    after_noise = texts(after.get("expected_noise_keywords"))
    before_unknowns = {str(value).strip() for value in before.get("open_unknowns") or [] if str(value).strip()}
    after_unknowns = {str(value).strip() for value in after.get("open_unknowns") or [] if str(value).strip()}
    return {
        "scenario_changed": (
            str(before.get("expected_scenario") or "").strip()
            != str(after.get("expected_scenario") or "").strip()
        ),
        "key_added": sorted(after_keys - before_keys),
        "key_removed": sorted(before_keys - after_keys),
        "noise_added": sorted(after_noise - before_noise),
        "noise_removed": sorted(before_noise - after_noise),
        "unknowns_resolved": sorted(before_unknowns - after_unknowns),
        "unknowns_added": sorted(after_unknowns - before_unknowns),
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


def _context_from_state(state, exclude_skill_key: str = "", include_existing: bool = True,
                        baseline_skill=None) -> dict:
    """`baseline_skill` is passed ONLY by the export path, and only when the
    loaded skill will genuinely become this draft's parent — it makes the
    prompt state that the new skill inherits that skill's keywords, which is
    false anywhere else (see learning_service's BASELINE SKILL block)."""
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
                {"text": f["text"], "excluding": f["excluding"], "hits": f["hits"],
                 "unique_hits": f.get("unique_hits"), "dropped": f.get("dropped")}
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
        "existing_skills": _other_skill_descriptions(
            exclude_skill_key, domain, _selected_skill_keys(state, domain)
        ) if include_existing else [],
        "baseline_skill": {
            "key": state.active_skill_key,
            "name": baseline_skill.name,
            "description": baseline_skill.description,
            "triggers": list(baseline_skill.triggers or []),
            "keywords": list(baseline_skill.keywords or []),
            "exclusive": list(baseline_skill.exclusive or []),
            "expert_rules": baseline_skill.expert_rules or "",
            "version": baseline_skill.version,
            "parent": baseline_skill.parent,
            "lineage": list(baseline_skill.lineage or []),
        } if baseline_skill else None,
        "sample_lines": _sample_lines(state.filtered_preview, limit=40),
        "case_summary": state.case_summary,
        "log_annotations": state.log_annotations,
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
    teaches from scratch, PRIOR shows the selected same-domain skill docs so the
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
    # PRIOR mode shows the selected same-domain skill docs to the interview so it
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
    result["questions"] = _record_questions(
        state,
        result.get("questions"),
        source="round",
        source_prefix=f"round:{state.round_count}",
    )

    # Marks these operations as "accounted for" so the frontend's nudge card
    # (see checkRoundNudge in log_viewer.html) stops showing until the
    # engineer changes the filter again.
    state.last_round_op_count = len(state.operations)

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
        "ambiguity": result.get("ambiguity", {}),
        "assessment": _assessment_payload(state),
        "prior_knowledge": state.prior_knowledge,
        "decision_ledger": decision_ledger.payload(state),
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


def _filter_signature(state) -> str:
    """Identity of the filter SET a baseline was formed on.

    Deliberately built from the source log + loaded .tat/skill identity, never
    from individual filter contents or enabled state: filter edits are the
    engineer action the baseline exists to be compared against, so folding
    enabled-state in here would mark the baseline stale on every toggle and
    re-baseline against the engineer's own edit — erasing the comparison and
    costing an LLM call per click. Loading a different log/.tat/skill changes
    the evidence source, which is the case that should invalidate it.

    Keys off filter_skill_key, NOT active_skill_key: this is the identity of
    what is on screen. Choosing a different export baseline from the Skill
    Library changes active_skill_key without touching a single filter, and
    folding that in here would mark the baseline stale and burn an LLM call
    re-reading a filter set that did not change.
    """
    return state.baseline_signature()


def _format_baseline_message(result: dict) -> str:
    """Turn the structured first read into a scan-friendly chat card.

    Keeping the same markdown in chat_history means the live result and a
    later page reload render identically through renderMarkdownLite().
    """
    parts = [
        "# Baseline analysis",
        "Current chat knowledge, filter steps, labeled E/X observations, and sampled surviving log lines were analyzed together.",
    ]
    version = result.get("version")
    if version:
        parts.extend(["## Baseline version", f"v{version}"])
    basis = str(result.get("comparison_basis") or "").strip()
    if basis:
        parts.extend(["## Comparison basis", basis])
    scenario = str(result.get("expected_scenario") or "").strip()
    if scenario:
        parts.extend(["## Expected scenario", scenario])
    analysis = str(result.get("analysis") or "").strip()
    if analysis:
        parts.extend(["## Key interpretation", analysis])

    key_rows = result.get("expected_key_keywords") or []
    if key_rows:
        parts.append("## Key signals")
        parts.extend(
            f"- **{row.get('text', '')}** — {row.get('why') or 'Likely load-bearing'}"
            for row in key_rows
        )

    noise_rows = result.get("expected_noise_keywords") or []
    if noise_rows:
        parts.append("## Possible noise")
        parts.extend(
            f"- **{row.get('text', '')}** — {row.get('why') or 'Likely low-signal'}"
            for row in noise_rows
        )

    issue_hint = str(result.get("expected_issue_time_hint") or "").strip()
    if issue_hint:
        parts.extend(["## Issue-time clue", issue_hint])

    unknowns = result.get("open_unknowns") or []
    if unknowns:
        parts.append("## Open questions")
        parts.extend(f"- {item}" for item in unknowns)
    delta = result.get("delta_from_previous") or {}
    changes = []
    for key, label in (
        ("key_added", "Key signal added"),
        ("key_removed", "Key signal removed"),
        ("noise_added", "Noise added"),
        ("noise_removed", "Noise removed"),
        ("unknowns_resolved", "Unknown resolved"),
        ("unknowns_added", "Unknown added"),
    ):
        if delta.get(key):
            changes.append(f"- **{label}:** {', '.join(delta[key])}")
    if delta.get("scenario_changed"):
        changes.insert(0, "- **Expected scenario changed**")
    if changes:
        parts.append("## Changes from previous baseline")
        parts.extend(changes)
    return "\n\n".join(parts)


@learning_bp.route("/baseline", methods=["POST"])
def baseline():
    """Form the LLM's committed first read of a just-loaded default filter set,
    BEFORE the engineer edits anything (see learning_service.analyze_baseline).

    This is the starting point of the new flow: the read commits to which
    keywords it thinks are load-bearing, which look like noise, and what it
    cannot tell — so later engineer actions can be diffed against it to find
    genuine, observable disagreement instead of asking the model to rate its
    own uncertainty. It also produces the session's FIRST assessment, taking
    over the role round 1 used to play, so readiness/gaps are populated even
    in a session where the engineer never needs to change the filter at all.

    Re-callable cheaply: a repeat call for the same filter set returns the
    stored baseline without spending another LLM call, so a page reload or a
    double-fire can't quietly cost tokens. Pass force=true to re-read anyway.
    """
    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({"success": False, "message": "LLM is not configured yet."}), 503

    state = session_store.get_state()
    if not state.filters:
        return jsonify({"success": False, "message": "Load a .tat file or a skill first."}), 400
    if not state.filtered_preview:
        return jsonify({"success": False, "message": "Run the filter first so there's something to read."}), 400

    data = request.get_json(silent=True) or {}
    if "case_summary" in data:
        state.case_summary = str(data.get("case_summary") or "").strip()[:4000]
    if "selected_skill_keys" in data and isinstance(data.get("selected_skill_keys"), list):
        domain = (state.log_domain or "wifi").lower()
        pool = _skill_pool(domain)
        state.selected_skill_keys = list(dict.fromkeys(
            str(key) for key in data["selected_skill_keys"] if str(key) in pool
        ))
        state.prior_knowledge = bool(state.selected_skill_keys)
    signature = _filter_signature(state)
    if state.baseline and state.baseline_filter_sig == signature and not data.get("force"):
        return jsonify({
            "success": True, "cached": True,
            "baseline": state.baseline,
            "assessment": _assessment_payload(state),
        })
    previous_baseline = (
        dict(state.baseline)
        if state.baseline and state.baseline_filter_sig == signature
        else {}
    )

    # The checked document set is the explicit boundary for prerequisite knowledge.
    # OFF: commit to the LLM's own evidence-based first read. ON: include
    # loaded/existing same-domain skills so the baseline records what was
    # already known before the engineer teaches anything new.
    context = _context_from_state(
        state,
        exclude_skill_key="",
        include_existing=state.prior_knowledge,
    )
    try:
        result = learning_service.analyze_baseline(
            llm_helper,
            context,
            use_prior_knowledge=state.prior_knowledge,
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500

    result["comparison_basis"] = (
        "LLM + loaded/existing skills" if state.prior_knowledge else "LLM first read only"
    )
    if previous_baseline:
        state.baseline_history.append({
            "version": state.baseline_version or 1,
            "signature": signature,
            "baseline": previous_baseline,
        })
        state.baseline_history = state.baseline_history[-10:]
        result["delta_from_previous"] = _baseline_delta(previous_baseline, result)
        state.baseline_version = max(1, state.baseline_version) + 1
    else:
        # A different evidence source starts its own comparison history.
        state.baseline_history = []
        state.baseline_version = 1
    result["version"] = state.baseline_version
    state.baseline = {k: v for k, v in result.items() if k != "assessment"}
    state.baseline_filter_sig = signature
    # Everything logged so far BUILT the filter set this read just described,
    # so it can't have deviated from it — divergence starts counting here.
    state.baseline_op_seq = len(state.operations)
    # Same guard as log_round: assessment=None means the JSON didn't parse,
    # and silently zeroing a previously-good readiness would be misleading.
    if result.get("assessment") is not None:
        _store_assessment(state, result["assessment"])

    baseline_message = _format_baseline_message(result)
    state.chat_history.append({
        "role": "assistant",
        "content": baseline_message,
        "step": "all",
    })

    return jsonify({
        "success": True, "cached": False,
        "baseline": state.baseline,
        "message": baseline_message,
        "assessment": _assessment_payload(state),
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


@learning_bp.route("/clarify", methods=["POST"])
def clarify():
    """Turn ONE divergence into ONE question.

    The decision of WHETHER to interrupt was already made, deterministically
    and without a model, by utils.divergence.detect (measured materiality
    first, baseline stance only to classify). This route just picks the single
    highest-value target among what that found and spends one LLM call on
    phrasing it. If nothing qualifies it returns question=None and costs
    nothing — the common case on an ordinary filter edit.

    CONTRADICTIONS, material OMISSIONS, and the focus window are all eligible
    — but not in the same voice, and not with the same stakes:

      - CONTRADICTION — two competing readings, so this gets a DISCRIMINATING
        question (ambiguity gate) and BLOCKS Export until resolved (a genuine
        specification decision — see decision_ledger's `blocking`).
      - OMISSION — the baseline never had a stance, so there is nothing to
        discriminate between; asking "which is right" here would be
        low-information-gain prompting. Instead this gets an open PROVENANCE
        question ("why does this matter, does it generalize?") and is
        explicitly NON-BLOCKING — capturing this knowledge is valuable, but
        Export shouldn't nag over something that was never ambiguous. This is
        the single highest-value elicitation target for distilling the
        engineer's tacit knowledge into the skill: previously an omission
        only surfaced as the Steps panel's passive 🎓 hint, so it was skipped
        far more often than it was explained.

    Both are answered through /learning/answer_step_question, whose existing
    behaviour closes the loop exactly right — recording the answer as the
    operation's `reason` both captures the knowledge for export and removes
    the edit from the unexplained set, so the same divergence cannot come
    back.
    """
    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({"success": False, "message": "LLM is not configured yet."}), 503

    state = session_store.get_state()
    report = divergence.detect(state)
    domain = (state.log_domain or "wifi").lower()

    # Contradictions first: they carry two concrete competing readings, which
    # is strictly more information than an omission or the focus question.
    # Most recent first — it's the edit the engineer still has in their head.
    target = None
    pending = [c for c in report["contradictions"] if c["seq"] not in state.clarified_seqs]
    if pending:
        c = sorted(pending, key=lambda x: x["seq"])[-1]
        target = {
            "kind": "contradiction", "domain": domain, "text": c["text"],
            "action_phrase": operation_journal._ACTION_VERB.get(c["action"], c["action"]),
            "effect_phrase": c["effect_phrase"],
            "baseline_stance": c["baseline_stance"], "baseline_why": c["baseline_why"],
            "seq": c["seq"],
        }

    # Then material omissions — genuinely new knowledge the baseline never
    # had, so it outranks the focus question (which the baseline DID have a
    # locating guess for).
    if not target:
        pending_omissions = [o for o in report["omissions"] if o["seq"] not in state.elicited_omission_seqs]
        if pending_omissions:
            o = sorted(pending_omissions, key=lambda x: x["seq"])[-1]
            target = {
                "kind": "omission", "domain": domain, "text": o["text"],
                "action_phrase": operation_journal._ACTION_VERB.get(o["action"], o["action"]),
                "effect_phrase": o["effect_phrase"],
                "seq": o["seq"],
            }

    if not target and report["focus"] and not state.focus_clarified:
        target = {"kind": "focus", "domain": domain, **report["focus"]}

    if not target:
        return jsonify({"success": True, "question": None})

    try:
        result = learning_service.clarify_divergence(llm_helper, target, state.prior_knowledge)
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500
    if not result:
        # No usable question came back. Deliberately NOT falling back to a
        # generic one — an undiscriminating question is the exact thing the
        # gate exists to avoid — and deliberately not marking the target as
        # asked, so a transient parse failure doesn't burn it permanently.
        return jsonify({"success": True, "question": None})

    decision = decision_ledger.record_question(
        state,
        source=target["kind"],
        question=result["question"]["question"],
        qtype=result["question"].get("type"),
        options=result["question"].get("options"),
        recommended_answer=result["question"].get("recommended_answer"),
        recommendation_reason=result["question"].get("recommendation_reason"),
        step=target.get("seq", "all"),
        source_key=(
            f"contradiction:{target.get('seq')}" if target["kind"] == "contradiction"
            else f"omission:{target.get('seq')}" if target["kind"] == "omission"
            else f"focus:{state.focus_center_iso}:{state.focus_window_min}"
        ),
        # An omission is provenance capture, not a specification ambiguity —
        # see the docstring above — so it must not block Export the way a
        # genuine contradiction does.
        blocking=(target["kind"] != "omission"),
    )

    seq = target.get("seq")
    if target["kind"] == "focus":
        state.focus_clarified = True
    elif target["kind"] == "omission":
        state.elicited_omission_seqs.append(seq)
    else:
        state.clarified_seqs.append(seq)

    state.chat_history.append({
        "role": "assistant",
        "content": f"❓ {result['question']['question']}",
        "step": seq if seq is not None else "all",
    })
    return jsonify({
        "success": True,
        "kind": target["kind"],
        "seq": seq,
        "keyword": target.get("text", ""),
        "question": result["question"],
        "captures": result["captures"],
        "decision_id": decision["id"] if decision else "",
        "decision_ledger": decision_ledger.payload(state),
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


@learning_bp.route("/answer_focus_clarify", methods=["POST"])
def answer_focus_clarify():
    """Answer to the focus-window locating question. No LLM call. Stored on
    state.focus_reason rather than an operation's `reason` because a focus
    window is not a filter edit and has no operation to attach to (see
    WorkingState.focus_reason)."""
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    answer = (data.get("answer") or "").strip()
    if not answer:
        return jsonify({"success": False, "message": "Empty answer"}), 400
    state = session_store.get_state()
    state.focus_reason = answer
    decision_ledger.resolve(state, data.get("decision_id"), answer)
    if question:
        state.chat_history.append({"role": "assistant", "content": f"❓ {question}", "step": "all"})
    state.chat_history.append({"role": "user", "content": answer, "step": "all"})
    return jsonify({"success": True, "decision_ledger": decision_ledger.payload(state)})


@learning_bp.route("/set_mode", methods=["POST"])
def set_mode():
    """Persist the header prior-knowledge toggle so the auto-fired background
    assess (and a page reload) reflect the current mode even before the next
    Log Round. No LLM call — just stores the flag."""
    data = request.get_json(silent=True) or {}
    state = session_store.get_state()
    domain = (state.log_domain or "wifi").lower()
    if "selected_skill_keys" in data:
        requested = data.get("selected_skill_keys") or []
        if not isinstance(requested, list):
            return jsonify({"success": False, "message": "selected_skill_keys must be a list"}), 400
        pool = _skill_pool(domain)
        # Preserve UI order, drop duplicates, and never allow WiFi documents
        # into a BT case (or vice versa).
        state.selected_skill_keys = list(dict.fromkeys(
            str(key) for key in requested if str(key) in pool
        ))
        state.prior_knowledge = bool(state.selected_skill_keys)
    if "case_summary" in data:
        # Long enough for a useful customer symptom/timeline summary, bounded
        # so this compact field cannot accidentally become a second log dump.
        state.case_summary = str(data.get("case_summary") or "").strip()[:4000]
    if "use_prior_knowledge" in data:
        requested_prior = bool(data.get("use_prior_knowledge"))
        state.prior_knowledge = requested_prior and bool(
            _selected_skill_keys(state, domain)
        )
    if "interview_mode" in data:
        # No retroactive re-flagging of open items here any more: `blocking`
        # is now a property of the question itself (see decision_ledger.
        # record_question), so switching modes can no longer silently
        # downgrade a real decision already put to the engineer.
        state.interview_mode = decision_ledger.normalize_mode(data.get("interview_mode"))
    return jsonify({
        "success": True,
        "prior_knowledge": state.prior_knowledge,
        "case_summary": state.case_summary,
        "selected_skill_keys": _selected_skill_keys(state, domain),
        "interview_mode": state.interview_mode,
        "decision_ledger": decision_ledger.payload(state),
    })


@learning_bp.route("/decision/defer", methods=["POST"])
def defer_decision():
    """Record an explicit Skip: the decision stays visible in the ledger and
    in the Export-time spec review, but stops being asked and stops warning
    before Export — a deliberate "not now", distinct from an unanswered one."""
    data = request.get_json(silent=True) or {}
    state = session_store.get_state()
    item = decision_ledger.defer(state, data.get("decision_id"))
    if not item:
        return jsonify({"success": False, "message": "Unknown decision"}), 404
    return jsonify({"success": True, "decision_ledger": decision_ledger.payload(state)})


def _step_annotations(state, op: dict) -> list:
    """E/X lines attributable to exactly one filter-edit Step.

    E and X are mutually exclusive on a single source line (annotate_line
    replaces the line's previous label). Different lines may legitimately
    fall on opposite sides of the same keyword; retaining both is what lets
    the interview elicit the boundary instead of erasing a counterexample.
    """
    if op.get("action") in {"load_skill", "load_tat"}:
        return []
    target = str(op.get("text") or "").strip().casefold()
    excluding = bool(op.get("excluding"))
    if not target:
        return []
    return [
        dict(item)
        for item in state.log_annotations
        if any(
            isinstance(keyword, dict)
            and bool(keyword.get("excluding")) == excluding
            and str(keyword.get("text") or "").strip().casefold() == target
            for keyword in (item.get("matched_keywords") or [])
        )
    ]


def _step_question_history(state, seq: int) -> list:
    return [
        {"question": item.get("question", ""), "answer": item.get("answer", "")}
        for item in state.decision_ledger
        if item.get("step") == seq
        and item.get("source") == "step"
        and item.get("status") == "resolved"
        and item.get("answer")
    ]


def _step_op_context(state, op: dict, *, continuing: bool = False) -> dict:
    domain = (state.log_domain or "wifi").lower()
    return {
        "domain": domain,
        "verb": operation_journal._ACTION_VERB.get(op["action"], op["action"]),
        "target": op.get("label") or op["text"],
        "excluding": op["excluding"],
        "effect_phrase": operation_journal._effect_phrase(op),
        "reason": op.get("reason") or "",
        "existing_skills": _other_skill_descriptions(
            "", domain, _selected_skill_keys(state, domain)
        ) if state.prior_knowledge else [],
        "case_summary": state.case_summary,
        "sample_lines": _sample_lines(state.filtered_preview, limit=24),
        "step_annotations": _step_annotations(state, op),
        "answered_questions": _step_question_history(state, op["seq"]),
        "continuing": continuing,
    }


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

    op_context = _step_op_context(state, op)
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
    follow_up_decision = None
    if follow_up:
        follow_up_decision = decision_ledger.record_question(
            state,
            source="step_follow_up",
            question=follow_up,
            step=seq,
            source_key=f"step-follow-up:{seq}:{follow_up}",
            blocking=False,
        )

    return jsonify({
        "success": True,
        "seq": seq,
        "knowledge_core": knowledge_core,
        "expert_note": expert_note,
        "follow_up_question": follow_up,
        "follow_up_decision_id": follow_up_decision["id"] if follow_up_decision else "",
        "decision_ledger": decision_ledger.payload(state),
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

    answered = _step_question_history(state, seq)
    if len(answered) >= 4:
        return jsonify({
            "success": True, "seq": seq, "question": None,
            "no_question": True,
            "message": "This step already has enough answered refinement questions.",
            "decision_ledger": decision_ledger.payload(state),
        })
    op_context = _step_op_context(state, op, continuing=bool(answered))
    try:
        question = learning_service.ask_step_question(llm_helper, op_context, state.prior_knowledge)
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500
    if not question:
        return jsonify({
            "success": True, "seq": seq, "question": None,
            "no_question": True,
            "message": "No remaining question would materially change this skill.",
            "decision_ledger": decision_ledger.payload(state),
            "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
        })

    decision = decision_ledger.record_question(
        state,
        source="step",
        question=question["question"],
        qtype=question.get("type"),
        options=question.get("options"),
        recommended_answer=question.get("recommended_answer"),
        recommendation_reason=question.get("recommendation_reason"),
        step=seq,
        source_key=f"step:{seq}:{question['question']}",
    )
    state.chat_history.append({"role": "assistant", "content": f"❓ {question['question']}", "step": seq})
    return jsonify({
        "success": True,
        "seq": seq,
        "question": question,
        "question_number": len(answered) + 1,
        "decision_id": decision["id"] if decision else "",
        "decision_ledger": decision_ledger.payload(state),
        "usage": {"last": llm_helper.last_usage, "session": llm_helper.session_usage},
    })


@learning_bp.route("/answer_step_question", methods=["POST"])
def answer_step_question():
    """Record one Step answer, then optionally generate the next high-value
    question. Only one card is ever returned. The chain stops when the model
    finds no remaining skill-changing gap, after four answered questions as a
    runaway guard, or immediately when the UI uses Skip (which calls defer
    instead of this route)."""
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

    decision_ledger.resolve(state, data.get("decision_id"), answer)
    if not op["reason"]:
        operation_journal.annotate_reason(state, seq, answer)
    state.chat_history.append({"role": "user", "content": answer, "step": seq})

    next_question = None
    next_decision = None
    answered = _step_question_history(state, seq)
    llm_helper = app_config.llm_helper
    if (data.get("continue_chain") is True and len(answered) < 4
            and llm_helper and llm_helper.is_ready):
        try:
            next_question = learning_service.ask_step_question(
                llm_helper,
                _step_op_context(state, op, continuing=True),
                state.prior_knowledge,
            )
        except Exception:
            # The submitted answer is already safely recorded. A failed
            # optional continuation must never turn that successful action
            # into an error or make the user submit twice.
            next_question = None
        if next_question:
            next_decision = decision_ledger.record_question(
                state,
                source="step",
                question=next_question["question"],
                qtype=next_question.get("type"),
                options=next_question.get("options"),
                recommended_answer=next_question.get("recommended_answer"),
                recommendation_reason=next_question.get("recommendation_reason"),
                step=seq,
                source_key=f"step:{seq}:{next_question['question']}",
            )
            state.chat_history.append({
                "role": "assistant",
                "content": f"❓ {next_question['question']}",
                "step": seq,
            })
    return jsonify({
        "success": True,
        "seq": seq,
        "operations": operation_journal.payload(state),
        "decision_ledger": decision_ledger.payload(state),
        "next_question": next_question,
        "next_decision_id": next_decision["id"] if next_decision else "",
        "question_number": len(answered) + 1 if next_question else len(answered),
        "chain_complete": not bool(next_question),
        "usage": {
            "last": llm_helper.last_usage if llm_helper and next_question else None,
            "session": llm_helper.session_usage if llm_helper else None,
        },
    })


@learning_bp.route("/assess", methods=["POST"])
def assess():
    """Lightweight re-assessment after each chat answer, so the readiness
    badge + 防呆 details panel update live instead of only on Log Round. The
    request may carry the picker-derived `use_prior_knowledge`; if
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

    # Same conversation mode the document picker set — so live re-scoring after
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
    """Synthesize one or more mutually-exclusive skill drafts from the chat,
    chronological log evidence, and operation-pattern stats. The whole chat
    panel is the interview; no separate Q&A export step is needed.

    It follows the conversation mode from the header prior-knowledge toggle
    (sent as `use_prior_knowledge`, falling back to the stored
    state.prior_knowledge):
      • FRESH — no existing skill is consulted during synthesis OR routing.
        Every split draft remains standalone and contains only this session's
        log/operation/user teaching.
      • PRIOR — same-domain skills are read-only documents. Each split draft
        is independently classified against the explicitly loaded skill:
        additive same-capability knowledge becomes a NEW flattened child;
        unrelated domains remain standalone; fully covered knowledge is
        flagged instead of creating a duplicate child.
    Either mode may split the conversation into several drafts.

    Export never updates an existing skill in place. Shared/team/local skills
    all remain read-only during synthesis: the explicitly loaded one can be
    ancestry for a new self-contained child, while every other split stays a
    new standalone entry. Each relation is shown before Save.

    The route's decision (`judge`) is attached to each draft for the frontend
    to display — it never writes anything by itself; the engineer still
    confirms/edits in the Edit-Skill modal before Save persists it."""
    llm_helper = app_config.llm_helper
    if not llm_helper or not llm_helper.is_ready:
        return jsonify({"success": False, "message": "LLM is not configured yet."}), 503

    state = session_store.get_state()
    if not state.has_current_baseline():
        return jsonify({
            "success": False,
            "baseline_required": True,
            "message": "Set the comparison baseline before exporting a skill.",
        }), 409
    if not state.filtered_preview and not state.chat_history:
        return jsonify({
            "success": False,
            "message": "Run a filter and chat about it first so there's something to converge into a skill.",
        }), 400

    # Nothing new since the last successful Export on this exact conversation
    # — mashing the button again would re-synthesize the SAME chat_history,
    # and a merge target's expert_rules would pick up a re-phrased-but-not-
    # substantively-new restatement each time (basic_merge_draft's dedup is
    # line-based, not semantic, so near-duplicate paraphrases slip through).
    # Block it here instead, before any LLM call — cheap, and it's the
    # engineer's OWN mistake to fix (keep teaching, then Export again),
    # rather than something worth spending a token on.
    if state.chat_history and len(state.chat_history) <= state.last_export_chat_len:
        return jsonify({
            "success": False,
            "no_new_content": True,
            "message": "Nothing new to export — you haven't added any teaching since the last "
                       "Export on this conversation. Keep chatting or teach another step, then "
                       "Export again.",
        })

    data = request.get_json(silent=True) or {}
    if "use_prior_knowledge" in data:
        state.prior_knowledge = bool(data["use_prior_knowledge"])
    use_prior = state.prior_knowledge
    domain = (state.log_domain or "wifi").lower()

    # Load skills is authoritative for BOTH halves of prior knowledge:
    #   OFF -> no old skill enters synthesis, routing, or inheritance;
    #   ON  -> existing skills are read-only documents, and the explicitly
    #          loaded skill may become a parent for each RELATED draft.
    # This used to resolve a parent regardless of use_prior, which meant a
    # nominally FRESH export could still inherit old rules after synthesis.
    parent = _baseline_skill(state) if use_prior else None
    depth = skill_dedup.lineage_depth_check(parent) if parent else None
    lineage_blocked = None
    if parent and not depth["allowed"]:
        # Already as deep as the chain can usefully go. Inheriting again would
        # produce a keyword list matching most of the log, which is where
        # Avatar's per-skill evidence payload gets head+tail truncated and the
        # skill stops focusing anything (see skill_dedup.MAX_LINEAGE_DEPTH).
        # Export still happens — the taught knowledge is not what's at fault —
        # it just lands as a fresh root instead.
        lineage_blocked = {
            "parent_key": state.active_skill_key,
            "parent_name": parent.name,
            "child_depth": depth["child_depth"],
            "max_depth": depth["max_depth"],
            "parent_lineage": list(parent.lineage),
        }
        parent = None

    # FRESH: no existing skill enters the prompt. PRIOR: existing same-domain
    # skills are read-only documents so synthesis can avoid duplicate scope;
    # the loaded skill is a candidate parent, not a promise that every split
    # draft will inherit it.
    context = _context_from_state(state, exclude_skill_key="", include_existing=use_prior,
                                  baseline_skill=parent if use_prior else None)
    try:
        result = learning_service.synthesize_skill_draft(
            llm_helper, context, qa_pairs=[], use_prior_knowledge=use_prior)
    except Exception as e:
        return jsonify({"success": False, "message": f"LLM call failed: {e}"}), 500

    raw_drafts = result.get("drafts") or []
    domain_pool = _skill_pool(domain)

    drafts = []
    for draft in raw_drafts:
        draft["domain"] = domain
        if state.tat_path:
            draft.setdefault("tat_path", state.tat_path)
        draft["teaching_evidence"] = learning_service.assess_teaching_evidence(draft, context)
        relationship = None
        if parent:
            relationship = learning_service.classify_loaded_parent(
                llm_helper, draft, state.active_skill_key, parent, domain_pool,
                filter_stats=state.filter_stats)
            draft["parent_relationship"] = relationship

        if not use_prior:
            # Strict standalone path: no retrieval, no merge suggestion, and
            # no parent metadata. The only knowledge source is this session.
            routed = dict(draft)
            routed["skill_key"] = None
            routed["judge"] = {
                "action": "add", "target_skill_key": None,
                "target_skill_name": None, "target_skill_version": None,
                "reason": "Load skills is off; built only from this session.",
                "source": "fresh",
            }
        elif relationship and relationship["relationship"] == "extends":
            # A related loaded skill becomes ancestry for a NEW child. Do not
            # route this into an in-place local merge: parent stays immutable.
            routed = dict(draft)
            routed["skill_key"] = None
            routed["judge"] = {
                "action": "add", "target_skill_key": None,
                "target_skill_name": None, "target_skill_version": None,
                "reason": relationship["reason"], "source": "loaded_parent",
            }
        elif relationship and relationship["relationship"] == "covered":
            # Fully covered knowledge is still reviewable, but must not create
            # a content-identical child merely to display a lineage arrow.
            routed = dict(draft)
            routed["skill_key"] = None
            routed["judge"] = {
                "action": "discard", "target_skill_key": None,
                "target_skill_name": parent.name,
                "target_skill_version": parent.version,
                "reason": relationship["reason"], "source": "loaded_parent",
            }
        else:
            # PRIOR knowledge shapes questions, scope and deduplication, but
            # Export still creates a NEW entry. Only a verified relationship
            # to the explicitly loaded parent creates ancestry; otherwise the
            # split draft is standalone rather than silently updating some
            # other local skill found by retrieval.
            routed = dict(draft)
            routed["skill_key"] = None
            routed["judge"] = {
                "action": "add", "target_skill_key": None,
                "target_skill_name": None, "target_skill_version": None,
                "reason": (
                    relationship["reason"] if relationship else
                    "Prior skill documents were considered; no loaded parent was selected."
                ),
                "source": "prior",
            }
        routed["domain"] = domain
        drafts.append(routed)

    # In PRIOR mode the related drafts inherit the loaded parent's complete
    # body; unrelated domains remain standalone. This per-draft split matters
    # when one conversation yields, for example, one roam skill that extends
    # the loaded baseline and one unrelated DHCP skill.
    #
    # Related draft -> rebuild on the parent's complete framework and record
    # lineage. Unrelated/covered draft -> preserve its standalone body. The
    # related child is fully resolved, never delta-only: Avatar ignores the
    # lineage keys, so a delta-only child would run with incomplete filters.
    if parent:
        rebuilt = []
        for d in drafts:
            relation = d.get("parent_relationship") or {}
            if relation.get("relationship") != "extends":
                rebuilt.append(d)
                continue
            diff = skill_dedup.diff_against_parent(d, parent)
            ext = skill_dedup.build_extension_skill(
                d, parent, state.active_skill_key, diff)
            # Inheritance always creates a NEW Copycat-owned child. A local
            # parent must never be overwritten merely because routing found it.
            ext["skill_key"] = None
            # What the modal needs to SHOW the separation rather than just
            # presenting a longer list: an unexplained merge reads as either
            # data loss or duplication depending on which way it went.
            ext["lineage_info"] = {
                "parent_key": state.active_skill_key,
                "parent_name": parent.name,
                "lineage": ext["lineage"],
                "inherited": ext["inherited_counts"],
                "added": ext["added_counts"],
                "removed_as_duplicate": diff["summary"]["auto_removable"],
                "needs_review": diff["summary"]["needs_review"],
                "duplicate_keywords": [i["text"] for i in
                                       diff["keywords"]["exact"] + diff["keywords"]["covered"]],
                "review_keywords": [i["text"] for i in diff["keywords"]["near"]],
                # The exact split the modal colours by. Counts alone can't do
                # it: the engineer has to see WHICH chip came from the parent
                # and which one they just taught, per item, not "19 vs 1".
                "inherited_keywords": list(parent.keywords),
                "inherited_exclusive": list(parent.exclusive),
                # The child shares every one of the parent's keywords, so its
                # description is the ONLY thing Avatar's agent can tell them
                # apart by (see skill_dedup.description_conflict). The prompt
                # is told to keep them exclusive; this catches it when it
                # didn't, while the engineer still has the field in front of
                # them and can just rewrite it.
                "description_conflict": skill_dedup.description_conflict(
                    d.get("description", ""), parent, d.get("triggers")),
                "parent_triggers": list(getattr(parent, "triggers", []) or []),
            }
            rebuilt.append(ext)
        drafts = rebuilt

    inherited_count = sum(1 for d in drafts if d.get("parent"))

    # Review-only control plane. These fields never enter skill_service.Skill
    # and therefore never alter Avatar's YAML contract.
    for draft in drafts:
        draft["spec_review"] = decision_ledger.build_skill_spec(draft, state)

    state.skill_draft = drafts
    # Stamp the watermark AFTER a successful synthesis — the guard above
    # compares against this on the NEXT converge() call.
    state.last_export_chat_len = len(state.chat_history)
    return jsonify({
        "success": True,
        "drafts": drafts,
        "inherited_from": state.active_skill_key if inherited_count else None,
        "inheritance_summary": {
            "enabled": use_prior,
            "parent_key": state.active_skill_key if parent else None,
            "parent_name": parent.name if parent else None,
            "inherited_drafts": inherited_count,
            "standalone_drafts": len(drafts) - inherited_count,
        },
        # Set only when a skill WAS loaded but the chain was already at max
        # depth, so the UI can say why this export came out standalone
        # instead of silently looking like nothing was loaded.
        "lineage_blocked": lineage_blocked,
        "mode": "prior" if use_prior else "fresh",
        "domain": domain,
        "decision_ledger": decision_ledger.payload(state),
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
            # Carried through from the Edit-Skill modal. This is the route the
            # EXPORT path actually saves through, so omitting these here meant
            # an inherited draft lost its whole ancestry on the way to disk
            # even though /skills/save handled it correctly.
            parent=data.get("parent") or None,
            lineage=[str(a) for a in (data.get("lineage") or []) if str(a).strip()],
            triggers=[str(t) for t in (data.get("triggers") or []) if str(t).strip()],
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"Invalid skill data: {e}"}), 400

    # skill_key comes ONLY from what the frontend actually sends — the Edit-
    # Skill modal pre-fills it from route_draft()'s judged decision when the
    # engineer is looking at a suggested merge (see converge()), and it's
    # null for a fresh "New Skill" draft. This used to also fall back to
    # state.active_skill_key (whatever skill happened to be loaded as this
    # session's filter baseline) whenever the payload's own skill_key was
    # empty — which silently overwrote that loaded skill on EVERY "New
    # Skill" export with zero similarity check, the exact opposite of what
    # the UI told the engineer they were saving. That fallback is gone:
    # merging onto an existing skill now only ever happens through a real
    # judged decision the engineer can see and reject in the modal first.
    skill_key = data.get("skill_key") or None
    # Snapshot the FULL currently-loaded merged view (shared baseline + this
    # engineer's contribution + previous local edits) into the local file,
    # not just this one skill — see skill_service.save_skill's docstring.
    base_pool = app_config.bt_skills if domain == "bt" else app_config.skills
    try:
        saved_key = skill_service.save_skill(skill_key, skill, domain=domain, base=base_pool)
    except skill_service.SkillStoreError as e:
        # The local skills file could not be read, so nothing was written and
        # the draft is still in session state. Report it as a failed save so
        # the modal stays open with the engineer's work intact, rather than a
        # 500 they would read as "it probably went through".
        return jsonify({"success": False, "message": str(e)}), 409
    set_up_app.reload_pools()

    state.active_skill_key = saved_key
    if saved_key not in state.selected_skill_keys:
        state.selected_skill_keys.append(saved_key)
    state.prior_knowledge = True
    state.skill_draft = []
    state.learning_questions = []
    state.learning_answers = []
    # round_count / prior_knowledge / last_readiness / last_coverage /
    # last_gaps / last_validation / operations / prev_survivors are
    # deliberately NOT reset here anymore — an engineer often exports
    # several rounds from the SAME ongoing log session, and wiping the
    # readiness state on every single Save made it look like teaching
    # progress kept vanishing. Only an explicit "start over" (Clear /
    # loading a different log — see WorkingState.reset_teaching_progress)
    # clears that state now.
    return jsonify({"success": True, "skill_key": saved_key})
