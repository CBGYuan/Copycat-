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
import shutil
import tempfile
import threading
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

# Serializes the whole read-modify-write cycle of save_skill/delete_skill.
# Flask runs threaded, so two requests saving different skills at the same
# time would otherwise each read the file, each apply their own edit to their
# own copy, and the second write would drop the first one's skill entirely.
# An RLock (not Lock) because restore_version calls save_skill while holding it.
_WRITE_LOCK = threading.RLock()

# Suffix of the one-generation backup kept beside the local skills file. It is
# NOT read automatically — see _write_skills_yaml for why — it exists so a bad
# hand-edit or an unwanted bulk change is recoverable by hand.
BACKUP_SUFFIX = ".bak"


class SkillStoreError(RuntimeError):
    """The local skills file exists but could not be understood. Raised
    instead of degrading to "no skills", because every write path starts by
    reading this file: silently treating an unreadable file as empty would
    make the very next save persist that emptiness and destroy the real
    content for good."""


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
    # Lineage — set when this skill was exported as an EXTENSION of another
    # one (see utils.skill_dedup.build_extension_skill). `parent` is the key
    # it was derived from; `lineage` is the full root→…→parent chain, so a
    # skill several generations deep still knows its whole ancestry without
    # having to walk and re-resolve every link.
    #
    # These are documentation/organisation only — the keywords, exclusive and
    # expert_rules written to YAML are always FULLY RESOLVED (parent content
    # merged in), never delta-only. That is not a stylistic choice: Avatar's
    # loader (wireless_ce_avatar/services/log_chatbot_service.
    # load_skills_from_yaml) reads exactly name/description/keywords/
    # exclusive/expert_rules and silently ignores every other key, so a
    # delta-only child would load there with a fraction of its keywords and
    # produce quietly wrong analysis. Flat-and-complete is what keeps the
    # exported file correct in the system that actually runs it.
    parent: Optional[str] = None
    lineage: List[str] = Field(default_factory=list)


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
                parent=val.get("parent") or None,
                lineage=[str(x) for x in (val.get("lineage") or []) if str(x).strip()],
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


def list_skill_sources(domain: str = "wifi") -> List[Dict]:
    """Every YAML in the local mirror that can serve as this domain's baseline.

    The mirror is a copy of the corp share (see refresh_shared_cache), so this
    is "which version of the team's knowledge base am I standing on" — the
    team baseline for the domain, plus, for WiFi, each engineer contribution
    file found there. Entries are ordered baseline-first, then contributions
    newest-first, and each carries enough to be chosen from a dropdown without
    a second round-trip.
    """
    domain = "bt" if domain == "bt" else "wifi"
    out: List[Dict] = []

    def _entry(path: str, kind: str, label: str) -> Optional[Dict]:
        if not path or not os.path.isfile(path):
            return None
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
        except OSError:
            mtime = None
        return {
            "key": os.path.basename(path),
            "path": path,
            "kind": kind,
            "label": label,
            "skill_count": len(_load_yaml_skills_from_path(path)),
            "updated": mtime,
        }

    if domain == "wifi":
        base = _entry(path_configs.SKILLS_CACHE_WIFI_PATH, "shared", "Team baseline (skills.yaml)")
        if base:
            out.append(base)
        contribs = sorted(
            glob.glob(os.path.join(path_configs.SKILLS_CACHE_USER_CONTRIB_DIR, "*.yaml")),
            key=os.path.getmtime, reverse=True)
        for path in contribs:
            name = os.path.basename(path)
            who = name.split("__")[0] if "__" in name else name
            e = _entry(path, "contribution", f"{who}'s contribution ({name})")
            if e:
                out.append(e)
    else:
        base = _entry(path_configs.SKILLS_CACHE_BT_PATH, "shared", "Team baseline (bt_skills.yaml)")
        if base:
            out.append(base)
    return out


def default_source_path(domain: str = "wifi") -> str:
    """The baseline used when nothing has been chosen: the team's own file for
    this domain, which is what the app has always started on."""
    return (path_configs.SKILLS_CACHE_BT_PATH if domain == "bt"
            else path_configs.SKILLS_CACHE_WIFI_PATH)


