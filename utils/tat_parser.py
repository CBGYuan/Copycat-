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
import re
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional


def _to_bool(val: Optional[str]) -> bool:
    return (val or "").strip().lower() == "y"


def _color(hex6: Optional[str]) -> Optional[str]:
    """Normalize a TAT color attribute ("ff0000") to CSS ("#ff0000")."""
    if not hex6:
        return None
    hex6 = hex6.strip()
    return f"#{hex6}" if not hex6.startswith("#") else hex6


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


def _leading_timestamp(line: str):
    m = _TS_RE.match(line)
    return m.group(1) if m else None


def compute_filter_stats(log_lines: List[str], filters: List[Dict],
                          preview_limit: int = 500) -> Dict:
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
    """
    enabled = [(i, r) for i, r in enumerate(filters) if r["enabled"]]
    including = [(i, r) for i, r in enabled if not r["excluding"]]
    excluding = [(i, r) for i, r in enabled if r["excluding"]]

    # Matchers/hit_counts cover EVERY filter, not just enabled ones, so a
    # disabled pattern still gets a real raw hit count in per_filter below
    # instead of staying blank in the UI until the engineer checks it.
    matchers = {i: _line_matcher(r) for i, r in enumerate(filters)}
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
    surviving_count = 0
    first_ts = None
    last_ts = None

    for line_no, line in enumerate(log_lines, start=1):
        line_lower = line.lower()
        # Raw match count for every pattern, enabled or not — independent of
        # the include/exclude interplay below.
        for i in range(len(filters)):
            if matchers[i](line_lower, line):
                hit_counts[i] += 1
        include_hits = [i for i, r in including if matchers[i](line_lower, line)]
        if not include_hits:
            continue
        exclude_hits = [i for i, r in excluding if matchers[i](line_lower, line)]
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
        ts = _leading_timestamp(line)
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if len(surviving_preview) < preview_limit:
            first = filters[include_hits[0]]
            cleaned = re.sub(r"^\d+\t", "", line).rstrip("\n")
            surviving_preview.append({
                "line_no": line_no,
                "text": cleaned,
                "matched": include_hits,
                "back_color": first["back_color"],
                "fore_color": first["fore_color"],
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

    return {
        "per_filter": per_filter,
        "overlap_count": overlap_count,
        "co_occurrence": co_occurrence,
        "time_span": {"first": first_ts, "last": last_ts},
        "surviving_count": surviving_count,
        "preview": surviving_preview,
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
