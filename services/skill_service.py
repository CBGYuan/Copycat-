"""
Skill storage — a skill is {name, description, keywords, exclusive,
tat_path, expert_rules}, persisted as one YAML file so it can be reviewed /
hand-edited. Schema matches wireless_ce_avatar/IntelAvatar's skills.yaml
(services/log_chatbot_service.Skill / load_skills_from_yaml), so files are
interchangeable between the two systems.

Skill *sources*, lowest to highest priority (higher overrides same-key
entries from lower):
  1. Shared corp drive baseline (skills_config/skills.yaml, or
     skills_config/bt_skills.yaml for Bluetooth) — the team's knowledge
     base, read-only.
  2. This engineer's own shared contribution file
     (skills_config/user_contributions/<username>__skills_*.yaml, WiFi
     only) — also read-only from this app's perspective; it's edited by
     hand / by the original IntelAvatar tool.
  3. Local data/skills/skills.yaml (WiFi) or data/skills/bt_skills.yaml
     (Bluetooth) — the only files this app ever writes to. save_skill()/
     delete_skill() take a `domain` ("wifi"/"bt") and only ever touch that
     domain's local file, never the shared drive and never the other
     domain's file — so a BT scenario can never get silently filed under
     WiFi (or vice versa) and nothing here can clobber a teammate's shared
     skill. The local file only ever holds skills this copycat instance
     genuinely originated — save_skill() edits a key IN PLACE only when it
     already exists LOCALLY; a key that currently resolves via the shared
     baseline or a contribution file (but was never saved locally) is
     treated as brand-new and gets its own fresh, non-colliding local key
     instead of shadowing the shared entry (see save_skill's docstring).
     This keeps the shared drive purely read-only in practice, not just in
     intent: a shared skill can never end up duplicated into, or silently
     overridden by, the local file.

WiFi and Bluetooth skills are kept in two entirely separate pools
end-to-end (app_config.skills / app_config.bt_skills) — see
load_shared_skills()'s return shape.
"""
import glob
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from configs import path_configs

# How many prior-version snapshots to keep per skill in `version_history`
# before the oldest is dropped — the audit trail is a rolling window, not an
# unbounded log, so a skill edited hundreds of times doesn't bloat the YAML.
# (Same rolling-window idea as AutoSkill's maintenance version history.)
_HISTORY_LIMIT = 30


class Skill(BaseModel):
    name: str
    description: str
    keywords: List[str] = Field(default_factory=list)
    exclusive: List[str] = Field(default_factory=list)
    tat_path: Optional[str] = None
    expert_rules: str = ""
    # AutoSkill-style versioning (Phase 1): `version` is bumped one patch level
    # on every UPDATE to an existing skill (see save_skill), and a snapshot of
    # the PRE-edit state is pushed onto `version_history` — giving the skill an
    # inspectable, roll-back-able evolution trail instead of silently
    # overwriting. A brand-new skill starts at "0.1.0" with an empty history.
    version: str = "0.1.0"
    version_history: List[dict] = Field(default_factory=list)


def _now_iso() -> str:
    """Seconds-precision local timestamp for a version snapshot's `saved_at`."""
    return datetime.now().isoformat(timespec="seconds")


def _bump_patch(version: str) -> str:
    """Increment the patch component of a semver-ish "MAJOR.MINOR.PATCH"
    string (0.1.0 -> 0.1.1). Anything not shaped like three integers resets to
    "0.1.1" — a safe, monotonic-ish default. Ported from AutoSkill's
    maintenance._bump_patch so learned skills iterate the same way."""
    parts = [p for p in str(version or "").split(".") if p.strip().isdigit()]
    if len(parts) != 3:
        return "0.1.1"
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    return f"{major}.{minor}.{patch + 1}"


