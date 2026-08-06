"""
.tat filter file parsing + log filtering/statistics.

Parses TextAnalysisTool.NET's real XML schema (confirmed against an actual
exported .tat file):

  <TextAnalysisTool.NET version="..." showOnlyFilteredLines="True">
    <filters>
      <filter enabled="y" excluding="n" foreColor="ffff00" backColor="ff0000"
              type="matches_text" case_sensitive="n" regex="n" text="Assert" />
      ...
    </filters>
  </TextAnalysisTool.NET>

`excluding="y"` filters are NOT keywords to match on — they're noise terms
that drop a line even if some other filter matched it (this is the same
semantic as the `exclusive` field in skills.yaml). An earlier version of this
parser only looked at `enabled`/`text` and silently treated every excluding
filter as an including one, which is wrong: the sample connectivity.tat has
17 `excluding="y"` rules (SCAN_REQUEST, TASK_SCAN, PROP_SET_, ...) that must
subtract lines, not add them.
"""
import bisect
import re
from collections import deque
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple


def _to_bool(val: Optional[str]) -> bool:
    return (val or "").strip().lower() == "y"


_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _color(hex6: Optional[str]) -> Optional[str]:
    """Normalize a TAT color attribute ("ff0000") to CSS ("#ff0000").

    Anything that is not a plain 6-digit hex is dropped: the frontend renders
    this straight into a style="..." attribute, so an arbitrary value from a
    .tat file would otherwise be an HTML-injection vector.
    """
    if not hex6:
        return None
    hex6 = hex6.strip()
    if not _HEX_COLOR_RE.match(hex6):
        return None
    return hex6 if hex6.startswith("#") else f"#{hex6}"


def parse_filter_file(filter_file_path: str) -> List[Dict]:
    """Parse a .tat file into a list of filter dicts:
    {text, enabled, excluding, case_sensitive, regex, fore_color, back_color}
    in file order (this order is preserved everywhere else too, so the UI's
    filter list matches what TextAnalysisTool.NET itself shows)."""
    tree = ET.parse(filter_file_path)
    root = tree.getroot()
    filters_el = root.find("filters")
    if filters_el is None:
        return []
    rules = []
    for f in filters_el.findall("filter"):
        text = f.get("text", "")
        if not text:
            continue
        rules.append({
            "text": text,
            "enabled": _to_bool(f.get("enabled")),
            "excluding": _to_bool(f.get("excluding")),
            "case_sensitive": _to_bool(f.get("case_sensitive")),
            "regex": _to_bool(f.get("regex")),
            "fore_color": _color(f.get("foreColor")),
            "back_color": _color(f.get("backColor")),
        })
    return rules


# Kept for the two remaining call sites (skill-based filters have no colors/
# excluding metadata of their own — they're synthesized as plain including
# keywords from skill.keywords / skill.exclusive).
def extract_all_keywords_from_filter_file(filter_file_path: str) -> List[Dict]:
    return parse_filter_file(filter_file_path)


def _line_matcher(rule: Dict):
    """Return a fast predicate(line_lower, line) -> bool for one rule."""
    if rule["regex"]:
        flags = 0 if rule["case_sensitive"] else re.IGNORECASE
        try:
            pattern = re.compile(rule["text"], flags)
        except re.error:
            # Malformed regex in the .tat file — treat as a non-match rather
            # than crashing the whole filter run.
            return lambda line_lower, line: False
        return lambda line_lower, line: pattern.search(line) is not None
    needle = rule["text"] if rule["case_sensitive"] else rule["text"].lower()
    if rule["case_sensitive"]:
        return lambda line_lower, line: needle in line
    return lambda line_lower, line: needle in line_lower


# Leading timestamp, e.g. "10/28/2025-09:54:36.214 " or "04/14/2026-01:20:13.333 "
_TS_RE = re.compile(r'^\s*(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')

# WiFi DDD exports keep a raw frame counter in front of the timestamp; it is
# noise in the log pane and in the LLM context.
_FRAME_COUNTER_RE = re.compile(r'^\d+\t')


def clean_row_text(line: str) -> str:
    """One raw log line as the log pane / LLM context should see it."""
    return _FRAME_COUNTER_RE.sub("", line).rstrip("\n")


def _leading_timestamp(line: str):
    m = _TS_RE.match(line)
    return m.group(1) if m else None


# ---- Issue-time focus window --------------------------------------------
# Every /apply_filter call re-scans the WHOLE loaded file, which is fine for
# a log with a few thousand lines but a multi-second stall per checkbox
# toggle on a real multi-million-line capture. Narrowing to a ±N-minute
# window around a typed "issue time" (see log_viewer_routes._cached_log_lines
# / /set_focus) is the fix: everything downstream — compute_filter_stats,
# the raw preview — then only ever has to look at that slice.
_TS_DT_FMT = "%m/%d/%Y-%H:%M:%S.%f"


