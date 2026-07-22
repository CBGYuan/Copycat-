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
     skill. save_skill() also snapshots the FULL merged view (`base`,
     normally app_config.skills/bt_skills) into that local file on every
     save, not just the one skill being edited — so the local file is a
     complete standalone copy of everything this engineer has ever loaded,
     and local wins over the shared drive for every key it has ever
     touched this way (see save_skill's docstring for the trade-off).

WiFi and Bluetooth skills are kept in two entirely separate pools
end-to-end (app_config.skills / app_config.bt_skills) — see
load_shared_skills()'s return shape.
"""
import glob
import os
import re
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from configs import path_configs


class Skill(BaseModel):
    name: str
    description: str
    keywords: List[str] = Field(default_factory=list)
    exclusive: List[str] = Field(default_factory=list)
    tat_path: Optional[str] = None
    expert_rules: str = ""


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


def _find_user_contribution_path(username: str) -> Optional[str]:
    """The contribution filename is `<username>__skills_<date>.yaml` — date
    varies, so glob for it and take the most recently modified match."""
    if not username or not os.path.isdir(path_configs.SKILLS_SHARE_USER_CONTRIB_DIR):
        return None
    pattern = os.path.join(path_configs.SKILLS_SHARE_USER_CONTRIB_DIR, f"{username}__skills_*.yaml")
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _local_path(domain: str) -> str:
    return path_configs.SKILLS_BT_YAML_PATH if domain == "bt" else path_configs.SKILLS_YAML_PATH


def load_shared_skills() -> Dict[str, Dict]:
    """Load every skill source and return:
      {"wifi": {key: Skill}, "bt": {key: Skill}, "sources": {...}}
    `wifi` is the merge of shared baseline -> this user's shared
    contribution -> local data/skills/skills.yaml (later wins). `bt` is the
    shared Bluetooth baseline -> local data/skills/bt_skills.yaml (later
    wins) — same override shape as wifi, just without a shared per-user
    contribution file for BT.
    `sources` records which paths actually loaded, for the startup log line.
    """
    username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    contrib_path = _find_user_contribution_path(username)

    ensure_skills_file("wifi")
    ensure_skills_file("bt")
    shared_wifi = _load_yaml_skills_from_path(path_configs.SKILLS_SHARE_WIFI_PATH)
    contrib = _load_yaml_skills_from_path(contrib_path) if contrib_path else {}
    local_wifi = _load_yaml_skills_from_path(path_configs.SKILLS_YAML_PATH)
    shared_bt = _load_yaml_skills_from_path(path_configs.SKILLS_SHARE_BT_PATH)
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
            "shared_wifi": path_configs.SKILLS_SHARE_WIFI_PATH if shared_wifi else None,
            "contribution": contrib_path if contrib else None,
            "local": path_configs.SKILLS_YAML_PATH if local_wifi else None,
            "bt": path_configs.SKILLS_SHARE_BT_PATH if shared_bt else None,
            "local_bt": path_configs.SKILLS_BT_YAML_PATH if local_bt else None,
        },
    }


def ensure_skills_file(domain: str = "wifi") -> None:
    os.makedirs(path_configs.SKILLS_DIR, exist_ok=True)
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
    "#   expert_rules: use \"|\" to start a multi-line string"
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
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _write_skills_yaml(raw: dict, domain: str = "wifi") -> None:
    with open(_local_path(domain), "w", encoding="utf-8") as f:
        f.write(_dump_skills_yaml(raw))


def save_skill(skill_key: Optional[str], skill: Skill, domain: str = "wifi",
               base: Optional[Dict[str, Skill]] = None) -> str:
    """Create (skill_key=None/unknown) or update a skill entry. Returns the
    key used.

    Snapshots the FULL merged view for this domain — `base` (normally the
    caller's already-loaded app_config.skills/app_config.bt_skills: shared
    corp baseline + this engineer's shared contribution + whatever was
    already local) with `skill` written in under `skill_key` (or a fresh
    slugified key for a new skill) — into the LOCAL file for `domain`, never
    the shared corp drive. This makes the local file a complete, standalone
    copy of everything this engineer has ever loaded for this domain, not
    just an incremental diff of what was edited through this app, so it
    keeps working even if the shared drive becomes unreachable later.

    Trade-off: once a skill has been mirrored into the local file this way,
    local ALWAYS wins over the shared drive for that key from then on (see
    load_shared_skills()'s merge order) — if a teammate later improves that
    same skill on the shared baseline, this local copy keeps shadowing it
    until someone manually deletes the local override.

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
    raw = {k: _skill_to_raw(v) for k, v in base.items()}

    is_new = not skill_key or skill_key not in raw
    key = skill_key or _slugify(skill.name)
    if is_new:
        base_key = key if key not in raw else _slugify(skill.name)
        key = base_key
        i = 2
        while key in raw:
            key = f"{base_key}_{i}"
            i += 1

    raw[key] = _skill_to_raw(skill)
    _write_skills_yaml(raw, domain)
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
