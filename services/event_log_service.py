"""
Windows System Event Log (.evt/.evtx) reader for the BT view — a faithful port
of wireless_ce_avatar/IntelAvatar's services/event_log_service.py, trimmed to
what the copycat needs.

A BT capture ships a Windows System event log next to the driver log
(``System.evtx`` in an ``Event logs/`` subfolder, or
``rawEventViewerSystemLogs.evt`` in the log's own / grandparent folder). This module locates
that file relative to the picked driver log (find_event_log_near) and decodes
it into filterable rows the UI shows in a collapsible panel above the log, with
each row's timestamp click-syncing to the nearest driver-log line.

pywin32 (``win32evtlog``) is imported lazily: the whole app must still run on a
machine without it — get_paged_events just returns an ``error`` the panel shows
instead of crashing at import time.

Note on time: events are stored UTC-naive; this port does NOT apply
IntelAvatar's full DST-aware timezone_utils resolution (IANA zone lookup,
manual-override sidecar, Taiwan-ETL-decode frame, etc. — see
wireless_ce_avatar/utils/timezone_utils.py). It DOES read the same
systeminfo.txt/system_info.txt "Time Zone" field IntelAvatar does and applies
its FIXED (UTC±HH:MM) offset (see find_capture_utc_offset_minutes) — no DST
adjustment, but enough to put the two timestamp sources in the same frame
instead of comparing raw UTC against raw customer-local with no correction at
all, which was silently landing the click-sync's "nearest" match on a
plausible-looking but wrong line whenever the capture wasn't near UTC+0.
"""
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime

# Sources always kept regardless of level (the BT/WiFi driver stack), plus the
# groups the UI's Source dropdown maps to — mirrors IntelAvatar's constants so
# a filter file stays interchangeable.
_SPECIAL_SOURCES = ['ibtusb', 'ibtpci', 'bthmini', 'bthusb', 'netwaw', 'netwtw']

_SOURCE_GROUPS = {
    'pci_bt':      ['ibtpci', 'bthmini'],
    'usb_bt':      ['ibtusb', 'bthusb'],
    'pci_bt_wifi': ['ibtpci', 'bthmini', 'netwaw', 'netwtw'],
    'usb_bt_wifi': ['ibtusb', 'bthusb', 'netwaw', 'netwtw'],
}

_LEVEL_MAP = {'1': 'Critical', '2': 'Error', '3': 'Warning', '4': 'Information', '5': 'Verbose'}

# System.evtx / rawEventViewerSystemLogs.evt — the two filenames a BT capture
# uses for its exported System event log (same set IntelAvatar looks for).
_EVT_FILENAMES = {"raweventviewersystemlogs.evt", "system.evtx"}

_CACHE = {}


class EventLogUnavailable(Exception):
    """pywin32 missing, or the file can't be read as an event log."""


def _win32evtlog():
    """Import win32evtlog only when actually parsing, so a machine without
    pywin32 can still run everything else — the caller turns this into a
    user-facing 'install pywin32' message instead of a hard crash."""
    try:
        import win32evtlog
        return win32evtlog
    except ImportError as e:
        raise EventLogUnavailable(
            "Reading Windows event logs needs pywin32 — run `pip install pywin32`."
        ) from e


def find_event_log_near(log_path: str) -> str:
    """Locate the System event log belonging to the SAME capture as `log_path`.
    Relative-path search only (the copycat has no case-download context like
    IntelAvatar's Strategy 1): the log's own dir, its grandparent, then a
    sibling ``Event logs/`` folder. Returns "" if nothing matches."""
    if not log_path or not os.path.isfile(log_path):
        return ""
    log_dir = os.path.dirname(os.path.abspath(log_path))

    search_dirs = [
        log_dir,
        os.path.dirname(os.path.dirname(log_dir)),  # grandparent
        os.path.join(log_dir, "Event logs"),
    ]
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if fname.lower() in _EVT_FILENAMES:
                candidate = os.path.join(d, fname)
                if os.path.isfile(candidate):
                    return candidate
    return ""


# Matches the fixed-offset prefix of a Windows "Time Zone:" value, e.g.
# "(UTC+08:00) Taipei" -> sign='+', hh='08', mm='00'. Same regex shape
# IntelAvatar's timezone_utils uses as its own fallback for zone strings it
# can't resolve to a named DST-aware zone — good enough here since this port
# doesn't attempt DST resolution at all.
_TZ_OFFSET_RE = re.compile(r'\(UTC([+-])(\d{2}):(\d{2})\)', re.IGNORECASE)
_SYSTEMINFO_FILENAMES = ("systeminfo.txt", "system_info.txt")
_SYSTEMINFO_SEARCH_DEPTH = 4


