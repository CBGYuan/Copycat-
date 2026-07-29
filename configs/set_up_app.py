import os
import threading

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


def _load_skills_into_config(log_prefix: str) -> None:
    """Load the skill knowledge base from whatever's currently in the local
    read-only mirror (data/skills/shared_cache/) — shared corp baseline +
    this engineer's own shared contribution + local edits (WiFi), and the
    shared Bluetooth baseline + its own local edits (BT). See services/
    skill_service.load_shared_skills(). Never touches the network — safe to
    call both before AND after refresh_shared_cache() (see set_up() /
    _refresh_shared_cache_then_reload() below)."""
    loaded = skill_service.load_shared_skills()
    app_config.set_skills(loaded["wifi"])
    app_config.set_bt_skills(loaded["bt"])
    src = loaded["sources"]
    print(f"{log_prefix} Loaded {len(app_config.skills)} WiFi skill(s) "
          f"[shared={'y' if src['shared_wifi'] else 'n'} "
          f"contribution={'y' if src['contribution'] else 'n'} "
          f"local={'y' if src['local'] else 'n'}], "
          f"{len(app_config.bt_skills)} BT skill(s) "
          f"[shared={'y' if src['bt'] else 'n'} "
          f"local={'y' if src['local_bt'] else 'n'}]")


def _refresh_shared_cache_then_reload() -> None:
    """Runs on a background thread (see set_up()) so a slow/unreachable
    corp share (VPN down, laggy SMB mount) can't stall app startup — this is
    blocking network I/O (file-exists checks + reads/writes against the live
    share) with no timeout of its own. skill_service.refresh_shared_cache()
    is already best-effort (a source that's unreachable just leaves the
    local mirror untouched), so the only change here is WHEN it runs: after
    the server is already listening on the fast local-mirror snapshot
    set_up() loaded synchronously, instead of gating first paint on it.
    Re-loads + republishes app_config.skills/bt_skills once the mirror sync
    finishes, so a request that lands after this completes sees the live
    share's content; one that lands before it still sees a valid (if
    possibly stale) skill set rather than an empty one."""
    cache_refresh = skill_service.refresh_shared_cache()
    if not any(cache_refresh.values()):
        print("⚠️  Could not reach the shared skill drive this startup — "
              "still using the locally-cached copy (data/skills/shared_cache/).")
        return
    _load_skills_into_config("🔄")


def _configure_llm(llm_helper: "LLM_helper") -> None:
    """Runs on a background thread (see set_up()) — same reasoning as
    _refresh_shared_cache_then_reload(): _resolve_key_path() and the actual
    key.py read both do blocking, un-timed I/O against a UNC corp share
    (measured ~1.7s + ~2.6s on a warm network path, more when it's slow),
    which used to run before the server ever started listening.

    `llm_helper` is the SAME instance set_up() already registered into
    app_config — mutating its .client in place here is enough for
    llm_helper.is_ready to flip live, with no need to swap the object or
    re-publish anything (unlike the skills dict, which callers replace
    wholesale). Every route that reads app_config.llm_helper.is_ready
    (see log_viewer_routes.index's llm_ready, main_routes.llm_status) sees
    the update on its very next call, no restart required."""
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


def set_up():
    app_config.set_project_root(path_configs.PROJECT_ROOT)

    # Register an as-yet-unconfigured LLM_helper synchronously (llm_ready ==
    # False until the background thread below fills in its .client) — the
    # slow part (finding + reading key.py off a UNC corp share) moves off
    # the startup path entirely; see _configure_llm().
    llm_helper = LLM_helper()
    app_config.set_llm_helper(llm_helper)
    threading.Thread(target=_configure_llm, args=(llm_helper,), daemon=True).start()

    # Load skills from whatever's already in the local mirror RIGHT NOW
    # (previous startup's snapshot, or empty on a first-ever run) — no
    # network access, so this is fast and can't be stalled by the corp
    # share. The live-share sync that used to block here (refresh_shared_
    # cache()) now runs after this on a background thread and republishes
    # app_config once it lands (see _refresh_shared_cache_then_reload()).
    _load_skills_into_config("📌")
    threading.Thread(target=_refresh_shared_cache_then_reload, daemon=True).start()
