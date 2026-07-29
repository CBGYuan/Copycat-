"""Skill-level memory — what actually happened each time a skill was used.

Distinct from the skill's `version_history`, which records what the skill
*became*. This records how it *performed*: how much of the log it matched, and
what the engineer had to do immediately afterwards to make it useful. Those
are two different questions and only one of them is answerable from the skill's
own content.

The data is not new. Copycat already measures every filter run (filter_stats)
and journals every edit with its marginal effect (utils.operation_journal); all
of it was being discarded when the session ended. This module keeps the two
numbers that outlive a session:

  uses          — how many times the skill was loaded as a baseline
  added_after   — keywords the engineer added right after loading it, counted
                  per keyword across all uses

`added_after` is the useful one. A keyword the engineer has to add every single
time they load a skill is a gap in that skill, stated by their hands rather
than their words — and it is measured, not inferred from anything a model said.
Three engineers-worth of "I always have to add BT_COEX_DENY to this" becomes a
concrete suggestion to absorb it.

Stored in a SIDECAR json file, never in the skills YAML. The YAML is the file
Avatar loads and a teammate may read; usage telemetry has no business in it,
and appending to it on every filter run would churn the version history and the
backup for information that is not part of the skill.
"""
import json
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional

from configs import path_configs

_LOCK = threading.RLock()

# How many keywords to report as "always added after loading this skill".
_TOP_GAPS = 5

# Below this many uses, "the engineer added X every time" is one anecdote, not
# a pattern. Suggestions stay silent until a skill has actually been exercised.
MIN_USES_FOR_GAP = 3


def _path() -> str:
    return os.path.join(path_configs.SKILLS_LOCAL_DIR, "skill_memory.json")


def _load() -> Dict[str, dict]:
    """Whole file, or {} if it is missing/unreadable.

    Degrades silently on purpose — unlike the skills file, this is derived
    telemetry. Losing it costs a suggestion; refusing to start the app over it
    would cost the session.
    """
    path = _path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"⚠️  Could not read skill memory ({path}): {e}")
        return {}


def _save(data: Dict[str, dict]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"⚠️  Could not write skill memory ({path}): {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _entry(data: Dict[str, dict], key: str) -> dict:
    e = data.setdefault(key, {})
    e.setdefault("uses", 0)
    e.setdefault("added_after", {})
    e.setdefault("last_used", None)
    e.setdefault("last_matched_lines", None)
    return e


def record_load(skill_key: str, matched_lines: Optional[int] = None) -> None:
    """The skill was loaded as this session's baseline."""
    if not skill_key:
        return
    with _LOCK:
        data = _load()
        e = _entry(data, skill_key)
        e["uses"] += 1
        e["last_used"] = datetime.now().isoformat(timespec="seconds")
        if matched_lines is not None:
            e["last_matched_lines"] = int(matched_lines)
        _save(data)


def record_matched(skill_key: str, matched_lines: int) -> None:
    """How many lines this skill's filter set survives on the current log.

    Overwrites rather than accumulating: the interesting figure is the latest
    one, and averaging across captures of wildly different sizes would produce
    a number that describes nothing.
    """
    if not skill_key:
        return
    with _LOCK:
        data = _load()
        _entry(data, skill_key)["last_matched_lines"] = int(matched_lines)
        _save(data)


def record_added_keyword(skill_key: str, text: str) -> None:
    """The engineer added `text` to the filter while this skill was the
    baseline — i.e. the skill did not already cover it."""
    if not skill_key or not (text or "").strip():
        return
    with _LOCK:
        data = _load()
        e = _entry(data, skill_key)
        counts = e["added_after"]
        counts[text] = counts.get(text, 0) + 1
        _save(data)


def forget(skill_key: str) -> None:
    """Drop a deleted skill's memory so a later skill reusing the key doesn't
    inherit a stranger's history."""
    with _LOCK:
        data = _load()
        if data.pop(skill_key, None) is not None:
            _save(data)


def stats_for(skill_key: str) -> dict:
    """Memory for one skill, plus the gaps worth acting on.

    `coverage_gaps` are keywords added after loading this skill in at least
    MIN_USES_FOR_GAP separate sessions AND in most of them — a keyword added
    once is a one-off, a keyword added nearly every time is something the
    skill should probably own.
    """
    e = _load().get(skill_key) or {}
    uses = int(e.get("uses") or 0)
    added = e.get("added_after") or {}
    gaps = []
    if uses >= MIN_USES_FOR_GAP:
        gaps = sorted(
            ({"text": t, "times": c, "of_uses": uses}
             for t, c in added.items() if c >= MIN_USES_FOR_GAP and c * 2 >= uses),
            key=lambda g: g["times"], reverse=True)[:_TOP_GAPS]
    return {
        "uses": uses,
        "last_used": e.get("last_used"),
        "last_matched_lines": e.get("last_matched_lines"),
        "added_after": added,
        "coverage_gaps": gaps,
    }


def unused_keys(pool_keys: List[str]) -> List[str]:
    """Keys in `pool_keys` this workbench has never loaded.

    The pruning signal from the paper set, stated honestly: it means "never
    used HERE", not "useless". A shared skill can be central to a teammate and
    still never have been opened on this machine, so this is only ever input
    to a human decision, never grounds for deleting anything automatically.
    """
    data = _load()
    return [k for k in pool_keys if int((data.get(k) or {}).get("uses") or 0) == 0]
