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

from configs import path_configs
from configs.set_up_app import set_up
from blueprints import main_bp, log_viewer_bp, chatbot_bp, learning_bp, skills_bp
from utils import helpers
from utils.browser_utils import ManagedChromeWindow

HOST = "127.0.0.1"

def create_app():
    # Explicit folders: in a frozen build Flask would otherwise resolve them
    # relative to the exe rather than the unpacked bundle, and find neither.
    app = Flask(
        __name__,
        template_folder=os.path.join(path_configs.BUNDLE_ROOT, "templates"),
        static_folder=os.path.join(path_configs.BUNDLE_ROOT, "static"),
    )
    app.secret_key = os.environ.get("LOG_TRIAGE_SECRET", "dev-secret-change-me")

    set_up()

    app.register_blueprint(main_bp)
    app.register_blueprint(log_viewer_bp)
    app.register_blueprint(chatbot_bp)
    app.register_blueprint(learning_bp)
    app.register_blueprint(skills_bp)

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
    _shutting_down = threading.Event()

    def _shutdown(reason: str) -> None:
        # Guards against two explicit shutdown signals arriving together.
        if _shutting_down.is_set():
            return
        _shutting_down.set()
        print(f"🛑 {reason} — shutting down.")
        browser_window.close()
        # os._exit, not sys.exit: Werkzeug's dev server threads (one per
        # in-flight request, e.g. a chatbot stream) are not daemonized by
        # older Werkzeug and would otherwise keep the process alive after
        # app.run() unwinds — the original "Ctrl+C doesn't fully close it"
        # symptom. os._exit terminates immediately, no thread join needed.
        os._exit(0)

    def _open_then_watch(url: str) -> None:
        # Small delay so the server is accepting connections before Chrome
        # requests the page.
        time.sleep(1.2)
        if not browser_window.open(url):
            print("ℹ️  No app window to watch — press Ctrl+C here to stop the server.")
            return
        # Closing the app window is the natural "I'm done" gesture, so treat
        # the managed Chrome process exiting as a shutdown request — otherwise
        # the Flask server keeps running in the terminal with no UI attached.
        browser_window.wait_for_exit()
        _shutdown("App window closed")

    threading.Thread(
        target=_open_then_watch, args=(f"http://{HOST}:{port}/",), daemon=True
    ).start()

    # A plain KeyboardInterrupt can get lost while the main thread is
    # blocked inside Werkzeug's blocking accept()/select() loop on Windows,
    # which is why Ctrl+C sometimes did nothing. Installing explicit handlers
    # makes SIGINT (Ctrl+C) and SIGBREAK (console close / Ctrl+Break on
    # Windows) force an immediate, deterministic shutdown instead.
    signal.signal(signal.SIGINT, lambda signum, frame: _shutdown("Ctrl+C received"))
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda signum, frame: _shutdown("Console closing"))

    # use_reloader=False: avoids double-initialising set_up() (and re-opening
    # the browser tab) and keeps the native tkinter file-picker threads
    # (used for log/.tat selection) stable.
    # debug=False: the interactive Werkzeug debugger exposes source, locals and
    # a code console on any unhandled exception.
    try:
        app.run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True)
    finally:
        _shutdown("Server loop exited")
