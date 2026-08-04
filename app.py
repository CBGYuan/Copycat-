import os
import sys
import threading
import time

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

# Last time templates/base.html's keepalive ping hit /__heartbeat, and how
# long the watchdog thread (started only under __main__, see below) waits
# without one before deciding the browser window was closed. A plain list
# (not a bare float) so the route closure can mutate it without `global`.
_last_heartbeat = [time.time()]
_HEARTBEAT_TIMEOUT_SEC = 12


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("LOG_TRIAGE_SECRET", "dev-secret-change-me")

    set_up()

    app.register_blueprint(main_bp)
    app.register_blueprint(log_viewer_bp)
    app.register_blueprint(chatbot_bp)
    app.register_blueprint(learning_bp)
    app.register_blueprint(skills_bp)

    # Every loaded page pings this every few seconds (see templates/base.html).
    # Harmless when nothing is watching it (e.g. the test-client Flask app
    # built by tests/*.py) — the watchdog thread that actually acts on a
    # missed ping is only started below, under `if __name__ == "__main__"`.
    @app.route("/__heartbeat", methods=["POST"])
    def _heartbeat():
        _last_heartbeat[0] = time.time()
        return ("", 204)

    return app


if __name__ == "__main__":
    import signal

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
    _last_heartbeat[0] = time.time()

    _shutting_down = threading.Event()

    def _shutdown(reason: str) -> None:
        # Guards against the watchdog thread and a signal handler both firing
        # (e.g. Ctrl+C arrives right as the heartbeat times out).
        if _shutting_down.is_set():
            return
        _shutting_down.set()
        print(f"🛑 {reason} — shutting down.")
        launch_timer.cancel()
        browser_window.close()
        # os._exit, not sys.exit: Werkzeug's dev server threads (one per
        # in-flight request, e.g. a chatbot stream) are not daemonized by
        # older Werkzeug and would otherwise keep the process alive after
        # app.run() unwinds — the original "Ctrl+C doesn't fully close it"
        # symptom. os._exit terminates immediately, no thread join needed.
        os._exit(0)

    # A plain KeyboardInterrupt can get lost while the main thread is
    # blocked inside Werkzeug's blocking accept()/select() loop on Windows,
    # which is why Ctrl+C sometimes did nothing. Installing explicit handlers
    # makes SIGINT (Ctrl+C) and SIGBREAK (console close / Ctrl+Break on
    # Windows) force an immediate, deterministic shutdown instead.
    signal.signal(signal.SIGINT, lambda signum, frame: _shutdown("Ctrl+C received"))
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda signum, frame: _shutdown("Console closing"))

    def _watch_heartbeat() -> None:
        """Closing the browser window (its X button, or the tab) stops
        templates/base.html's periodic /__heartbeat ping. Once one's been
        missing for _HEARTBEAT_TIMEOUT_SEC, treat the app as abandoned and
        exit — nothing else was watching for that before, so the server
        process just sat there running with no visible window."""
        while not _shutting_down.is_set():
            time.sleep(2)
            if time.time() - _last_heartbeat[0] > _HEARTBEAT_TIMEOUT_SEC:
                _shutdown("Browser window closed (no heartbeat)")
                return

    threading.Thread(target=_watch_heartbeat, daemon=True).start()

    # use_reloader=False: avoids double-initialising set_up() (and re-opening
    # the browser tab) and keeps the native tkinter file-picker threads
    # (used for log/.tat selection) stable.
    try:
        app.run(host=HOST, port=port, debug=True, use_reloader=False, threaded=True)
    finally:
        _shutdown("Server loop exited")
