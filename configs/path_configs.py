import os
import sys

# Frozen (PyInstaller) builds have TWO roots, and conflating them silently
# destroys the engineer's own skills: read-only bundled assets are unpacked
# into a temp dir that is deleted the moment the exe exits, so anything
# written there is gone at the next launch. BUNDLE_ROOT is that temp dir;
# PROJECT_ROOT is the folder the exe actually sits in, and is the only place
# this app may write to.
_FROZEN = getattr(sys, "frozen", False)
_SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BUNDLE_ROOT = getattr(sys, "_MEIPASS", _SOURCE_ROOT) if _FROZEN else _SOURCE_ROOT
PROJECT_ROOT = os.path.dirname(sys.executable) if _FROZEN else _SOURCE_ROOT

# ---- LLM key locations (checked in order, first existing wins) ----
# 1) local override for offline/dev testing (gitignored) — create it by hand
#    with the same schema as your key.py: gnaigpt_token / gnaigpt_url /
#    gnaigpt_model. Only needed when the corp shares below are unreachable.
KEY_PATH_local = os.path.join(PROJECT_ROOT, "configs", "keys_local.py")
# 2) primary corp share (same as wireless_ce_avatar/IntelAvatar's KEY_PATH_prim)
KEY_PATH_prim = r"\\pgsfls0101.gar.corp.intel.com\symstore\CMAttachments\JIRA\WIFI\Temp\KJ\Intel_WirelessCE_Avatar\key\keys.py"
# 3) backup corp share (same as wireless_ce_avatar/IntelAvatar's KEY_PATH_bkup)
KEY_PATH_bkup = r"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\key\keys.py"

# ---- local data dirs (skill knowledge base, session cache) ----
# data/skills/ splits into two clearly separate folders so it's obvious on
# disk which is which — never mixed into one file the way the original
# single skills.yaml/bt_skills.yaml pair used to be:
#
#   local/          — THIS engineer's own copycat-originated skills. The
#                      only files this app ever WRITES to (skill_service.
#                      save_skill/delete_skill). Genuinely owned content —
#                      commit it to git like any other work product.
#   shared_cache/    — a READ-ONLY local mirror of the shared corp drive
#                      (skills_config/), refreshed automatically on every
#                      app startup (see skill_service.refresh_shared_cache,
#                      called from set_up_app.set_up). Never hand-edited,
#                      never written to by save_skill/delete_skill. If the
#                      shared drive is unreachable at startup (VPN down),
#                      the refresh is a no-op and the app falls back to
#                      whatever was cached from the last successful sync —
#                      shared skills don't just vanish because the network
#                      dropped mid-session. Regenerated content — gitignored.
#
# The data folder stays BESIDE THE RUNNING EXE. That is the only location the
# engineer ever chose, so it is the only one this app creates: writing to a
# fixed per-user folder instead would put their skills somewhere they never
# opened, and would move with the Windows account rather than with the copy of
# Copycat they are actually using (a "Run as administrator" launch is a
# different account, and a different %LOCALAPPDATA%).
#
# The cost is that a SECOND copy of Copycat is a second, EMPTY knowledge base,
# with nothing on screen to say so — an engineer ran the Downloads copy, moved
# the folder to C:\BT-work, and every skill they had exported stayed behind.
# USER_STATE_DIR is how that becomes noticeable: each launch records the data
# folder it used, so a copy that starts out empty can point at the folders
# this user has genuinely run before. Only that small registry lives there —
# never a skill (see services.data_location).
DATA_DIR = os.environ.get("COPYCAT_DATA_DIR", "").strip() or os.path.join(PROJECT_ROOT, "data")
USER_STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".copycat"),
    "Copycat",
)
DATA_LOCATIONS_PATH = os.path.join(USER_STATE_DIR, "data-locations.json")

SKILLS_DIR = os.path.join(DATA_DIR, "skills")

SKILLS_LOCAL_DIR = os.path.join(SKILLS_DIR, "local")
SKILLS_YAML_PATH = os.path.join(SKILLS_LOCAL_DIR, "skills.yaml")
# Local BT counterpart to SKILLS_YAML_PATH above — WITHOUT this, any
# BT-domain skill learned/edited through the UI used to get written into the
# WiFi-only file above and then silently show up merged into the *WiFi* pool
# (see skill_service.save_skill's `domain` param), which is exactly the
# WiFi/BT architecture mismatch this file exists to prevent.
SKILLS_BT_YAML_PATH = os.path.join(SKILLS_LOCAL_DIR, "bt_skills.yaml")

# ---- shared skill knowledge base — the actual live corp share (same share
# as the key.py backup path above). Confirmed layout on \\infs089...:
# skills.yaml (WiFi baseline), bt_skills.yaml (Bluetooth),
# user_contributions\<username>__skills_<date>.yaml (personal overrides, one
# file per engineer). We only ever READ from here — nothing saved/edited
# through the UI is ever written back to this share; see SKILLS_CACHE_*
# below for the local mirror everything else in this app actually reads.
SKILLS_SHARE_DIR = r"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data\skills_config"
SKILLS_SHARE_WIFI_PATH = os.path.join(SKILLS_SHARE_DIR, "skills.yaml")
SKILLS_SHARE_BT_PATH = os.path.join(SKILLS_SHARE_DIR, "bt_skills.yaml")
SKILLS_SHARE_USER_CONTRIB_DIR = os.path.join(SKILLS_SHARE_DIR, "user_contributions")

# ---- local mirror of the shared drive above (see the module comment on
# SKILLS_LOCAL_DIR) — this is what load_shared_skills() actually reads for
# the "shared_wifi"/"bt"/"contribution" sources; refresh_shared_cache() is
# what keeps it in sync with the live share on every app startup.
SKILLS_CACHE_DIR = os.path.join(SKILLS_DIR, "shared_cache")
SKILLS_CACHE_WIFI_PATH = os.path.join(SKILLS_CACHE_DIR, "skills.yaml")
SKILLS_CACHE_BT_PATH = os.path.join(SKILLS_CACHE_DIR, "bt_skills.yaml")
SKILLS_CACHE_USER_CONTRIB_DIR = os.path.join(SKILLS_CACHE_DIR, "user_contributions")
