import os
import sys
import threading

# Windows consoles often default to a legacy codepage (cp1252/cp950/...)
# that can't encode the emoji used in this app's startup log lines
# (configs/set_up_app.py, utils/browser_utils.py, services/skill_service.py),
# which crashes create_app() before the server even starts. Force UTF-8 on
# stdout/stderr up front so those prints always succeed.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask

from configs.set_up_app import set_up
from blueprints import main_bp, log_viewer_bp, chatbot_bp, learning_bp, skills_bp
from utils import helpers
from utils.browser_utils import ManagedChromeWindow

HOST = "127.0.0.1"


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("LOG_TRIAGE_SECRET", "dev-secret-change-me")

    set_up()

    app.register_blueprint(main_bp)
    app.register_blueprint(log_viewer_bp)
    app.register_blueprint(chatbot_bp)
    app.register_blueprint(learning_bp)
    app.register_blueprint(skills_bp)

    return app


if __name__ == "__main__":
    app = create_app()
    # A fresh random port every launch (same approach as IntelAvatar's
    # DriverManager.run_driver -> get_available_port) instead of a hardcoded
    # one — this app used to always bind :5000, which meant a leftover
    # process from an earlier run silently kept holding it, and there was no
    # way to tell which process a browser tab was actually talking to. Each
    # launch now gets its own independent port, so it can never collide with
    # a still-running previous instance.
    port = helpers.get_available_port()
    print(f"🌐 Starting on port {port}")
    # Pop the UI open in Chrome shortly after the server starts (mirrors
    # IntelAvatar's own auto-launch-Chrome-on-startup behavior). This managed
    # window uses its own temporary Chrome process, allowing Ctrl+C to close
    # exactly this page without touching any pre-existing Chrome windows.
    browser_window = ManagedChromeWindow()
    launch_timer = threading.Timer(
        1.2, browser_window.open, args=(f"http://{HOST}:{port}/",)
    )
    launch_timer.daemon = True
    launch_timer.start()
    # use_reloader=False: avoids double-initialising set_up() (and re-opening
    # the browser tab) and keeps the native tkinter file-picker threads
    # (used for log/.tat selection) stable.
    try:
        app.run(host=HOST, port=port, debug=True, use_reloader=False, threaded=True)
    finally:
        launch_timer.cancel()
        browser_window.close()
