import json
import re


def parse_json_loose(raw: str) -> dict:
    """Extract the first {...} JSON object from an LLM response and parse it,
    tolerating markdown code fences / stray prose around it. Returns {} on
    failure so callers can degrade gracefully instead of crashing."""
    if not raw:
        return {}
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?', '', raw).strip()
    raw = re.sub(r'```$', '', raw).strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def parse_json_array_loose(raw: str) -> list:
    """Same as parse_json_loose but for a top-level JSON array."""
    if not raw:
        return []
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?', '', raw).strip()
    raw = re.sub(r'```$', '', raw).strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        return []
    try:
        arr = json.loads(match.group(0))
        return arr if isinstance(arr, list) else []
    except json.JSONDecodeError:
        return []