def _coerce_history(raw_hist) -> List[dict]:
    """Read `version_history` back from YAML tolerantly: it's normally stored
    as a JSON string inside a `|` block scalar (see _dump_skills_yaml — keeps
    the hand-rolled writer simple and the multi-line expert_rules snapshots
    from bloating the file), but a hand-edited plain-YAML list is accepted too.
    Anything unparseable degrades to an empty trail rather than raising."""
    if isinstance(raw_hist, list):
        return [dict(h) for h in raw_hist if isinstance(h, dict)]
    if isinstance(raw_hist, str) and raw_hist.strip():
        try:
            parsed = json.loads(raw_hist)
            if isinstance(parsed, list):
                return [dict(h) for h in parsed if isinstance(h, dict)]
        except Exception:
            pass
    return []


def _parse_skills_yaml(raw: dict) -> Dict[str, Skill]:
    skills: Dict[str, Skill] = {}
    for key, val in (raw or {}).items():
        if not isinstance(val, dict):
            continue
        try:
            skills[key] = Skill(
                name=val.get("name", key),
                description=val.get("description", ""),
                keywords=val.get("keywords") or [],
                exclusive=val.get("exclusive") or [],
                tat_path=val.get("tat_path"),
                expert_rules=val.get("expert_rules", ""),
                version=str(val.get("version") or "0.1.0"),
                version_history=_coerce_history(val.get("version_history")),
            )
        except Exception as e:
            print(f"⚠️  Skipping invalid skill '{key}': {e}")
    return skills


def _load_yaml_skills_from_path(path: str) -> Dict[str, Skill]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return _parse_skills_yaml(raw)
    except Exception as e:
        print(f"⚠️  Failed to read skills from {path}: {e}")
        return {}


def _find_user_contribution_path(username: str, contrib_dir: str) -> Optional[str]:
    """The contribution filename is `<username>__skills_<date>.yaml` — date
    varies, so glob for it and take the most recently modified match.
    `contrib_dir` is parameterized so this can search either the LIVE share
    (refresh_shared_cache, syncing FROM it) or the local cache mirror
    (load_shared_skills, reading FROM it) with the same logic."""
    if not username or not os.path.isdir(contrib_dir):
        return None
    pattern = os.path.join(contrib_dir, f"{username}__skills_*.yaml")
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _local_path(domain: str) -> str:
    return path_configs.SKILLS_BT_YAML_PATH if domain == "bt" else path_configs.SKILLS_YAML_PATH


