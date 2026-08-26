"""Which data folder this launch is writing to — and which ones earlier
launches wrote to.

Copycat keeps its knowledge base beside the exe, because that folder is the
one the engineer actually chose. The price is that a second copy of the app
is a second, empty knowledge base, and nothing about that is visible: Export
succeeds, the YAML really is written, it is simply not under the folder they
believe they are running. One engineer ran the Downloads copy, then worked
from a copy in C:\\BT-work, and every skill they had exported stayed behind.

This module is the record that makes that noticeable. It stores ONLY a list
of data folders this user has genuinely launched from — no skill ever lives
here, and no folder is ever guessed at or scanned for, so the app can never
offer (or create) a location the engineer never opened.
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import List

from configs import path_configs
from services import skill_service

# Enough to cover "the zip I extracted, the folder I moved it to, last
# version, this version" several times over; past that the oldest entries
# describe folders nobody is coming back to.
_MAX_REMEMBERED = 12


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path or ""))


def current_root() -> str:
    return os.path.abspath(path_configs.DATA_DIR)


def _load() -> List[dict]:
    try:
        with open(path_configs.DATA_LOCATIONS_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
        return [e for e in entries if isinstance(e, dict) and e.get("path")]
    except Exception:
        # Missing, unreadable or corrupt: this is a convenience record, never
        # a source of truth. Starting over costs the engineer one prompt.
        return []


def _save(entries: List[dict]) -> None:
    try:
        os.makedirs(path_configs.USER_STATE_DIR, exist_ok=True)
        with open(path_configs.DATA_LOCATIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(entries[:_MAX_REMEMBERED], f, indent=2)
    except Exception as e:
        print(f"⚠️  Could not record this data folder ({e}) — the app is unaffected.")


def record_current() -> None:
    """Note that this launch is using this data folder. Called once at startup."""
    root = current_root()
    entries = [e for e in _load() if _norm(e.get("path")) != _norm(root)]
    entries.insert(0, {"path": root, "last_used": datetime.now().isoformat(timespec="seconds")})
    _save(entries)


def is_known(path: str) -> bool:
    """Only a folder this user has actually launched from may be imported.
    The API is reachable from the page, so an arbitrary path in the request
    would otherwise be a way to read any YAML on disk."""
    return any(_norm(e.get("path")) == _norm(path) for e in _load())


def is_app_folder(path: str) -> bool:
    """Where this launch writes, or where an earlier one did — the only paths
    the page is allowed to name."""
    return _norm(path) == _norm(current_root()) or is_known(path)


def reveal(path: str) -> None:
    """Show a data folder in the OS file manager. Callers must have passed the
    path through is_app_folder first."""
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def mark_imported(path: str, total: int) -> None:
    """Remember that this folder's skills have already been brought over, and
    how many it held at the time. Without this the banner keeps offering the
    same folder forever -- the source is copied, not moved, so it never stops
    looking like it has skills in it."""
    entries = _load()
    for entry in entries:
        if _norm(entry.get("path")) == _norm(path):
            entry["imported_at"] = datetime.now().isoformat(timespec="seconds")
            entry["imported_count"] = int(total)
            _save(entries)
            return


def other_locations() -> List[dict]:
    """Previously-used data folders that still exist and still hold skills."""
    here = _norm(current_root())
    found = []
    for entry in _load():
        path = entry.get("path") or ""
        if _norm(path) == here or not os.path.isdir(path):
            continue
        counts = skill_service.local_skill_counts(path)
        total = counts["wifi"] + counts["bt"]
        if not total:
            continue
        # Already brought over: only speak up again if that copy has been used
        # since and has skills this one has never seen.
        if entry.get("imported_at") and total <= int(entry.get("imported_count") or 0):
            continue
        found.append({"path": path, "last_used": entry.get("last_used") or "",
                      "skill_count": total})
    return found
