from flask import Blueprint, redirect, url_for, jsonify

from configs.global_configs import app_config

main_bp = Blueprint("main", __name__, url_prefix="/")


@main_bp.route("/")
def index():
    # No separate landing page — Log Viewer (log + filters + chat) is the
    # only screen anyone actually uses, so go straight there.
    return redirect(url_for("log_viewer.index"))


@main_bp.route("/llm_status")
def llm_status():
    """Polled by the "LLM is not configured yet" banner (see log_viewer.html)
    so it can clear itself once configs.set_up_app's background thread
    finishes reading key.py, instead of needing a manual page reload — LLM
    setup is no longer done synchronously at startup (see set_up_app.
    _configure_llm), so a page loaded in the first few seconds can catch
    llm_helper still mid-configure."""
    ready = bool(app_config.llm_helper and app_config.llm_helper.is_ready)
    return jsonify({"ready": ready})
