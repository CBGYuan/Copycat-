# PyInstaller build spec — `pyinstaller copycat.spec`
#
# One-file console build. Console, not windowed: the startup log (chosen port,
# whether the LLM key resolved, whether the shared skill drive was reachable)
# is the only diagnostic an engineer gets when something is misconfigured, and
# a windowed build has nowhere to print it.
#
# Bundled: templates/ + static/ only. data/skills/ is deliberately NOT
# bundled — it is per-engineer content written at runtime, and it lives next
# to the exe (see configs/path_configs.PROJECT_ROOT), not in the temp dir this
# bundle unpacks into.

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    # Imported lazily inside services/event_log_service.py, so the analysis
    # pass can't see it.
    "win32evtlog",
    "win32evtlogutil",
]
hiddenimports += collect_submodules("openai")
hiddenimports += collect_submodules("anthropic")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("templates", "templates"),
        ("static", "static"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "_pytest", "PIL", "numpy", "matplotlib"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Copycat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
