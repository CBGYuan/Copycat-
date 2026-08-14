import importlib.util
import os
import random
import re
import socket
from datetime import date, datetime


def load_module(file_path: str, module_name: str = "dyn_module"):
    """Import an arbitrary .py file by path (used to load key.py / skill draft
    modules the same way wireless_ce_avatar/IntelAvatar's utils.helpers does)."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- Cross-format driver-log line normalization ---------------------------
# Three raw line shapes reach read_log_file():
#   1. WiFi WPP dump — already canonical: "MM/DD/YYYY-HH:MM:SS.mmm ...".
#      Left untouched (fast path: this is the common case, zero behavior
#      change for it).
#   2. BT decoded .hci.txt — "[core]pid.tid::MM/DD/YYYY-HH:MM:SS.mmm ..."
#      (the WDK trace-decode prefix wraps the SAME dated timestamp format,
#      just not at column 0 — confirmed against two real multi-hundred-
#      thousand-line ibtpci/ibtusb .hci.txt captures, 100% line match).
#      Rewritten by stripping the prefix so it becomes shape (1).
#   3. Time-only lines with no date at all — BT's alternate/older HCI export
#      "<HH:MM:SS.mmm>", or WiFi's DDD-player export "<TIME:HH:MM:SS>" /
#      "TIME:HH:MM:SS" (mirrors wireless_ce_avatar/IntelAvatar's own
#      log_chatbot_service._normalize_time_message regexes for these two
#      formats). A date must be SYNTHESIZED (see `fallback_date` below) to
#      reformat these into shape (1).
# Every downstream consumer — tat_parser's stats/time_span, the LLM context
# builder, and the log-viewer's column rendering + event-log click-sync —
# reads ONLY the canonical leading-timestamp shape. Normalizing here, once,
# at ingestion is what lets ALL of that machinery stay completely unchanged
# for BT/DDD logs instead of needing a parallel code path (and column layout)
# everywhere else — the row alignment fixed earlier keeps working as-is.
_CANONICAL_TS_RE = re.compile(r'^\s*\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3}')
_HCI_DATED_RE = re.compile(
    r'^\[[^\]]*\][^:\s]+::(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})\s*(.*)$')
_HCI_TIME_ONLY_RE = re.compile(r'<(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?>')
_DDD_TIME_RE = re.compile(r'(?:<)?TIME:(\d{2}:\d{2}:\d{2})(?:>)?', re.IGNORECASE)
# Another WiFi DDD-export shape (seen from a real capture): a tab-separated
# leading frame/sequence counter, then a colon-separated HH:MM:SS:mmm (not
# angle-bracketed, not period-separated ms, no "TIME:" prefix) — e.g.
# "0000345912\t17:07:24:599\t[OSC ]...". None of the three patterns above
# match this shape, which is why it was passing through _canonicalize_log_line
# completely unrecognized (no date synthesis at all, not even a wrong one).
_DDD_FRAME_TIME_RE = re.compile(r'^\d+\t(\d{2}):(\d{2}):(\d{2}):(\d{3})\t(.*)$')


def _canonicalize_log_line(line: str, fallback_date: date) -> str:
    """Reformat one raw line into the canonical leading `MM/DD/YYYY-HH:MM:SS.mmm`
    shape when it's a recognized BT .hci.txt / WiFi DDD variant; returns
    `line` unchanged otherwise (already-canonical WiFi WPP lines, and any
    unrecognized line)."""
    if _CANONICAL_TS_RE.match(line):
        return line
    nl = "\n" if line.endswith("\n") else ""
    body = line[:-1] if nl else line

    m = _HCI_DATED_RE.match(body)
    if m:
        return f"{m.group(1)} {m.group(2)}{nl}"

    m = _HCI_TIME_ONLY_RE.search(body)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ms = int((m.group(4) or "0").ljust(3, "0")[:3])
        rest = (body[:m.start()] + body[m.end():]).strip()
        ts = (f"{fallback_date.month:02d}/{fallback_date.day:02d}/{fallback_date.year:04d}-"
              f"{h:02d}:{mn:02d}:{s:02d}.{ms:03d}")
        return f"{ts} {rest}{nl}"

    m = _DDD_TIME_RE.search(body)
    if m:
        h, mn, s = (int(x) for x in m.group(1).split(":"))
        rest = (body[:m.start()] + body[m.end():]).strip()
        ts = (f"{fallback_date.month:02d}/{fallback_date.day:02d}/{fallback_date.year:04d}-"
              f"{h:02d}:{mn:02d}:{s:02d}.000")
        return f"{ts} {rest}{nl}"

    m = _DDD_FRAME_TIME_RE.match(body)
    if m:
        h, mn, s, ms, rest = m.groups()
        ts = (f"{fallback_date.month:02d}/{fallback_date.day:02d}/{fallback_date.year:04d}-"
              f"{h}:{mn}:{s}.{ms}")
        return f"{ts} {rest}{nl}"

    return line


def needs_date_synthesis(path: str, sniff_lines: int = 50) -> bool:
    """True if this log's own lines carry no date at all (BT's dateless HCI
    export, or WiFi's DDD export) — meaning read_log_file needs an explicit
    `fallback_date` for its canonicalization to land on a meaningful day
    (rather than silently defaulting to file mtime). Sniffs a few lines only;
    a line already carrying its own date (canonical WPP, or the dated
    .hci.txt prefix) does NOT count, even if it appears before a dateless one."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= sniff_lines:
                    break
                body = line.rstrip("\n")
                if _CANONICAL_TS_RE.match(body) or _HCI_DATED_RE.match(body):
                    continue
                if _HCI_TIME_ONLY_RE.search(body) or _DDD_TIME_RE.search(body) \
                        or _DDD_FRAME_TIME_RE.match(body):
                    return True
    except OSError:
        pass
    return False