def _current_username() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def refresh_shared_cache() -> Dict[str, bool]:
    """Best-effort sync of the LIVE shared corp drive (path_configs.
    SKILLS_SHARE_*) into the local read-only mirror (SKILLS_CACHE_*) that
    load_shared_skills() actually reads from. Called once at app startup
    (configs.set_up_app.set_up), before load_shared_skills().

    Never raises: each of the three sources (WiFi baseline, BT baseline,
    this engineer's own contribution file) is copied independently, and a
    source that's unreachable (VPN down, share offline) simply leaves
    whatever was cached from the last successful sync untouched — shared
    skills don't vanish just because the network dropped mid-session, they
    only go stale until the next successful refresh. Returns which sources
    were actually refreshed this call, for the startup log line.
    """
    os.makedirs(path_configs.SKILLS_CACHE_DIR, exist_ok=True)
    os.makedirs(path_configs.SKILLS_CACHE_USER_CONTRIB_DIR, exist_ok=True)
    refreshed = {"shared_wifi": False, "shared_bt": False, "contribution": False}

    def _copy(src: str, dst: str) -> bool:
        try:
            if not src or not os.path.isfile(src):
                return False
            with open(src, "r", encoding="utf-8") as f:
                content = f.read()
            with open(dst, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception as e:
            print(f"⚠️  Could not refresh shared-skill cache from {src}: {e}")
            return False

    refreshed["shared_wifi"] = _copy(path_configs.SKILLS_SHARE_WIFI_PATH, path_configs.SKILLS_CACHE_WIFI_PATH)
    refreshed["shared_bt"] = _copy(path_configs.SKILLS_SHARE_BT_PATH, path_configs.SKILLS_CACHE_BT_PATH)

    username = _current_username()
    live_contrib_path = _find_user_contribution_path(username, path_configs.SKILLS_SHARE_USER_CONTRIB_DIR)
    if live_contrib_path:
        cached_contrib_path = os.path.join(path_configs.SKILLS_CACHE_USER_CONTRIB_DIR,
                                            os.path.basename(live_contrib_path))
        refreshed["contribution"] = _copy(live_contrib_path, cached_contrib_path)

    return refreshed


def load_shared_skills() -> Dict[str, Dict]:
    """Load every skill source and return:
      {"wifi": {key: Skill}, "bt": {key: Skill}, "sources": {...}}
    `wifi` is the merge of shared baseline -> this user's shared
    contribution -> local data/skills/local/skills.yaml (later wins). `bt`
    is the shared Bluetooth baseline -> local data/skills/local/
    bt_skills.yaml (later wins) — same override shape as wifi, just without
    a shared per-user contribution file for BT.

    "shared baseline" / "contribution" here mean the LOCAL read-only mirror
    (SKILLS_CACHE_*), NOT a live network read — see refresh_shared_cache(),
    which is what actually keeps that mirror in sync with the real share.
    This keeps every load (including the post-save app_config refresh in
    skills_routes.py/learning_routes.py) fast and immune to the network
    being unreachable mid-session; only a fresh app startup re-syncs it.

    `sources` records which paths actually loaded, for the startup log line.
    """
    username = _current_username()
    contrib_path = _find_user_contribution_path(username, path_configs.SKILLS_CACHE_USER_CONTRIB_DIR)

    ensure_skills_file("wifi")
    ensure_skills_file("bt")
    shared_wifi = _load_yaml_skills_from_path(path_configs.SKILLS_CACHE_WIFI_PATH)
    contrib = _load_yaml_skills_from_path(contrib_path) if contrib_path else {}
    local_wifi = _load_yaml_skills_from_path(path_configs.SKILLS_YAML_PATH)
    shared_bt = _load_yaml_skills_from_path(path_configs.SKILLS_CACHE_BT_PATH)
    local_bt = _load_yaml_skills_from_path(path_configs.SKILLS_BT_YAML_PATH)

    wifi: Dict[str, Skill] = {}
    wifi.update(shared_wifi)
    wifi.update(contrib)
    wifi.update(local_wifi)

    bt: Dict[str, Skill] = {}
    bt.update(shared_bt)
    bt.update(local_bt)

    return {
        "wifi": wifi,
        "bt": bt,
        "sources": {
            "shared_wifi": path_configs.SKILLS_CACHE_WIFI_PATH if shared_wifi else None,
            "contribution": contrib_path if contrib else None,
            "local": path_configs.SKILLS_YAML_PATH if local_wifi else None,
            "bt": path_configs.SKILLS_CACHE_BT_PATH if shared_bt else None,
            "local_bt": path_configs.SKILLS_BT_YAML_PATH if local_bt else None,
        },
    }


def ensure_skills_file(domain: str = "wifi") -> None:
    os.makedirs(path_configs.SKILLS_LOCAL_DIR, exist_ok=True)
    path = _local_path(domain)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_dump_skills_yaml({}))


def load_all_skills(domain: str = "wifi") -> Dict[str, Skill]:
    """Local-only load — kept for the save/delete round-trip below, which
    must only ever see what THIS app owns, not the shared/merged view."""
    ensure_skills_file(domain)
    return _load_yaml_skills_from_path(_local_path(domain))


def _slugify(name: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9_]+', '_', name.strip()).strip('_')
    return slug or "skill"


def _skill_to_raw(skill: Skill) -> dict:
    """Skill model -> the plain dict shape _dump_skills_yaml()/save_skill()
    expect (same shape as one entry read back by _parse_skills_yaml)."""
    entry = {
        "name": skill.name,
        "description": skill.description,
        "keywords": skill.keywords,
        "expert_rules": skill.expert_rules,
    }
    if skill.exclusive:
        entry["exclusive"] = skill.exclusive
    if skill.tat_path:
        entry["tat_path"] = skill.tat_path
    entry["version"] = skill.version or "0.1.0"
    if skill.version_history:
        entry["version_history"] = skill.version_history
    return entry


