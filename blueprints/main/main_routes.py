from flask import Blueprint, redirect, url_for

main_bp = Blueprint("main", __name__, url_prefix="/")


@main_bp.route("/")
def index():
    # No separate landing page — Log Viewer (log + filters + chat) is the
    # only screen anyone actually uses, so go straight there.
    return redirect(url_for("log_viewer.index"))