# WiFi DDD export filenames encode the capture start TIME and DATE directly,
# e.g. "WiFiLog--17-07-24-599--17-06-2026--00001.LOG": the first --..-- group
# (17-07-24-599) is HH-MM-SS-mmm and matches the log's own first line exactly;
# the second (17-06-2026) is DD-MM-YYYY — 17 can't be a month, so the day
# comes first. Only the date group is needed here (the file's own lines
# supply the time-of-day once canonicalized).
_FILENAME_DATE_RE = re.compile(r'--(\d{2})-(\d{2})-(\d{4})--')


def extract_date_from_filename(path: str) -> date | None:
    """The capture date straight from a WiFi DDD export's filename (see
    _FILENAME_DATE_RE) — preferred over file_mtime_date, which only reflects
    when the file was last copied/downloaded and can silently be wrong (e.g.
    a capture downloaded days after it was recorded). Returns None for any
    filename that doesn't match this convention, so callers fall back to mtime."""
    m = _FILENAME_DATE_RE.search(os.path.basename(path))
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def file_mtime_date(path: str) -> date:
    """Best-effort capture-date fallback when no System Event Log is loaded
    to anchor a dateless BT HCI / WiFi DDD log against (see
    read_log_file's `fallback_date`)."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).date()
    except OSError:
        return datetime.today().date()


def read_log_file(path: str, encoding: str = "utf-8", fallback_date: date = None):
    """Read every line, normalizing BT .hci.txt / WiFi DDD-decoded lines into
    the same canonical leading-timestamp shape the rest of the app expects
    (see _canonicalize_log_line above). `fallback_date` supplies the date for
    lines that carry only a time-of-day — callers that have a loaded System
    Event Log should pass its earliest event's date (see
    event_log_service.peek_event_log_date) so the synthesized timestamps land
    on the SAME day the event log is stamped in, keeping the two panes'
    click-sync meaningful; falls back to the log file's own mtime date
    otherwise (see file_mtime_date)."""
    if fallback_date is None:
        fallback_date = file_mtime_date(path)
    with open(path, "r", encoding=encoding, errors="replace") as f:
        return [_canonicalize_log_line(line, fallback_date) for line in f]


# WiFi vs Bluetooth marker keywords for detect_log_domain() — chosen from
# lines actually seen in real logs of each type (WiFi: driver trace tags
# like [CNCT_FLOW]/[ALON], WDI_* OIDs; BT: [CMD DEC]/[EVT DEC] HCI command/
# event trace tags, L2CAP).
_WIFI_MARKERS = ("CNCT_FLOW", "WDI_", "legacy_alon_nic_identify", "TASK_Connect", "[ALON")
_BT_MARKERS = ("[CMD DEC]", "[EVT DEC]", "HCI_", "L2CAP", "BTHCI")


def detect_log_domain(path: str, sniff_lines: int = 300) -> str:
    """Best-effort WiFi vs Bluetooth classification of a log file, used to
    pick which shared skill set (skills_config/skills.yaml vs
    bt_skills.yaml) to default the UI to. Filename hints are checked first
    (fast, usually enough); if inconclusive, count a few domain-marker
    keywords across the first `sniff_lines` lines. Defaults to 'wifi' when
    still inconclusive — this app is WiFi-first, and that's the safer guess
    given most of its skill set assumes a WiFi driver log."""
    name = os.path.basename(path).lower()
    if any(h in name for h in ("bluetooth", "_bt_", "bt_log", ".hci.", "hci_log")):
        return "bt"
    if any(h in name for h in ("wifi", "wlan", "wireless")):
        return "wifi"

    wifi_hits = 0
    bt_hits = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= sniff_lines:
                    break
                if any(m in line for m in _WIFI_MARKERS):
                    wifi_hits += 1
                if any(m in line for m in _BT_MARKERS):
                    bt_hits += 1
    except OSError:
        pass

    return "bt" if bt_hits > wifi_hits else "wifi"


def get_available_port(start: int = 54000, end: int = 60000, max_tries: int = 20) -> int:
    """Pick a random free TCP port in [start, end] — same approach as
    wireless_ce_avatar/IntelAvatar's DriverManager.run_driver(), which calls
    this before every launch instead of binding a fixed port.

    This is what makes each launch land on an "independent page/port": a
    hardcoded port (this app used to just use 5000) collides with whatever
    stray/leftover process from a previous run might still be holding it —
    which is exactly the confusion we kept hitting this session (multiple
    processes fighting over :5000, no way to tell which one a browser tab was
    actually talking to). Testing an actual bind (not just "is something
    listening") also catches ports blocked by OS/firewall policy, not just
    ports already in use.
    """
    for _ in range(max_tries):
        port = random.randint(start, end)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No available port found in [{start}, {end}] after {max_tries} tries")
