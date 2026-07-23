import os

from configs import path_configs
from configs.global_configs import app_config
from services.llm_service import LLM_helper
from services import skill_service
from utils import helpers


def _resolve_key_path():
    for p in (path_configs.KEY_PATH_local, path_configs.KEY_PATH_prim, path_configs.KEY_PATH_bkup):
        if p and os.path.exists(p):
            return p
    return None


def set_up():
    app_config.set_project_root(path_configs.PROJECT_ROOT)

    llm_helper = LLM_helper()
    key_path = _resolve_key_path()
    if key_path:
        try:
            key = helpers.load_module(key_path, "key_module")
            model = getattr(key, "gnaigpt_model", "claude-4-6-sonnet")
            llm_helper.set_up(key.gnaigpt_token, key.gnaigpt_url, model)
            print(f"✅ LLM configured from {key_path} (model={model})")
        except Exception as e:
            print(f"⚠️  Failed to load key module from {key_path}: {e}")
    else:
        print(
            "⚠️  No keys.py found (checked configs/keys_local.py + the two corp "
            "shares). LLM features will be unavailable until one becomes reachable. "
            "See README.md → 'Set up LLM'."
        )
    app_config.set_llm_helper(llm_helper)

    # Refresh the local read-only mirror of the shared corp drive
    # (data/skills/shared_cache/) from the live share before loading anything
    # — see skill_service.refresh_shared_cache(). Best-effort: if the share
    # is unreachable (VPN down), this is a silent no-op and load_shared_
    # skills() below just reads whatever was cached from the last successful
    # startup, instead of the WiFi/BT pools going empty for that source.
    cache_refresh = skill_service.refresh_shared_cache()
    if not any(cache_refresh.values()):
        print("⚠️  Could not reach the shared skill drive this startup — "
              "using the last locally-cached copy (data/skills/shared_cache/).")

    # Load the skill knowledge base: shared corp baseline + this engineer's
    # own shared contribution (both from the local mirror just refreshed
    # above) + local edits (WiFi), and the shared Bluetooth baseline + its
    # own local edits (BT) — see services/skill_service.load_shared_skills().
    # It creates the local edit files itself, so no separate
    # ensure_skills_file() call is needed here.
    loaded = skill_service.load_shared_skills()
    app_config.set_skills(loaded["wifi"])
    app_config.set_bt_skills(loaded["bt"])
    src = loaded["sources"]
    print(f"📌 Loaded {len(app_config.skills)} WiFi skill(s) "
          f"[shared={'y' if src['shared_wifi'] else 'n'} "
          f"contribution={'y' if src['contribution'] else 'n'} "
          f"local={'y' if src['local'] else 'n'}], "
          f"{len(app_config.bt_skills)} BT skill(s) "
          f"[shared={'y' if src['bt'] else 'n'} "
          f"local={'y' if src['local_bt'] else 'n'}]")