# ---- Hand-rolled YAML writer ----------------------------------------------
# yaml.safe_dump's default plain/single-quoted scalar styles wrap long lines
# with backslash line-continuations once a string exceeds its `width`, which
# is NOT how the hand-authored skills_config yaml files (module docstring
# above) look — those use double-quoted one-line name/description/keyword
# scalars and a literal `|` block for expert_rules, with no wrapping and no
# backslashes. This writer reproduces that exact shape so files stay
# interchangeable and diff-friendly against the shared corp yaml.
_SKILLS_YAML_HEADER = (
    "# version skills_2026-06-04-1 features:\n"
    "#   name: skill name\n"
    "#   description: a brief description of the skill\n"
    "#   keywords: use \"-\" to represent each keyword\n"
    "#   expert_rules: use \"|\" to start a multi-line string\n"
    "#   version: semver-ish patch, auto-bumped on each edit\n"
    "#   version_history: rolling JSON audit trail of prior versions"
)


def _yaml_dq(value) -> str:
    """Double-quoted YAML scalar. Only backslash/quote/newline need escaping
    inside a double-quoted scalar — nothing here ever needs line-wrapping."""
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def _yaml_block_literal(text: str, indent: int) -> str:
    """Render `text` as the body of a `key: |` literal block scalar, each
    line indented by `indent` spaces (blank lines left bare, not padded)."""
    text = str(text)
    if not text.endswith("\n"):
        text += "\n"
    pad = " " * indent
    body_lines = text.split("\n")[:-1]  # drop the artifact from the trailing \n
    return "\n".join(pad + line if line else "" for line in body_lines)


def _dump_skills_yaml(skills_raw: dict) -> str:
    out = [_SKILLS_YAML_HEADER, ""]
    for key, entry in (skills_raw or {}).items():
        if not isinstance(entry, dict):
            continue
        out.append(f"{key}:")
        out.append(f"  name: {_yaml_dq(entry.get('name', key))}")
        out.append(f"  description: {_yaml_dq(entry.get('description', ''))}")
        keywords = entry.get("keywords") or []
        if keywords:
            out.append("  keywords:")
            out.extend(f"    - {_yaml_dq(kw)}" for kw in keywords)
        exclusive = entry.get("exclusive") or []
        if exclusive:
            out.append("  exclusive:")
            out.extend(f"    - {_yaml_dq(ex)}" for ex in exclusive)
        expert_rules = entry.get("expert_rules") or ""
        if expert_rules:
            out.append("  expert_rules: |")
            out.append(_yaml_block_literal(expert_rules, indent=4))
        if entry.get("tat_path"):
            out.append(f"  tat_path: {_yaml_dq(entry['tat_path'])}")
        # Versioning fields last, so the hand-authored name/description/
        # keywords/expert_rules stay in their familiar order at the top and the
        # audit fields sit at the bottom. version_history is serialized as
        # single-line JSON inside a `|` block: it round-trips cleanly through
        # yaml.safe_load + json.loads (see _coerce_history) without the
        # hand-rolled writer needing to emit nested list-of-dict YAML.
        out.append(f"  version: {_yaml_dq(entry.get('version') or '0.1.0')}")
        history = entry.get("version_history") or []
        if history:
            out.append("  version_history: |")
            out.append(_yaml_block_literal(json.dumps(history, ensure_ascii=False), indent=4))
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _write_skills_yaml(raw: dict, domain: str = "wifi") -> None:
    with open(_local_path(domain), "w", encoding="utf-8") as f:
        f.write(_dump_skills_yaml(raw))


