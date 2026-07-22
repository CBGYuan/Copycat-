"""
Server-side per-browser-session working state (log path, filter keywords,
filtered preview, chat history, learning Q&A). Keyed by a UUID stored in the
Flask session cookie — mirrors the `_chatbot_instances` server-side store
pattern used throughout wireless_ce_avatar/IntelAvatar's blueprints.
"""
import uuid

from flask import session

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
        self.filtered_preview: list = []   # plain-text surviving lines, chronological order (for LLM context)
        self.filter_stats: dict = {}       # last compute_filter_stats() result (per-filter hits, overlap, colored preview)
        # Operation journal: the ordered sequence of filter edits the engineer
        # made this session (add/remove/toggle/load), each annotated with its
        # marginal effect once a filter run follows it. This captures the
        # *reasoning journey* — not just the final filter — so the interview
        # can ask "why did you add/exclude X?" and record the answer. See
        # utils.operation_journal.
        self.operations: list = []
        self.prev_survivors = None         # surviving_count from the previous apply, for effect diffing
        self.chat_history: list = []       # [{"role": "user"/"assistant", "content": str}]
        self.learning_questions: list = []
        self.learning_answers: list = []
        self.skill_draft: list = []        # list of draft skill dicts from the last /learning/converge (usually 1, can be more — see synthesize_skill_draft)
        self.active_skill_key: str = ""    # skill currently loaded/being taught, if any
        self.round_count: int = 0          # how many "Log Round & Analyze" clicks this session
        # Conversation mode, set by which Log Round button was used and sticky
        # for the rest of the session (the interview questions, the per-answer
        # assessment, and the final export all read it): False = teach from
        # scratch (no existing skill consulted); True = teach WITH prior
        # knowledge (same-domain existing skills shown so the interview only
        # probes what's NEW beyond them and never re-asks covered knowledge).
        self.prior_knowledge: bool = False
        # Latest assessment (updated by BOTH analyze_round on Log Round and
        # assess_readiness on every chat answer) — drives the live readiness
        # badge + the readiness/防呆 details panel in the workbench.
        self.last_readiness: dict = {}     # {"score": 0-100, "note": str}
        self.last_coverage: dict = {}      # {"knowledge": int, "scope": int, "keywords": int}
        self.last_gaps: list = []          # ["short actionable missing piece", ...]
        self.last_validation: list = []    # [{"claim", "status": verified|asserted|contradiction, "note"}]


def get_state() -> WorkingState:
    sid = session.get("wsid")
    if not sid or sid not in _STORE:
        sid = str(uuid.uuid4())
        session["wsid"] = sid
        _STORE[sid] = WorkingState()
    return _STORE[sid]


def reset_state() -> WorkingState:
    sid = session.get("wsid")
    if sid and sid in _STORE:
        del _STORE[sid]
    return get_state()
