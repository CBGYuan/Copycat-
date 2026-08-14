"""
Server-side per-browser-session working state (log path, filter keywords,
filtered preview, chat history, learning Q&A). Keyed by a UUID stored in the
Flask session cookie — mirrors the `_chatbot_instances` server-side store
pattern used throughout wireless_ce_avatar/IntelAvatar's blueprints.
"""
import uuid

from flask import session

from utils import question_match

_STORE: dict = {}


class WorkingState:
    def __init__(self):
        self.log_path: str = ""
        self.log_domain: str = ""          # "wifi" | "bt" — best-effort auto-detect from the picked log, see utils.helpers.detect_log_domain
        self.tat_path: str = ""
        # System Event Log (.evtx/.evt) for the BT view — auto-discovered next
        # to the picked log (services.event_log_service.find_event_log_near) or
        # chosen manually. Drives the collapsible event panel above the log,
        # whose rows click-sync to the nearest driver-log line by timestamp.
        self.event_log_path: str = ""
        # System Event XML is UTC, but the text-log wall-clock frame depends on
        # domain: WiFi is analysing-engineer local; BT HCI is customer local.
        # These fields describe the domain-specific conversion used by the
        # event<->log click-sync. They are session-only UI state and never enter
        # Avatar's flat skill YAML.
        self.event_sync_offset_min = None
        self.event_sync_basis: str = ""  # engineer_local | customer | customer_unknown
        self.customer_utc_offset_min = None
        # ISO date (YYYY-MM-DD) used to synthesize a date for dateless BT HCI
        # / WiFi DDD log lines (see utils.helpers.read_log_file's
        # `fallback_date`) — computed once in /pick_log (from the loaded
        # System Event Log's earliest event, else the log file's own mtime)
        # and reused by /apply_filter so both reads stay consistent. Empty
        # when the log's own lines already carry a date (no synthesis needed).
        self.log_date_anchor: str = ""
        # [{"text": str, "enabled": bool, "excluding": bool, "case_sensitive": bool,
        #   "regex": bool, "fore_color": str|None, "back_color": str|None}, ...]
        # excluding=True entries drop a line even if another filter matched it
        # (same semantic as skills.yaml's `exclusive` field / TAT's excluding="y").
        self.filters: list = []
        # Issue-time focus window — narrows both the raw preview and every
        # /apply_filter run to a ±focus_window_min slice around
        # focus_center_iso ("MM/DD/YYYY-HH:MM:SS.mmm") instead of the whole
        # file, so toggling a filter checkbox on a multi-million-line capture
        # doesn't re-scan the entire thing each time (see utils.tat_parser.
        # slice_by_focus_window). Empty string = no focus, work the whole file.
        self.focus_center_iso: str = ""
        self.focus_window_min: int = 5
        # The LLM's committed first read of the CURRENT default filter set,
        # formed before the engineer edited anything (see learning_service.
        # analyze_baseline): {analysis, expected_scenario,
        # expected_key_keywords, expected_noise_keywords,
        # expected_issue_time_hint, open_unknowns}. This is the null
        # hypothesis every later engineer action is diffed against, which is
        # what makes the clarification gate an OBSERVABLE divergence between
        # two interpretations rather than the LLM rating its own uncertainty.
        #
        # Scoped to the FILTER SET, not to the conversation: like
        # state.filters itself, it survives a Clear (reset_teaching_progress)
        # and is replaced when a different .tat/skill is loaded. Clearing it
        # on reset would leave a session with no baseline and no way to
        # re-establish one short of reloading the filter file.
        # baseline_filter_sig records which filter set it describes, so a
        # stale baseline can be detected instead of silently compared against
        # filters it never saw.
        self.baseline: dict = {}
        self.baseline_filter_sig: str = ""
        # len(operations) when the baseline was formed. Divergence is only
        # meaningful for edits made AFTER the read was committed: the edits
        # that built the filter set the baseline was looking at are the thing
        # it described, not a deviation from it. Without this, loading a .tat
        # and then baselining would immediately report the .tat's own
        # keywords as contradicting the read of those same keywords.
        self.baseline_op_seq: int = 0
        # Divergences already put to the engineer (see /learning/clarify), so
        # a SKIPPED question isn't asked again on the next filter run. An
        # ANSWERED one drops out on its own — the answer becomes the
        # operation's `reason`, which removes it from the unexplained set —
        # so this list exists purely for the dismissed case. Being asked and
        # declining is itself a decision; re-prompting would override it.
        self.clarified_seqs: list = []
        # Material omissions already elicited (see /learning/clarify's omission
        # branch) so a still-unexplained addition is prompted for once, not on
        # every filter run. Separate from clarified_seqs: a contradiction is a
        # genuine ambiguity (blocking), an omission is provenance capture
        # (non-blocking) -- keeping their "already asked" tracking apart keeps
        # that distinction visible in the code, not just in decision_ledger.
        self.elicited_omission_seqs: list = []
        self.focus_clarified: bool = False
        # The engineer's answer to "how did you know the issue was here?"
        # (see /learning/clarify's focus branch). Kept separately from the
        # operation journal because a focus window isn't a filter edit and so
        # has no operation to hang a `reason` on — but it is exactly the kind
        # of locating rule a reusable skill needs.
        self.focus_reason: str = ""
        # Which focus window state.prev_survivors was measured under, so
        # operation_journal.annotate_effects can tell a survivor count that is
        # comparable to the previous run from one taken over a different slice
        # of the file (see its docstring).
        self.prev_focus_sig: str = ""
        self.filtered_preview: list = []   # plain-text surviving lines, chronological order (for LLM context)
        self.filter_stats: dict = {}       # last compute_filter_stats() result (per-filter hits, overlap, colored preview)
        # Backing description of whatever the log pane is currently showing,
        # so any window of it can be rebuilt on demand for the virtual
        # scroller (see /log_viewer/preview_page). The browser only ever
        # holds the rows it can see; this is what the rest are rebuilt from.
        #   view_mode      "raw" | "filtered"
        #   view_start_idx 0-based index into the cached log lines of view row
        #                  0 -- non-zero only under a focus window (raw only)
        #   view_rows      [(line_no, (matched filter indices, ...)), ...],
        #                  filtered mode only, one entry per surviving line
        #   view_total     number of rows in the view
        self.view_mode: str = "raw"
        self.view_start_idx: int = 0
        self.view_rows: list = []
        self.view_total: int = 0
        # Operation journal: the ordered sequence of filter edits the engineer
        # made this session (add/remove/toggle/load), each annotated with its
        # marginal effect once a filter run follows it. This captures the
        # *reasoning journey* — not just the final filter — so the interview
        # can ask "why did you add/exclude X?" and record the answer. See
        # utils.operation_journal.
        self.operations: list = []
        self.prev_survivors = None         # surviving_count from the previous apply, for effect diffing
        self.chat_history: list = []       # [{"role": "user"/"assistant", "content": str}]
        # Stable user-authored description of the case. Unlike an ordinary
        # chat message this is always included in question/synthesis context,
        # even after the rolling chat window has moved past the message that
        # originally described the symptom.
        self.case_summary: str = ""
        self.learning_questions: list = []
        self.learning_answers: list = []
        # Interview policy — one axis only: "ask" interrupts for meaningful
        # new or divergent knowledge, "quiet" never interrupts and skips the
        # structured-question schema and the auto-clarify call entirely.
        # Whether an unresolved decision warns before Export is NOT part of
        # this; it is unconditional (see decision_ledger.VALID_MODES).
        self.interview_mode: str = "ask"
        # Session-only sidecar. Never serialized into Avatar's skill YAML.
        self.decision_ledger: list = []
        self.decision_next_id: int = 0
        self.skill_draft: list = []        # list of draft skill dicts from the last /learning/converge (usually 1, can be more — see synthesize_skill_draft)
        # TWO different questions, deliberately kept as two fields — one field
        # answering both is what made the Log Viewer's skill dropdown lie.
        #
        #   active_skill_key — the skill this TEACHING SESSION is built on:
        #     what the interview treats as already-known, what route_draft
        #     checks for continuity, and what the next Export inherits from.
        #     Set by log_viewer.load_skill AND by the Skill Library's
        #     "Load as baseline" (which deliberately does not touch filters).
        #
        #   filter_skill_key — which skill PRODUCED the filter set currently on
        #     screen, or "" when the filters came from a raw .tat file or were
        #     built by hand. Only the Log Viewer's own load_skill sets this,
        #     and pick_tat clears it. This is the one the dropdown renders, so
        #     it can never show a skill whose keywords aren't actually loaded.
        self.active_skill_key: str = ""
        self.filter_skill_key: str = ""
        self.round_count: int = 0          # how many analysis rounds this session
        # Conversation mode, set by which Log Round button was used and sticky
        # for the rest of the session (the interview questions, the per-answer
        # assessment, and the final export all read it): False = teach from
        # scratch (no existing skill consulted); True = teach WITH prior
        # knowledge (same-domain existing skills shown so the interview only
        # probes what's NEW beyond them and never re-asks covered knowledge).
        self.prior_knowledge: bool = False
        # Explicit read-only skill documents selected by the engineer. An
        # empty list means FRESH mode; selecting one or more turns Load skills
        # on. `active_skill_key` remains the single possible inheritance
        # parent and is deliberately separate from this document set.
        self.selected_skill_keys: list = []
        # Latest assessment (updated by BOTH analyze_round on Log Round and
        # assess_readiness on every chat answer) — drives the live readiness
        # badge + the readiness/防呆 details panel in the workbench.
        self.last_readiness: dict = {}     # {"score": 0-100, "note": str}
        self.last_coverage: dict = {}      # {"knowledge": int, "scope": int, "keywords": int}
        self.last_gaps: list = []          # ["short actionable missing piece", ...]
        self.last_validation: list = []    # [{"claim", "status": verified|asserted|contradiction, "note"}]
        # 'Still missing' items the engineer explicitly waved off as not
        # applying to this case. Permanent: it is a human judgement the
        # model can't make. Filtered out of the assessment payload, so a
        # skipped item stops nagging AND stops blocking Export.
        self.skipped_gaps: list = []
        # Items answered in their own card. Also permanent: a question the
        # engineer has already written an answer to must not come back, or
        # the strip re-asks work they consider done. What the model thinks
        # of that answer belongs in the readiness score, not in a repeat.
        self.answered_gaps: list = []
        # Engineer-confirmed teaching evidence from the Log Preview, one entry
        # per labeled source line: {"line_no", "label": evidence|counterexample,
        # "text", "matched_keywords": [{"text", "excluding"}]}. matched_keywords
        # identifies the filter(s) that matched this line (see utils.tat_parser.
        # matched_keywords_for_line) -- always unambiguous, no guessing needed,
        # whether that filter was typed by hand or came in wholesale from a
        # loaded skill/.tat file. This is
        # provenance for the skill draft, not
        # a ground-truth test corpus -- actual TP/FP/FN correctness is only
        # ever measured externally, by running the skill in wireless_ce_avatar.
        self.log_annotations: list = []
        # len(chat_history) at the moment /learning/converge last actually ran
        # synthesis — lets converge() tell "nothing new since the last
        # export" apart from "genuinely new teaching", so mashing Export
        # again on an unchanged conversation doesn't re-run the LLM and
        # re-merge the same content into a skill's expert_rules a second time
        # (see converge()'s docstring).
        self.last_export_chat_len: int = 0
        # len(operations) as of the last successful /learning/log_round call —
        # lets the frontend show a "new changes to log" nudge card exactly
        # when operations has grown past this since, instead of guessing
        # client-side or nagging on every single filter edit.
        self.last_round_op_count: int = 0
        # Preserve committed reads when Update baseline is used.
        self.baseline_history: list = []
        self.baseline_version: int = 0

    def baseline_signature(self) -> str:
        """Identity of the evidence set described by the comparison baseline.

        Individual filter edits are deliberately excluded because additions,
        removals, and toggles are the later actions compared against the
        baseline. The source log, loaded .tat/skill identity, and prior-
        knowledge mode are included: changing any of those starts a genuinely
        different comparison basis.
        """
        return (
            f"{self.log_path}\x01{self.tat_path}\x01{self.filter_skill_key}"
            f"\x01prior={int(bool(self.prior_knowledge))}"
            f"\x01docs={','.join(sorted(str(k) for k in self.selected_skill_keys))}"
            f"\x01summary={self.case_summary.strip()}"
        )

    def close_gaps_covered_by(self, *texts) -> list:
        """Close the "still missing" items a piece of teaching just answered.

        The gap list is re-derived from the whole conversation, but only by
        the next assessment — until then an item the engineer plainly just
        explained (from a Step's teach box, a question card, or a plain chat
        reply) sat there still asking. Where the answer was typed is not
        supposed to matter, so this is called from every answer path rather
        than from the one that happens to own the strip.

        Counts as answered, not skipped: the engineer did give the knowledge,
        just not through this item's own box. Returns what it closed.
        """
        covering = [str(t).strip() for t in texts if str(t or "").strip()]
        if not covering:
            return []
        closed = []
        for gap in list(self.last_gaps or []):
            if gap in self.answered_gaps or gap in self.skipped_gaps:
                continue
            if any(question_match.answer_covers(gap, t) for t in covering):
                self.answered_gaps.append(gap)
                closed.append(gap)
        return closed

    def restamp_baseline(self) -> None:
        """Keep an existing baseline valid across bookkeeping that moves the
        signature without changing the evidence it describes — Export adding
        the skill it just saved to the loaded docs, or picking an Export
        parent in the Skill Library. Same log, same filter, same first read;
        only the record of which skill docs are loaded moved. Without this the
        next chat message is refused by a gate the UI still shows as met.
        """
        if self.baseline and self.baseline_filter_sig:
            self.baseline_filter_sig = self.baseline_signature()

    def has_current_baseline(self) -> bool:
        return bool(
            self.baseline
            and self.baseline_filter_sig
            and self.baseline_filter_sig == self.baseline_signature()
        )

    def reset_teaching_progress(self) -> None:
        """Clears the readiness/round/operation-journal state that should
        persist across a skill Export+Save (an engineer often does several
        rounds of exporting from the SAME ongoing log session) but SHOULD
        reset when they explicitly start over — clicking Clear
        (chatbot.reset) or loading a different log (log_viewer.pick_log).
        Deliberately narrow: does not touch filters/tat_path/log_path — those
        have their own, separate reset points."""
        self.operations = []
        self.prev_survivors = None
        self.round_count = 0
        self.last_readiness = {}
        self.last_coverage = {}
        self.last_gaps = []
        self.last_validation = []
        self.skipped_gaps = []
        self.answered_gaps = []
        self.skill_draft = []
        self.last_export_chat_len = 0
        self.last_round_op_count = 0
        self.decision_ledger = []
        self.decision_next_id = 0
        # Line labels belong to the prior teaching case and must not leak
        # into the next one.
        self.log_annotations = []
        self.case_summary = ""
        # Which divergences have already been put to the engineer belongs to
        # the CONVERSATION, not to the filter set (unlike self.baseline, which
        # deliberately survives) — starting the teaching over should let the
        # same question be asked again rather than silently suppressing it.
        self.clarified_seqs = []
        self.elicited_omission_seqs = []
        self.focus_clarified = False
        self.focus_reason = ""


def get_state() -> WorkingState:
    sid = session.get("wsid")
    if not sid or sid not in _STORE:
        sid = str(uuid.uuid4())
        session["wsid"] = sid
        _STORE[sid] = WorkingState()
    return _STORE[sid]