def build_timestamp_index(log_lines: List[str]) -> List[Tuple[int, datetime]]:
    """[(line_idx, datetime), ...] for every line with a parseable leading
    canonical timestamp — line_idx is 0-based into `log_lines`. Used by
    slice_by_focus_window to binary-search a time window's boundaries
    instead of scanning every line. O(n) to build, same as a single filter
    pass — callers should cache the result alongside the line list itself
    (see log_viewer_routes._cached_timestamp_index) rather than rebuild it
    on every focus/filter change."""
    out = []
    for i, line in enumerate(log_lines):
        ts = _leading_timestamp(line)
        if not ts:
            continue
        try:
            out.append((i, datetime.strptime(ts, _TS_DT_FMT)))
        except ValueError:
            continue
    return out


def slice_by_focus_window(log_lines: List[str], timestamp_index: List[Tuple[int, datetime]],
                           focus_dt: datetime, window_minutes: int = 5) -> Tuple[int, List[str]]:
    """Narrow `log_lines` to the [focus_dt - window, focus_dt + window]
    range using `timestamp_index` (see build_timestamp_index) to binary-
    search the boundary rather than scan every line. Assumes log_lines are
    chronologically ordered, true for a driver-log capture. Returns
    (start_line_idx, sliced_lines) — the 0-based index of the slice's first
    line within the ORIGINAL log_lines, needed by the caller to pass the
    correct `start_line_no` on to compute_filter_stats so real file line
    numbers survive the window (see its docstring). Falls back to the full
    list (start_line_idx=0) when nothing in the file has a parseable
    timestamp at all — can't window without one."""
    if not timestamp_index:
        return 0, log_lines
    dts = [dt for _, dt in timestamp_index]
    lo = bisect.bisect_left(dts, focus_dt - timedelta(minutes=window_minutes))
    hi = bisect.bisect_right(dts, focus_dt + timedelta(minutes=window_minutes)) - 1
    if lo > hi:
        return 0, []
    start_idx = timestamp_index[lo][0]
    end_idx = timestamp_index[hi][0]
    return start_idx, log_lines[start_idx:end_idx + 1]