def load_shared_skills(wifi_source: Optional[str] = None,
                       bt_source: Optional[str] = None) -> Dict[str, Dict]:
    """Load each domain's pool and return:
      {"wifi": {key: Skill}, "bt": {key: Skill}, "sources": {...}}

    Each domain is exactly TWO layers: ONE chosen baseline file, then this
    app's own local file on top (local wins).

    `wifi_source` / `bt_source` name the baseline file — any entry from
    list_skill_sources(); omitted means that domain's team file
    (default_source_path). An unreadable or unknown path falls back to the
    default rather than leaving the pool empty.

    There is deliberately NO automatic contribution merge any more. It used to
    stack shared -> this engineer's contribution -> local implicitly, which
    meant the effective baseline was a three-way merge nobody could see or
    reproduce: a skill could resolve from any of the three and the UI could
    only say which one won after the fact. A contribution file is now simply
    one of the baselines you can CHOOSE, and whatever is chosen is the whole
    of it — which is also what makes "the inherited part is immutable and is
    exactly this file" a statement that can be honoured (see
    utils.skill_dedup.build_extension_skill).

    "baseline" here means the LOCAL read-only mirror (SKILLS_CACHE_*), NOT a
    live network read — see refresh_shared_cache(), which keeps that mirror in
    sync with the real share. This keeps every load (including the post-save
    app_config refresh in skills_routes.py/learning_routes.py) fast and immune
    to the network being unreachable mid-session; only a fresh app startup
    re-syncs it.

    `sources` records which paths actually loaded, for the startup log line.
    """
    def _resolve(path: Optional[str], domain: str) -> str:
        if path and os.path.isfile(path):
            return path
        return default_source_path(domain)

    wifi_path = _resolve(wifi_source, "wifi")
    bt_path = _resolve(bt_source, "bt")

    ensure_skills_file("wifi")
    ensure_skills_file("bt")
    base_wifi = _load_yaml_skills_from_path(wifi_path)
    local_wifi = _load_yaml_skills_from_path(path_configs.SKILLS_YAML_PATH)
    base_bt = _load_yaml_skills_from_path(bt_path)
    local_bt = _load_yaml_skills_from_path(path_configs.SKILLS_BT_YAML_PATH)

    wifi: Dict[str, Skill] = {}
    wifi.update(base_wifi)
    wifi.update(local_wifi)

    bt: Dict[str, Skill] = {}
    bt.update(base_bt)
    bt.update(local_bt)

    return {
        "wifi": wifi,
        "bt": bt,
        "sources": {
            "shared_wifi": wifi_path if base_wifi else None,
            "wifi_source": wifi_path,
            "bt_source": bt_path,
            "local": path_configs.SKILLS_YAML_PATH if local_wifi else None,
            "bt": bt_path if base_bt else None,
            "local_bt": path_configs.SKILLS_BT_YAML_PATH if local_bt else None,
        },
    }


def skill_origins(domain: str = "wifi", source_path: Optional[str] = None) -> Dict[str, str]:
    """Which FILE each visible skill key actually resolves from:
    "local" | "contribution" | "shared".

    Same two layers and precedence as load_shared_skills (local beats the
    chosen baseline), so the answer is always the source whose content is the
    one in play. "contribution" vs "shared" distinguishes only WHAT KIND of
    baseline was chosen — both are read-only mirrors of the corp drive.

    The Skills page needs this to be honest about what can be edited: only
    "local" entries are writable, and Save on anything else mints a NEW local
    skill rather than modifying the shared original (see save_skill).
    """
    domain = "bt" if domain == "bt" else "wifi"
    path = source_path if (source_path and os.path.isfile(source_path)) else default_source_path(domain)
    kind = "contribution" if _is_contribution_path(path) else "shared"
    origins: Dict[str, str] = {key: kind for key in _load_yaml_skills_from_path(path)}
    for key in _load_yaml_skills_from_path(_local_path(domain)):
        origins[key] = "local"
    return origins


