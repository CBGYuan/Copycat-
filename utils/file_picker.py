"""
Native Windows file/folder pickers.

Used because the engineer's machine can't reach the shared TAT filter folder
(\\infs089.iil.intel.com\\...\\log_parser_data\\filter), so log files and .tat
filter files must be picked manually from the UI instead of being resolved
from a shared path. Same threading pattern as wireless_ce_avatar/IntelAvatar's
blueprints/*/browse() routes (Tk must be created off the Flask request thread).
"""
import sys
import threading
import tkinter as tk
from tkinter import filedialog


def _enable_dpi_awareness() -> None:
    """Without this, a Python process is DPI-unaware by default, so on a
    HiDPI display Windows bitmap-stretches the whole native file dialog to
    fit — that's the blurriness. Must be set once, before any window (Tk or
    native) is created in this process; safe to call more than once."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_dpi_awareness()


def _run_dialog(title: str, filetypes: list, result: dict):
    root = tk.Tk()
    root.wm_attributes("-topmost", True)
    root.withdraw()
    # Without an explicit parent, the dialog isn't reliably transient to
    # `root`'s topmost/stacking state on Windows and can open BEHIND the
    # browser window — looking like nothing happened while a hidden dialog
    # is actually waiting. lift()+focus_force() (plus passing parent=root
    # below) forces it to the front.
    root.lift()
    root.focus_force()
    path = filedialog.askopenfilename(parent=root, title=title, filetypes=filetypes)
    root.destroy()
    result["path"] = path or ""


def pick_file(title: str, filetypes: list, timeout: int = 120) -> str:
    result = {"path": ""}
    t = threading.Thread(target=_run_dialog, args=(title, filetypes, result))
    t.start()
    t.join(timeout=timeout)
    return result["path"]


def pick_log_file() -> str:
    return pick_file(
        "Select log file",
        [("Log files", "*.log *.txt *.hci.txt"), ("All files", "*.*")],
    )


def pick_tat_file() -> str:
    return pick_file(
        "Select TAT filter file",
        [("TAT filter files", "*.tat"), ("All files", "*.*")],
    )


def pick_event_log_file() -> str:
    return pick_file(
        "Select System Event Log (.evtx / .evt)",
        [("Windows event logs", "*.evtx *.evt"), ("All files", "*.*")],
    )
