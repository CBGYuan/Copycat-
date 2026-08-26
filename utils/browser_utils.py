"""
Auto-launch the local Flask UI in Google Chrome on startup — mirrors
wireless_ce_avatar/IntelAvatar's DriverManager.get_chrome_binary_path(), but
launches the installed Chrome directly with --new-window instead of Avatar's
full Selenium/ChromeDriver setup (which needs a chromedriver binary matched
to the installed Chrome version, downloaded through Intel's proxy — real
network dependencies this trimmed-down app doesn't need just to pop a
window). `webbrowser.get(...).open()` was tried first, but when Chrome is
already running it hands the URL to that existing process as a new TAB,
which is exactly the "stuck in an existing Chrome tab" behavior we don't
want — so we invoke chrome.exe with --new-window via subprocess directly,
which forces a separate top-level window even if Chrome is already open.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser


def _safe_print(message: str) -> None:
    """print() that never raises on a legacy Windows console codepage (e.g.
    cp1252) that can't encode the emoji in these status lines.

    app.py reconfigures stdout/stderr to UTF-8 once, at the top, before any
    other import runs -- but only for the app's own `python app.py` entry
    point. This module is also imported directly (unit tests import
    ManagedChromeWindow without ever importing app.py, and so would any other
    script that reuses it), where that reconfiguration never happens. A
    status print should not depend on which entry point got there first.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(enc, errors="replace").decode(enc, errors="replace"))


def get_chrome_binary_path():
    """Locate the installed Chrome executable (Windows registry first, then
    common install locations) so we can force-open Chrome specifically
    instead of whatever the OS default browser happens to be."""
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                ) as k:
                    path, _ = winreg.QueryValueEx(k, None)
                    if path and os.path.exists(path):
                        return path
            except OSError:
                continue
    except Exception:
        pass

    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


# Chrome that is going to run has not exited by now; Chrome that refuses to
# run (see _took_off) is gone in well under a second.
_LIFTOFF_SECONDS = 3.0


class ManagedChromeWindow:
    """Own the Chrome window opened for one local Flask server run.

    Chrome normally forwards ``--new-window`` to an already-running browser
    process. That makes the new window look independent, but leaves us without
    a process handle that can close it when Flask stops. A temporary user-data
    directory forces a genuinely separate Chrome instance, so Ctrl+C can close
    only this app window without touching the engineer's normal Chrome session.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._profile_dir = None
        self._closed = False

    def open(self, url: str) -> bool:
        """Open the app window. Returns whether a managed Chrome was started."""
        with self._lock:
            if self._closed or self._process is not None:
                return False

            chrome_path = get_chrome_binary_path()
            if chrome_path:
                profile_dir = tempfile.mkdtemp(prefix="log-triage-chrome-")
                process = None
                try:
                    process = subprocess.Popen([
                        chrome_path,
                        f"--user-data-dir={profile_dir}",
                        "--new-window",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-sync",
                        "--disable-background-mode",
                        url,
                    ])
                except Exception as e:
                    shutil.rmtree(profile_dir, ignore_errors=True)
                    _safe_print(
                        f"⚠️  Could not launch a managed Chrome window ({e}); "
                        "falling back to the default browser."
                    )

                if process is not None:
                    if self._took_off(process):
                        self._process = process
                        self._profile_dir = profile_dir
                        return True
                    shutil.rmtree(profile_dir, ignore_errors=True)
                    _safe_print(
                        f"⚠️  Chrome started and exited immediately (code "
                        f"{process.returncode}). The usual cause is running "
                        "Copycat as administrator: the child process inherits "
                        "that token and Chrome refuses to run elevated. "
                        "Falling back to the default browser."
                    )

            # There is no portable way to close a tab handed to an arbitrary
            # default browser, so this fallback remains intentionally unmanaged.
            opened = False
            try:
                opened = webbrowser.open(url)
            except Exception:
                opened = False
            if opened:
                _safe_print("⚠️  This fallback browser tab must be closed manually.")
            else:
                _safe_print(
                    "⚠️  No browser could be opened. The app IS running — "
                    f"paste this into a browser yourself: {url}"
                )
            return False

    @staticmethod
    def _took_off(process) -> bool:
        """Did the Chrome we just started actually stay up?

        Popen succeeding only means the process was created. A Chrome that
        refuses its environment -- elevated token, broken profile, missing
        dependency -- is created and then exits at once, and the caller would
        otherwise read that exit as "the engineer closed the window" and shut
        the whole app down before the UI ever appeared.
        """
        try:
            process.wait(timeout=_LIFTOFF_SECONDS)
        except subprocess.TimeoutExpired:
            return True
        return False

    def wait_for_exit(self) -> bool:
        """Block until this app's Chrome instance exits (user closed the window).

        Returns False right away when no managed Chrome is running -- the
        unmanaged fallback tab belongs to the engineer's own browser, so there
        is no process whose lifetime tracks this app's window.
        """
        with self._lock:
            process = self._process
        if process is None:
            return False
        process.wait()
        return True

    def close(self) -> None:
        """Close only this app's Chrome instance and remove its temp profile."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            profile_dir = self._profile_dir
            self._process = None
            self._profile_dir = None

        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
                _safe_print("🛑 App browser window closed.")
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
                _safe_print("🛑 App browser window closed.")
            except Exception as e:
                _safe_print(f"⚠️  Could not close the app Chrome window cleanly ({e}).")

        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)
