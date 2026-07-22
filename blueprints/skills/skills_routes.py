from flask import Blueprint, render_template, request, jsonify

from configs.global_configs import app_config
from services import skill_service

skills_bp = Blueprint("skills", __name__, url_prefix="/skills")


def _all_skills():
    """WiFi ∪ BT, for the library view — editing/deleting still only ever
    touches the local file for that skill's own domain (see
    skill_service.save_skill/delete_skill's `domain` param)."""
    merged = dict(app_config.bt_skills)
    merged.update(app_config.skills)  # WiFi wins on any (unlikely) key clash
    return merged


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
    saved_key = skill_service.save_skill(skill_key, skill, domain=domain, base=base_pool)
    loaded = skill_service.load_shared_skills()
    app_config.set_skills(loaded["wifi"])
    app_config.set_bt_skills(loaded["bt"])
    return jsonify({"success": True, "skill_key": saved_key})


@skills_bp.route("/delete/<skill_key>", methods=["POST"])
def delete_skill_route(skill_key):
    domain = (request.args.get("domain") or _domain_of(skill_key)).lower()
    ok = skill_service.delete_skill(skill_key, domain=domain)
    if ok:
        loaded = skill_service.load_shared_skills()
        app_config.set_skills(loaded["wifi"])
        app_config.set_bt_skills(loaded["bt"])
    return jsonify({"success": ok})
