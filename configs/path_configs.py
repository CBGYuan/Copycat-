import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SKILLS_DIR = os.path.join(DATA_DIR, "skills")
SKILLS_YAML_PATH = os.path.join(SKILLS_DIR, "skills.yaml")
# Local BT counterpart to SKILLS_YAML_PATH above — WITHOUT this, any
# BT-domain skill learned/edited through the UI used to get written into the
# WiFi-only file above and then silently show up merged into the *WiFi* pool
# (see skill_service.save_skill's `domain` param), which is exactly the
# WiFi/BT architecture mismatch this file exists to prevent.
SKILLS_BT_YAML_PATH = os.path.join(SKILLS_DIR, "bt_skills.yaml")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

# ---- shared skill knowledge base (same corp share as the key.py backup path
# above) — read-only inputs merged into app_config at startup. Confirmed
# layout on \\infs089...: skills.yaml (WiFi baseline), bt_skills.yaml
# (Bluetooth), user_contributions\<username>__skills_<date>.yaml (personal
# overrides, one file per engineer). We only ever READ from here — anything
# saved/edited through the UI goes to SKILLS_YAML_PATH above, never back to
# this shared drive.
SKILLS_SHARE_DIR = r"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data\skills_config"
SKILLS_SHARE_WIFI_PATH = os.path.join(SKILLS_SHARE_DIR, "skills.yaml")
SKILLS_SHARE_BT_PATH = os.path.join(SKILLS_SHARE_DIR, "bt_skills.yaml")
SKILLS_SHARE_USER_CONTRIB_DIR = os.path.join(SKILLS_SHARE_DIR, "user_contributions")
