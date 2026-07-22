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
import subprocess
import webbrowser


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


def open_in_chrome(url: str) -> None:
    """Open `url` in its own independent Chrome window (not a new tab tacked
    onto whatever Chrome window the user already has focused), falling back
    to the OS default browser if Chrome isn't installed."""
    chrome_path = get_chrome_binary_path()
    if chrome_path:
        try:
            subprocess.Popen([chrome_path, "--new-window", url])
            return
        except Exception as e:
            print(f"⚠️  Could not launch Chrome directly ({e}); falling back to default browser.")
    webbrowser.open(url)
