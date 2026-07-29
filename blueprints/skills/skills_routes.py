from flask import Blueprint, render_template, request, jsonify

from configs.global_configs import app_config
from services import session_store, skill_service
from utils import skill_dedup

skills_bp = Blueprint("skills", __name__, url_prefix="/skills")


def _all_skills():
    """WiFi ∪ BT, for the library view — editing/deleting still only ever
    touches the local file for that skill's own domain (see
    skill_service.save_skill/delete_skill's `domain` param)."""
    merged = dict(app_config.bt_skills)
    merged.update(app_config.skills)  # WiFi wins on any (unlikely) key clash
    return merged


def _store_error(e: skill_service.SkillStoreError):
    """The local skills file could not be read, so nothing was written. Say
    that plainly (and point at the backup) rather than returning a 500 the
    engineer would read as "the save probably went through"."""
    return jsonify({"success": False, "message": str(e)}), 409


def _domain_of(skill_key: str) -> str:
    """Which pool a skill_key currently belongs to — WiFi wins on a clash,
    matching _all_skills() above."""
    if skill_key and skill_key in app_config.skills:
        return "wifi"
    if skill_key and skill_key in app_config.bt_skills:
        return "bt"
    return "wifi"


@skills_bp.route("/")
def index():
    return render_template("skills.html", skills=_all_skills(), bt_keys=list(app_config.bt_skills.keys()))


@skills_bp.route("/graph")
def graph():
    """Everything the Skill Library page needs to draw one domain's skills as
    a lineage forest, in a single request.

    Each node reports where it can be edited (`origin`), how deep its ancestry
    runs, and whether it is the skill currently LOADED into the workbench.
    `parent` is only treated as an edge when the parent is actually present in
    this same pool — a skill whose parent was deleted, or lives in the other
    domain, still has to appear somewhere, so it is rendered as a root with
    `orphan_parent` recording the dangling reference rather than being dropped
    from the view entirely.
    """
    domain = (request.args.get("domain") or "wifi").lower()
    domain = "bt" if domain == "bt" else "wifi"
    pool = app_config.bt_skills if domain == "bt" else app_config.skills
    origins = skill_service.skill_origins(domain)
    active_key = session_store.get_state().active_skill_key or ""

    nodes = []
    for key, sk in pool.items():
        parent = sk.parent if (sk.parent and sk.parent in pool) else None
        nodes.append({
            "key": key,
            "name": sk.name,
            "description": sk.description,
            "origin": origins.get(key, "local"),
            "parent": parent,
            "orphan_parent": sk.parent if (sk.parent and not parent) else None,
            "lineage": list(sk.lineage),
            "version": sk.version,
            "history_count": len(sk.version_history),
            "keyword_count": len(sk.keywords),
            "exclusive_count": len(sk.exclusive),
            # Same itemizer the dedup uses, so "12 rules" here and "12 rules
            # inherited" in an export summary always count the same things.
            "rule_count": len(skill_dedup.split_rules(sk.expert_rules)),
            "is_active": key == active_key,
        })
    nodes.sort(key=lambda n: n["name"].lower())
    return jsonify({"success": True, "domain": domain, "active_key": active_key, "nodes": nodes})


@skills_bp.route("/versions/<skill_key>")
def versions(skill_key):
    """The skill's own revision trail, newest first, with the live state as
    entry 0. Each entry carries its full body so the page can show any past
    revision (and diff it against current) without another round-trip.

    `restorable` is per-entry rather than global: a shared/contribution skill's
    history is readable but not rewritable from here — this app never writes to
    the corp drive (see skill_service's module docstring)."""
    skill = _all_skills().get(skill_key)
    if not skill:
        return jsonify({"success": False, "message": "Not found"}), 404
    domain = _domain_of(skill_key)
    is_local = skill_service.skill_origins(domain).get(skill_key) == "local"

    entries = [{
        "version": skill.version,
        "name": skill.name,
        "description": skill.description,
        "keywords": list(skill.keywords),
        "exclusive": list(skill.exclusive),
        "expert_rules": skill.expert_rules,
        "saved_at": None,
        "is_current": True,
        "restorable": False,
    }]
    for snap in reversed(skill.version_history):
        entries.append({
            "version": str(snap.get("version") or "?"),
            "name": snap.get("name") or skill.name,
            "description": snap.get("description", ""),
            "keywords": list(snap.get("keywords") or []),
            "exclusive": list(snap.get("exclusive") or []),
            "expert_rules": snap.get("expert_rules", ""),
            "saved_at": snap.get("saved_at"),
            "is_current": False,
            "restorable": is_local,
        })
    return jsonify({"success": True, "skill_key": skill_key, "domain": domain,
                    "origin": skill_service.skill_origins(domain).get(skill_key, "local"),
                    "parent": skill.parent, "lineage": list(skill.lineage),
                    "versions": entries})