def save_skill(skill_key: Optional[str], skill: Skill, domain: str = "wifi",
               base: Optional[Dict[str, Skill]] = None) -> str:
    """Create (skill_key=None/unknown/shared-origin) or update a skill entry.
    Returns the key actually used.

    The LOCAL file for `domain` is treated as append-only relative to the
    shared corp drive: it only ever contains skills this copycat instance
    genuinely originated. `skill_key` is only editable IN PLACE when it
    already resolves to an entry in the local file itself — a key that
    currently resolves via the shared baseline / a teammate's contribution
    file (`base`, normally the caller's already-loaded app_config.skills /
    app_config.bt_skills) but has never been saved locally is NOT edited or
    shadowed: it's treated exactly like skill_key=None, minting a fresh,
    non-colliding local key instead (`base` is consulted only to avoid that
    new key accidentally colliding with anything already visible — shared,
    contribution, or local). This is what keeps the shared library purely
    read-only: it never gets mirrored into, or overridden by, this app's own
    save path (see this module's docstring). Any merge/union of shared-skill
    content into a new local skill must already have happened in `skill`
    itself before calling this (see learning_service.basic_merge_draft) —
    this function only decides WHERE that content is written, never how it's
    combined.

    `domain` MUST match where the caller intends this skill to live ("wifi"
    or "bt") — saving a BT scenario with domain left at the "wifi" default is
    what used to make it silently show up merged into the WiFi pool instead
    of the Bluetooth one. If `base` is omitted, falls back to a fresh
    load_shared_skills() read (keeps this usable standalone/without an
    app_config wired up).
    """
    ensure_skills_file(domain)
    if base is None:
        base = load_shared_skills()["bt" if domain == "bt" else "wifi"]
    # `local_raw` is the ONLY thing ever read as "current state" or written
    # back — it's always just this domain's own local file, never the full
    # shared+contribution+local merge. `merged_raw` (the full view) is used
    # SOLELY to avoid minting a new key that collides with anything already
    # visible; it never gets persisted.
    local_raw = {k: _skill_to_raw(v) for k, v in load_all_skills(domain).items()}
    merged_raw = {k: _skill_to_raw(v) for k, v in base.items()}

    is_new = not skill_key or skill_key not in local_raw
    key = skill_key or _slugify(skill.name)
    if is_new:
        base_key = key if key not in merged_raw else _slugify(skill.name)
        key = base_key
        i = 2
        while key in merged_raw:
            key = f"{base_key}_{i}"
            i += 1
        # Brand-new skill: start the version line at 0.1.0 with an empty trail,
        # unless the caller deliberately supplied one already.
        if not (skill.version or "").strip():
            skill.version = "0.1.0"
    else:
        # UPDATE of an already-local skill: push a snapshot of the PRE-edit
        # state onto the history trail and bump the patch version. The old
        # state is read from `local_raw[key]` (the currently-stored LOCAL
        # entry), NOT from the incoming `skill` (which is the just-edited NEW
        # state) — so the trail records what the skill looked like before
        # this save, even when the caller rebuilt the Skill without carrying
        # version fields through (e.g. the Edit-Skill modal's save payload).
        # This makes save_skill the single source of truth for version
        # bumping regardless of what the route passes in.
        old_entry = local_raw[key]
        old_version = str(old_entry.get("version") or "0.1.0")
        history = _coerce_history(old_entry.get("version_history"))
        history.append({
            "version": old_version,
            "name": old_entry.get("name", ""),
            "description": old_entry.get("description", ""),
            "keywords": list(old_entry.get("keywords") or []),
            "exclusive": list(old_entry.get("exclusive") or []),
            "expert_rules": old_entry.get("expert_rules", ""),
            "saved_at": _now_iso(),
        })
        skill.version = _bump_patch(old_version)
        skill.version_history = history[-_HISTORY_LIMIT:]

    local_raw[key] = _skill_to_raw(skill)
    _write_skills_yaml(local_raw, domain)
    return key


def delete_skill(skill_key: str, domain: str = "wifi") -> bool:
    """Deletes from the LOCAL file for the given domain only. If `skill_key`
    actually came from the shared baseline/contribution (not present
    locally), there is nothing to delete — this app never touches the
    shared drive."""
    ensure_skills_file(domain)
    with open(_local_path(domain), "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if skill_key not in raw:
        return False
    del raw[skill_key]
    _write_skills_yaml(raw, domain)
    return True