def find_capture_utc_offset_minutes(log_path: str):
    """Best-effort FIXED UTC offset (minutes, e.g. 480 for UTC+08:00) of the
    machine that captured `log_path`, read from a systeminfo.txt (plain text,
    UTF-16LE, "Time Zone: (UTC+08:00) Taipei") or system_info.txt (JSON,
    "System Time Zone") sitting near it — same two filenames/field names
    IntelAvatar's utils.timezone_utils reads, just without its DST-aware zone
    resolution (see this module's docstring). Walks the log's own directory
    and up to `_SYSTEMINFO_SEARCH_DEPTH` parents. Returns None if nothing is
    found or nothing parses — callers then skip the correction entirely
    rather than guess."""
    if not log_path:
        return None
    cur = os.path.dirname(os.path.abspath(log_path))
    for _ in range(_SYSTEMINFO_SEARCH_DEPTH):
        if not cur or not os.path.isdir(cur):
            break
        tz_text = ""
        text_path = os.path.join(cur, "systeminfo.txt")
        json_path = os.path.join(cur, "system_info.txt")
        if os.path.isfile(text_path):
            try:
                with open(text_path, "r", encoding="utf-16le", errors="replace") as f:
                    for line in f:
                        if line.startswith("Time Zone:"):
                            tz_text = line.split(":", 1)[1]
                            break
            except OSError:
                pass
        elif os.path.isfile(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    tz_text = json.load(f).get("System Time Zone", "") or ""
            except (OSError, ValueError):
                pass
        if tz_text:
            m = _TZ_OFFSET_RE.search(tz_text)
            if m:
                sign = 1 if m.group(1) == '+' else -1
                return sign * (int(m.group(2)) * 60 + int(m.group(3)))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _source_matches(source, source_filter):
    source_filter = (source_filter or 'all').lower()
    if source_filter == 'all':
        return True
    keywords = _SOURCE_GROUPS.get(source_filter)
    if not keywords:
        return True
    source_lower = (source or '').lower()
    return any(kw in source_lower for kw in keywords)


def _level_matches(level_text, level_filter):
    level_filter = (level_filter or 'all').lower()
    level_text = (level_text or '').lower()
    if level_filter == 'all':
        return True
    if level_filter == 'warning_error':
        return level_text in {'warning', 'error', 'critical'}
    if level_filter == 'information':
        return level_text == 'information'
    return True


def _load_raw_rows(path):
    """Decode every event via win32evtlog into plain dicts. Keeps all
    important (Critical/Error/Warning) + driver-source events, and caps plain
    Information rows at 1000 so a huge log doesn't blow up the panel — same
    policy as IntelAvatar's reader."""
    win32evtlog = _win32evtlog()
    query_handle = win32evtlog.EvtQuery(
        path,
        win32evtlog.EvtQueryFilePath | win32evtlog.EvtQueryReverseDirection,
        None,
    )
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    events_list = []
    normal_kept = 0

    while True:
        batch = win32evtlog.EvtNext(query_handle, 100)
        if not batch:
            break
        for event in batch:
            try:
                root = ET.fromstring(win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml))
                system = root.find('e:System', ns)
                if system is None:
                    continue

                level_val = getattr(system.find('e:Level', ns), 'text', '') or ''
                provider = system.find('e:Provider', ns)
                source = provider.get('Name', '') if provider is not None else ''

                is_important = level_val in ('1', '2', '3')
                is_special = any(kw in source.lower() for kw in _SPECIAL_SOURCES)
                if not (is_important or is_special):
                    if normal_kept >= 1000:
                        continue
                    normal_kept += 1

                time_created = system.find('e:TimeCreated', ns)
                time_str = (time_created.get('SystemTime', '')[:19].replace('T', ' ')
                            if time_created is not None else 'Unknown')

                event_id_elem = system.find('e:EventID', ns)
                id_str = event_id_elem.text if event_id_elem is not None else 'Unknown'

                event_data = root.find('e:EventData', ns)
                message = ''
                if event_data is not None:
                    message = ' | '.join(d.text or '' for d in event_data.findall('e:Data', ns) if d.text)

                events_list.append({
                    'time': time_str,
                    'level': _LEVEL_MAP.get(level_val, 'Unknown'),
                    'source': source,
                    'event_id': id_str,
                    'message': message[:500],
                })
            except Exception:
                continue
    return events_list


