import json
import re


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?', '', raw).strip()
    raw = re.sub(r'```$', '', raw).strip()
    return raw


def _first_balanced(raw: str, open_ch: str, close_ch: str) -> str:
    """Slice out the FIRST complete {...}/[...], counting only brackets that
    sit outside strings. A greedy first-to-last regex instead merges two
    adjacent objects — or swallows trailing prose containing a brace — into
    something that cannot parse no matter how well-formed the real object was.
    """
    start = raw.find(open_ch)
    if start < 0:
        return ""
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return raw[start:]          # unterminated — reported as truncated below


_TRAILING_COMMA = re.compile(r',\s*([}\]])')


def _loads(text: str, closer: str):
    """json.loads plus the two defects an LLM actually emits: a literal
    newline inside a string value (strict=False accepts it) and a trailing
    comma before the closing bracket. Returns None if it still won't parse,
    after saying WHERE — a tail print can't show a defect in the middle of a
    4000-character object, which is the case that keeps recurring."""
    if not text:
        return None
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        try:
            return json.loads(_TRAILING_COMMA.sub(r'\1', text), strict=False)
        except json.JSONDecodeError:
            pass
        if not text.rstrip().endswith(closer):
            print(f"\u26a0\ufe0f  JSON looks truncated ({len(text)} chars, no closing "
                  f"{closer!r}) — the model likely hit max_tokens.")
        else:
            at = max(exc.pos - 90, 0)
            print(f"\u26a0\ufe0f  JSON parse failed: {exc.msg} at line {exc.lineno} "
                  f"col {exc.colno}. Around it: ...{text[at:exc.pos + 90]!r}...")
        return None


def parse_json_loose(raw: str) -> dict:
    """Extract the first {...} JSON object from an LLM response and parse it,
    tolerating markdown code fences / stray prose around it. Returns {} on
    failure so callers can degrade gracefully instead of crashing."""
    if not raw:
        return {}
    parsed = _loads(_first_balanced(_strip_fences(raw), '{', '}'), '}')
    return parsed if isinstance(parsed, dict) else {}


def parse_json_array_loose(raw: str) -> list:
    """Same as parse_json_loose but for a top-level JSON array."""
    if not raw:
        return []
    parsed = _loads(_first_balanced(_strip_fences(raw), '[', ']'), ']')
    return parsed if isinstance(parsed, list) else []