@skills_bp.route("/restore/<skill_key>", methods=["POST"])
def restore_route(skill_key):
    data = request.get_json(silent=True) or {}
    version = str(data.get("version") or "")
    domain = (data.get("domain") or _domain_of(skill_key)).lower()
    base_pool = app_config.bt_skills if domain == "bt" else app_config.skills
    try:
        saved = skill_service.restore_version(skill_key, version, domain=domain, base=base_pool)
    except skill_service.SkillStoreError as e:
        return _store_error(e)
    if not saved:
        return jsonify({"success": False,
                        "message": "Only locally-saved skills can be restored, "
                                   "and only to a version in their own history."}), 400
    loaded = skill_service.load_shared_skills()
    app_config.set_skills(loaded["wifi"])
    app_config.set_bt_skills(loaded["bt"])
    return jsonify({"success": True, "skill_key": saved})


@skills_bp.route("/clear_baseline", methods=["POST"])
def clear_baseline():
    """Drop the export baseline without touching the filters on screen — the
    mirror image of activate() below.

    A separate endpoint rather than activate() with an empty key: expressing
    "no key" as a `defaults={"skill_key": ""}` rule builds fine on some
    Werkzeug versions and raises BuildError on others (which is exactly how
    this page started 500-ing). An explicit path has no such ambiguity.
    """
    session_store.get_state().active_skill_key = ""
    return jsonify({"success": True, "active_key": "", "active_name": ""})


@skills_bp.route("/activate/<skill_key>", methods=["POST"])
def activate(skill_key):
    """Mark a skill as the LOADED skill for this session — the same slot
    log_viewer's skill dropdown sets. Doing it from the library only records
    the CHOICE (which skill the next Export inherits from, and which one the
    workbench opens with); it deliberately does not touch the current filter
    set, because the log viewer's own load_skill is what owns replacing what
    is on screen."""
    if skill_key and not (app_config.skills.get(skill_key) or app_config.bt_skills.get(skill_key)):
        return jsonify({"success": False, "message": "Skill not found"}), 404
    state = session_store.get_state()
    state.active_skill_key = skill_key or ""
    skill = (app_config.skills.get(state.active_skill_key)
             or app_config.bt_skills.get(state.active_skill_key))
    return jsonify({"success": True, "active_key": state.active_skill_key,
                    "active_name": skill.name if skill else ""})


@skills_bp.route("/get/<skill_key>")
def get_skill(skill_key):
    skill = _all_skills().get(skill_key)
    if not skill:
        return jsonify({"success": False, "message": "Not found"}), 404
    payload = skill.model_dump()
    payload["domain"] = _domain_of(skill_key)
    return jsonify({"success": True, "skill": payload})


@skills_bp.route("/list")
def list_skills():
    """Used by the Log Viewer's skill dropdown to switch between the WiFi and
    Bluetooth skill sets after a log is picked and its domain auto-detected."""
    domain = (request.args.get("domain") or "wifi").lower()
    pool = app_config.bt_skills if domain == "bt" else app_config.skills
    items = [{"key": key, "name": sk.name} for key, sk in pool.items()]
    return jsonify({"success": True, "domain": domain, "skills": items})


@skills_bp.route("/save", methods=["POST"])
def save_skill_route():
    data = request.get_json(silent=True) or {}
    try:
        skill = skill_service.Skill(
            name=data.get("name", ""),
            description=data.get("description", ""),
            keywords=data.get("keywords") or [],
            exclusive=data.get("exclusive") or [],
            tat_path=data.get("tat_path") or None,
            expert_rules=data.get("expert_rules", ""),
            # Carried through from whatever opened the editor (an extension
            # draft from /learning/converge, or an existing skill being
            # re-edited). Without this the ancestry would be silently erased by
            # the first trip through the modal, and a skill built on a loaded
            # parent would come out looking like an unrelated standalone one.
            parent=data.get("parent") or None,
            lineage=[str(a) for a in (data.get("lineage") or []) if str(a).strip()],
        )
    except Exception as e:
        return jsonify({"success": False, "message": f"Invalid skill: {e}"}), 400

    skill_key = data.get("skill_key") or None
    # Trust an explicit `domain` from the editor (set when it opened an
    # existing skill); fall back to looking up which pool that key is
    # currently in, or "wifi" for a brand-new skill.
    domain = (data.get("domain") or _domain_of(skill_key)).lower()
    # Snapshot the FULL currently-loaded merged view (shared baseline +
    # contribution + previous local edits) into the local file, not just
    # this one skill — see skill_service.save_skill's docstring.
    base_pool = app_config.bt_skills if domain == "bt" else app_config.skills
    try:
        saved_key = skill_service.save_skill(skill_key, skill, domain=domain, base=base_pool)
    except skill_service.SkillStoreError as e:
        return _store_error(e)
    loaded = skill_service.load_shared_skills()
    app_config.set_skills(loaded["wifi"])
    app_config.set_bt_skills(loaded["bt"])
    return jsonify({"success": True, "skill_key": saved_key})


@skills_bp.route("/delete/<skill_key>", methods=["POST"])
def delete_skill_route(skill_key):
    domain = (request.args.get("domain") or _domain_of(skill_key)).lower()
    try:
        ok = skill_service.delete_skill(skill_key, domain=domain)
    except skill_service.SkillStoreError as e:
        return _store_error(e)
    if ok:
        loaded = skill_service.load_shared_skills()
        app_config.set_skills(loaded["wifi"])
        app_config.set_bt_skills(loaded["bt"])
    return jsonify({"success": ok})