def compute_filter_stats(log_lines: List[str], filters: List[Dict],
                          preview_limit: int = 500, start_line_no: int = 1) -> Dict:
    """Apply every filter to `log_lines` in one pass and return:

      - per_filter: raw hit count for EVERY filter, enabled or not, in file
        order — mirrors TextAnalysisTool.NET's own filter-list "Hits" column,
        which always shows a pattern's match count in the full log regardless
        of whether its checkbox is ticked, so an engineer can gauge a
        candidate keyword's impact before enabling it. Only *enabled* filters
        additionally get unique_hits/dropped (marginal contribution against
        the currently active include/exclude set) since that concept only
        makes sense for filters that actually participated in this run.
      - overlap_count: number of lines matched by 2+ *including* filters
        (the "intersection" stat) — useful signal for which keyword
        combinations are actually correlated vs. redundant.
      - co_occurrence: the top including-filter *pairs* that fire on the same
        line — this is the "operation pattern" signal (which log events the
        engineer's filter set correlates), used to ground the skill-building
        interview without ever shipping the raw log to the LLM.
      - time_span: first/last timestamp among surviving lines.
      - surviving_count / preview: lines that matched >=1 including filter
        AND no excluding filter, in original chronological order, each tagged
        with its original 1-based file line number + which filter(s) matched
        (for color highlighting), capped at `preview_limit`.

    `start_line_no` — the real 1-based file line number of `log_lines[0]`.
    Defaults to 1 (the normal whole-file case); callers passing a WINDOWED
    slice (see slice_by_focus_window) must pass the slice's true starting
    line number here, otherwise every line_no in the returned preview would
    be renumbered from 1 within the window instead of matching the file's
    real line numbers — silently breaking annotate_line and jump-to-line,
    which both key off the real number.
    """
    enabled = [(i, r) for i, r in enumerate(filters) if r["enabled"]]
    including = [(i, r) for i, r in enabled if not r["excluding"]]
    excluding = [(i, r) for i, r in enabled if r["excluding"]]

    # Matchers/hit_counts cover EVERY filter, not just enabled ones, so a
    # disabled pattern still gets a real raw hit count in per_filter below
    # instead of staying blank in the UI until the engineer checks it.
    #
    # Two things used to make a single checkbox toggle stall for seconds on a
    # real capture, both addressed here:
    #   1. Every rule was tested up to THREE times per line — once for
    #      hit_counts, again in the include_hits comprehension, again for
    #      exclude_hits. Each line now resolves its matched set ONCE and the
    #      three consumers read that set.
    #   2. Each of those tests went through a per-rule closure (_line_matcher),
    #      so a 20-rule .tat over a 113k-line log paid ~6.8M Python-level calls
    #      per re-run. Plain substring rules — nearly all of a .tat file — are
    #      now tested inline in tight per-bucket loops with no call at all.
    # _line_matcher remains the single readable statement of what "matches"
    # means for one rule; these buckets MUST stay semantically identical to it.
    plain_ci: List[tuple] = []   # (index, lowered needle) tested against line_lower
    plain_cs: List[tuple] = []   # (index, needle)         tested against line
    regexes: List[tuple] = []    # (index, compiled)       tested against line
    for i, r in enumerate(filters):
        if r["regex"]:
            try:
                regexes.append((i, re.compile(
                    r["text"], 0 if r["case_sensitive"] else re.IGNORECASE)))
            except re.error:
                # Malformed regex in the .tat file — same contract as
                # _line_matcher: never matches, rather than crashing the run.
                pass
        elif r["case_sensitive"]:
            plain_cs.append((i, r["text"]))
        else:
            plain_ci.append((i, r["text"].lower()))

    # Deliberately NOT prefiltered by a union regex of every pattern. That
    # looks like it should pay off (most lines match nothing, so one C-level
    # call could replace N Python-level tests) but measured 2.9x SLOWER on a
    # 113k-line capture: `needle in line` is a fast C substring search, while
    # an alternation of ~17 literals makes Python's backtracking `re` engine
    # try each branch at every position. Individual `in` tests win; don't
    # "optimize" this back into an alternation.
    hit_counts = {i: 0 for i in range(len(filters))}
    # unique_hits[i] = surviving lines where include-filter i was the ONLY
    # including filter that matched — i.e. its *marginal* contribution to the
    # result. A high hit count but zero unique hits means the keyword is
    # redundant (everything it catches, some other keyword already caught);
    # that's the signal that lets the interview / synthesis drop it from the
    # minimal key set instead of guessing. Only counted on surviving lines
    # (an excluded line isn't "contributed" by anyone).
    unique_hits = {i: 0 for i, _ in including}
    # excluded_survivors[i] = lines that matched an include but were dropped
    # BECAUSE excluding-filter i matched — i.e. how much noise that exclude
    # actually removed from the result. This quantifies a "why did you exclude
    # X" edit's payoff without re-scanning the log.
    excluded_by = {i: 0 for i, _ in excluding}
    pair_counts: Dict[tuple, int] = {}
    overlap_count = 0
    surviving_preview = []
    # (line_no, matched include-filter indices) for EVERY survivor, not just
    # the previewed ones. Two ints and a small tuple per row -- cheap next to
    # the line strings themselves, which the caller already has cached -- and
    # it is what lets the log pane page through the whole result instead of
    # only ever seeing `preview_limit` rows (see /log_viewer/preview_page).
    survivor_rows: List[tuple] = []
    # Separate LLM context tail. The visible preview intentionally remains a
    # chronological prefix for the log table, but questions also need to see
    # how a long scenario ends. A bounded deque captures that without keeping
    # the entire surviving result in memory.
    context_tail = deque(maxlen=min(300, max(2, preview_limit // 3)))
    surviving_count = 0
    first_ts = None
    last_ts = None

    including_idx = [i for i, _ in including]
    excluding_idx = [i for i, _ in excluding]

    for line_no, line in enumerate(log_lines, start=start_line_no):
        line_lower = line.lower()
        # Raw match count for every pattern, enabled or not — independent of
        # the include/exclude interplay below. Resolved once per line; the
        # include/exclude splits below are set lookups, not re-tests.
        matched = set()
        for i, needle in plain_ci:
            if needle in line_lower:
                matched.add(i)
                hit_counts[i] += 1
        for i, needle in plain_cs:
            if needle in line:
                matched.add(i)
                hit_counts[i] += 1
        for i, pattern in regexes:
            if pattern.search(line) is not None:
                matched.add(i)
                hit_counts[i] += 1
        # Nothing matched at all — by far the common case on a real capture,
        # so it short-circuits before building any list.
        if not matched:
            continue
        include_hits = [i for i in including_idx if i in matched]
        if not include_hits:
            continue
        exclude_hits = [i for i in excluding_idx if i in matched]
        if len(include_hits) >= 2:
            overlap_count += 1
            # Record every co-firing pair (order-independent) for the
            # operation-pattern summary.
            for a in range(len(include_hits)):
                for b in range(a + 1, len(include_hits)):
                    key = (include_hits[a], include_hits[b])
                    pair_counts[key] = pair_counts.get(key, 0) + 1
        if exclude_hits:
            for i in exclude_hits:
                excluded_by[i] += 1
            continue
        if len(include_hits) == 1:
            unique_hits[include_hits[0]] += 1
        surviving_count += 1
        survivor_rows.append((line_no, tuple(include_hits)))
        ts = _leading_timestamp(line)
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if len(surviving_preview) < preview_limit:
            first = filters[include_hits[0]]
            cleaned = clean_row_text(line)
            surviving_preview.append({
                "line_no": line_no,
                "text": cleaned,
                "matched": include_hits,
                "back_color": first["back_color"],
                "fore_color": first["fore_color"],
            })
        # Keep the true end even after the visible preview cap was reached.
        context_tail.append({
            "line_no": line_no,
            "text": clean_row_text(line),
        })

    per_filter = [{
        "index": i,
        "text": r["text"],
        "excluding": r["excluding"],
        "enabled": r["enabled"],
        "hits": hit_counts[i],
        # Marginal contribution: unique surviving lines for includes, or noise
        # lines actually dropped for excludes. Only meaningful for filters
        # that were actually enabled this run — None otherwise (not "0", to
        # avoid implying a disabled pattern was measured and found useless).
        "unique_hits": unique_hits.get(i) if r["enabled"] and not r["excluding"] else None,
        "dropped": excluded_by.get(i) if r["enabled"] and r["excluding"] else None,
        "back_color": r["back_color"],
        "fore_color": r["fore_color"],
    } for i, r in enumerate(filters)]

    text_by_index = {i: r["text"] for i, r in enabled}
    co_occurrence = [
        {"a": text_by_index[a], "b": text_by_index[b], "count": c}
        for (a, b), c in sorted(pair_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    ]

    if surviving_count > preview_limit:
        head_n = max(1, preview_limit - len(context_tail))
        context_preview = [
            {"line_no": row["line_no"], "text": row["text"]}
            for row in surviving_preview[:head_n]
        ] + [{"line_no": None, "text": "... (chronological middle omitted) ..."}] + list(context_tail)
    else:
        context_preview = [
            {"line_no": row["line_no"], "text": row["text"]}
            for row in surviving_preview
        ]

    return {
        "per_filter": per_filter,
        "overlap_count": overlap_count,
        "co_occurrence": co_occurrence,
        "time_span": {"first": first_ts, "last": last_ts},
        "surviving_count": surviving_count,
        "preview": surviving_preview,
        "survivor_rows": survivor_rows,
        "context_preview": context_preview,
        "total_lines": len(log_lines),
    }


def matched_keywords_for_line(filters: List[Dict], matched_filter_indices) -> List[Dict]:
    """Resolve a labeled line's matched filter indices into the actual
    keyword identities: [{"text", "excluding"}, ...], deduplicated.

    `matched_filter_indices` are positions in `filters`, exactly what
    compute_filter_stats' per-line "matched" list contains. This is what a
    labeled line's evidence is FOR. Unlike attributing to a historical edit
    (a Step in the operation journal), a filter's keyword+role is ALWAYS
    present and unambiguous, whether it was typed in by hand or came in
    wholesale from a loaded skill/.tat file — so there is no "couldn't
    correlate" state and no manual-correction UI needed. A line that co-fires
    2+ include filters credits ALL of them (the same idea compute_filter_
    stats' own overlap_count already tracks).
    """
    seen = set()
    result = []
    for idx in (matched_filter_indices or []):
        if not isinstance(idx, int) or not (0 <= idx < len(filters)):
            continue
        f = filters[idx]
        key = (str(f.get("text", "")).casefold(), bool(f.get("excluding")))
        if key in seen:
            continue
        seen.add(key)
        result.append({"text": str(f.get("text", "")), "excluding": bool(f.get("excluding"))})
    return result


def preprocess_log_for_llm(log_lines: List[str]) -> List[str]:
    """Light cleanup pass: strip trailing whitespace, drop empty lines."""
    return [line.rstrip() for line in log_lines if line.strip()]


def group_similar_logs(processed_lines: List[str], repeat_threshold: int = 2) -> List[str]:
    """Collapse consecutive identical/near-identical lines (ignoring a leading
    timestamp) into one line with a repeat-count suffix, to save tokens on
    noisy repeating log spam."""
    if not processed_lines:
        return []

    ts_re = re.compile(r'^\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3}\s*')

    def _normalize(line: str) -> str:
        return ts_re.sub('', line)

    grouped = []
    i = 0
    n = len(processed_lines)
    while i < n:
        current = processed_lines[i]
        current_norm = _normalize(current)
        count = 1
        j = i + 1
        while j < n and _normalize(processed_lines[j]) == current_norm:
            count += 1
            j += 1
        if count >= repeat_threshold:
            grouped.append(f"{current}  (repeated x{count})")
        else:
            grouped.append(current)
        i = j
    return grouped