def _get_cached_raw_rows(path):
    """mtime-keyed cache of the parsed rows (parsing an .evtx is slow); keeps
    only the currently-viewed file in memory, same as IntelAvatar."""
    mtime = os.path.getmtime(path)
    if path not in _CACHE and _CACHE:
        _CACHE.clear()
    cached = _CACHE.get(path)
    if cached and cached.get('mtime') == mtime:
        return cached['events']
    events_list = _load_raw_rows(path)
    _CACHE[path] = {'mtime': mtime, 'events': events_list}
    return events_list


def peek_event_log_date(path):
    """Read just the file's OWN earliest event to get a capture date — used to
    anchor synthesized dates for time-only driver-log lines (BT's dateless HCI
    export, WiFi's DDD export — see utils.helpers.read_log_file) onto the SAME
    day the loaded System Event Log is stamped in, so the event panel's
    click-sync lands on a meaningful date instead of guessing from the driver
    log file's mtime. Cheap: reads a single event, not the whole file.
    Returns a date, or None on any failure (missing pywin32, bad path, empty
    log) — callers fall back to the driver log's own mtime."""
    if not path or not os.path.isfile(path):
        return None
    try:
        win32evtlog = _win32evtlog()
        query_handle = win32evtlog.EvtQuery(
            path, win32evtlog.EvtQueryFilePath | win32evtlog.EvtQueryForwardDirection, None,
        )
        batch = win32evtlog.EvtNext(query_handle, 1)
        if not batch:
            return None
        ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
        root = ET.fromstring(win32evtlog.EvtRender(batch[0], win32evtlog.EvtRenderEventXml))
        system = root.find('e:System', ns)
        if system is None:
            return None
        time_created = system.find('e:TimeCreated', ns)
        if time_created is None:
            return None
        system_time = time_created.get('SystemTime', '')
        return datetime.strptime(system_time[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def detect_default_source_filter(raw_events, domain=None):
    """Pick a starting Source dropdown value from what the capture actually
    contains, instead of a blind WiFi-vs-BT guess. The dropdown isn't really
    binary: it's (a) which bus the BT radio is on (PCI vs USB) and (b)
    whether to also fold in WiFi-coexistence sources (netwaw/netwtw). Both
    are answered by scanning the sources that are actually present.

    Falls back to 'all' when no BT bus source is present at all (e.g. a pure
    WiFi capture with no BT radio events) — there is nothing to narrow to."""
    pci_count = usb_count = 0
    has_wifi = False
    for row in raw_events:
        source = (row.get('source') or '').lower()
        if any(kw in source for kw in _SOURCE_GROUPS['pci_bt']):
            pci_count += 1
        if any(kw in source for kw in _SOURCE_GROUPS['usb_bt']):
            usb_count += 1
        if any(kw in source for kw in ('netwaw', 'netwtw')):
            has_wifi = True

    if not pci_count and not usb_count:
        return 'all'
    bus = 'pci_bt' if pci_count >= usb_count else 'usb_bt'
    return f'{bus}_wifi' if has_wifi else bus


def get_paged_events(path, offset=0, limit=0, source_filter='all', level_filter='all', auto_source=False):
    """A filtered page of events for the UI's virtual scroll. Never raises to
    the route: pywin32-missing / unreadable-file come back as {"error": ...}
    so the panel can show it inline.

    `auto_source=True` ignores `source_filter` and derives it from the
    capture's own data instead (see detect_default_source_filter) — used
    only for a panel's first load, so the dropdown starts on a value that
    reflects what's actually in this capture rather than always "All"."""
    if not path or not os.path.isfile(path):
        return {"error": "No event log file loaded."}
    try:
        raw_events = _get_cached_raw_rows(path)
    except EventLogUnavailable as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Couldn't read event log: {e}"}

    if auto_source:
        source_filter = detect_default_source_filter(raw_events)

    filtered = [
        row for row in raw_events
        if _source_matches(row.get('source', ''), source_filter)
        and _level_matches(row.get('level', ''), level_filter)
    ]
    total = len(filtered)
    page = filtered[offset: offset + limit] if limit > 0 else filtered[offset:]
    return {
        'events': page,
        'total': total,
        'offset': offset,
        'limit': limit,
        'has_more': offset + len(page) < total,
        'time_header': 'Time (UTC)',
        'applied_source_filter': source_filter,
    }