def _is_contribution_path(path: str) -> bool:
    """A per-engineer contribution file rather than the team baseline — they
    live in the mirror's user_contributions/ subfolder."""
    try:
        return os.path.basename(os.path.dirname(path or "")) == os.path.basename(
            path_configs.SKILLS_CACHE_USER_CONTRIB_DIR)
    except Exception:
        return False


def ensure_skills_file(domain: str = "wifi") -> None:
    os.makedirs(path_configs.SKILLS_LOCAL_DIR, exist_ok=True)
    path = _local_path(domain)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_dump_skills_yaml({}))


def load_all_skills(domain: str = "wifi") -> Dict[str, Skill]:
    """Local-only load — the read half of the save/delete round-trip below,
    which must only ever see what THIS app owns, not the shared/merged view.

    STRICT on purpose, unlike every other load in this module. The rest of
    the app reads skills to DISPLAY them, so a broken file degrading to "no
    skills" keeps the app usable. This one is read to be written straight
    back: if a corrupted (or hand-mangled) local file quietly parsed as
    empty, the next save would write that emptiness over the real content
    and the loss would become permanent. Refusing loudly leaves the file
    untouched and recoverable — including from the .bak beside it.
    """
    ensure_skills_file(domain)
    path = _local_path(domain)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        raw = yaml.safe_load(text) or {}
    except OSError as e:
        raise SkillStoreError(f"Could not read the local skills file {path}: {e}") from e
    except yaml.YAMLError as e:
        raise SkillStoreError(
            f"The local skills file {path} is not valid YAML, so it cannot be safely "
            f"rewritten: {e}. The previous version may be recoverable from "
            f"{path}{BACKUP_SUFFIX}."
        ) from e
    if not isinstance(raw, dict):
        raise SkillStoreError(
            f"The local skills file {path} does not contain a mapping of skills; "
            f"refusing to overwrite it."
        )
    return _parse_skills_yaml(raw)


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
    # Lineage travels with the skill through every save/load round-trip. Without
    # this the chain recorded by utils.skill_dedup.build_extension_skill would be
    # dropped the moment the draft went through the Edit-Skill modal, and the
    # exported file would look like an unrelated standalone skill that merely
    # happens to repeat its parent's keywords.
    if skill.parent:
        entry["parent"] = skill.parent
    if skill.lineage:
        entry["lineage"] = list(skill.lineage)
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
        # Lineage before the version block. Avatar's loader ignores both (it
        # only reads name/description/keywords/exclusive/expert_rules), which
        # is exactly why carrying the chain here is safe: it documents the
        # ancestry for Copycat and for a human reading the file, without
        # changing a single thing about how the skill actually runs.
        if entry.get("parent"):
            out.append(f"  parent: {_yaml_dq(entry['parent'])}")
        lineage = entry.get("lineage") or []
        if lineage:
            out.append("  lineage:")
            out.extend(f"    - {_yaml_dq(a)}" for a in lineage)
        out.append(f"  version: {_yaml_dq(entry.get('version') or '0.1.0')}")
        history = entry.get("version_history") or []
        if history:
            out.append("  version_history: |")
            out.append(_yaml_block_literal(json.dumps(history, ensure_ascii=False), indent=4))
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _write_skills_yaml(raw: dict, domain: str = "wifi") -> None:
    """Persist the local skills file so it is never left half-written.

    The previous implementation was `open(path, "w")` followed by
    `f.write(_dump_skills_yaml(raw))`. Python truncates the file the moment
    it is opened and only THEN evaluates the argument, so any failure while
    serializing — one odd field on one skill was enough — left every local
    skill, and every `version_history` trail with it, permanently gone. The
    Skill Library's whole version-rollback promise sits on this file, so it
    must not be destroyable by a bad value.

    Three properties, in the order they matter:

    1. Serialize BEFORE opening anything. If _dump_skills_yaml raises, the
       existing file has not been touched at all.
    2. Write to a temp file in the same directory, fsync it, then
       os.replace() onto the target. os.replace is atomic on both POSIX and
       Windows, so a reader sees either the whole old file or the whole new
       one — never a partial write, even if the process dies mid-save.
    3. Keep one generation of backup (BACKUP_SUFFIX) taken from the last
       known-good file. Deliberately NOT restored automatically: an empty
       skills file is a perfectly legitimate state (fresh install, or the
       last skill was deleted on purpose), and nothing in the content can
       distinguish that from an accident. Auto-restoring would resurrect
       skills the engineer meant to delete. It is a manual recovery path.
    """
    content = _dump_skills_yaml(raw)   # (1) may raise; file still intact

    path = _local_path(domain)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    if os.path.exists(path):
        try:
            shutil.copy2(path, path + BACKUP_SUFFIX)   # (3)
        except OSError as e:
            # A missing backup is not worth failing the save over — the
            # atomic replace below is what actually protects the data.
            print(f"⚠️  Could not refresh skills backup {path}{BACKUP_SUFFIX}: {e}")

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".skills-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)                     # (2)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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

    The whole read-modify-write runs under _WRITE_LOCK. Flask serves requests
    on threads, so two concurrent saves would otherwise each read the file,
    each apply their own edit to their own in-memory copy, and the later
    write would silently drop the earlier one's skill.
    """
    with _WRITE_LOCK:
        return _save_skill_locked(skill_key, skill, domain, base)


def _save_skill_locked(skill_key: Optional[str], skill: Skill, domain: str,
                       base: Optional[Dict[str, Skill]]) -> str:
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


def restore_version(skill_key: str, version: str, domain: str = "wifi",
                    base: Optional[Dict[str, Skill]] = None) -> Optional[str]:
    """Roll a LOCAL skill back to one of its recorded `version_history`
    snapshots. Returns the key written, or None if the skill isn't local or
    that version isn't in its trail.

    Deliberately NOT destructive and NOT a branch: the restore is applied as a
    normal forward save, so the state being rolled back FROM is itself pushed
    onto the history first and the version number goes UP, not back. Restoring
    v0.1.1 while at v0.1.4 lands you at v0.1.5 carrying v0.1.1's content, with
    v0.1.4 still recoverable. A rollback that rewound the version counter, or
    truncated the trail, would make "restore" the one operation in the app
    capable of losing work — which is exactly what an audit trail exists to
    prevent.

    Only lineage-independent fields are restored (name/description/keywords/
    exclusive/expert_rules): `parent`/`lineage` describe where the skill came
    from, which no past revision of its own body can change.
    """
    with _WRITE_LOCK:
        return _restore_version_locked(skill_key, version, domain, base)


def _restore_version_locked(skill_key: str, version: str, domain: str,
                            base: Optional[Dict[str, Skill]]) -> Optional[str]:
    ensure_skills_file(domain)
    local = load_all_skills(domain)
    current = local.get(skill_key)
    if not current:
        return None
    snapshot = next(
        (h for h in current.version_history if str(h.get("version")) == str(version)), None)
    if not snapshot:
        return None
    restored = Skill(
        name=snapshot.get("name") or current.name,
        description=snapshot.get("description", ""),
        keywords=list(snapshot.get("keywords") or []),
        exclusive=list(snapshot.get("exclusive") or []),
        tat_path=current.tat_path,
        expert_rules=snapshot.get("expert_rules", ""),
        parent=current.parent,
        lineage=list(current.lineage),
    )
    return save_skill(skill_key, restored, domain=domain, base=base)


def delete_skill(skill_key: str, domain: str = "wifi") -> bool:
    """Deletes from the LOCAL file for the given domain only. If `skill_key`
    actually came from the shared baseline/contribution (not present
    locally), there is nothing to delete — this app never touches the
    shared drive.

    Under _WRITE_LOCK for the same reason as save_skill: it is a
    read-modify-write of the same file."""
    with _WRITE_LOCK:
        ensure_skills_file(domain)
        # Goes through the strict local loader rather than a bare
        # yaml.safe_load: a delete rewrites the WHOLE file, so it has to
        # refuse on an unparseable one exactly like a save does.
        local_raw = {k: _skill_to_raw(v) for k, v in load_all_skills(domain).items()}
        if skill_key not in local_raw:
            return False
        del local_raw[skill_key]
        _write_skills_yaml(local_raw, domain)
        return True
