from typing import Optional, Dict, Any

from services.llm_service import LLM_helper


class GlobalConfig:
    """Process-wide state: the configured LLM client and the in-memory skill
    knowledge base (mirrored from data/skills/skills.yaml)."""

    def __init__(self):
        self.llm_helper: Optional[LLM_helper] = None
        self.skills: Dict[str, Any] = {}       # WiFi: chosen baseline file + local edits
        self.bt_skills: Dict[str, Any] = {}    # Bluetooth: chosen baseline file + local edits
        # Which YAML in the shared mirror is each domain's baseline right now
        # (see skill_service.list_skill_sources / load_shared_skills). Empty
        # means "the team file for that domain". Process-wide rather than
        # per-session on purpose: this is a single-engineer desktop tool, and
        # the choice has to survive a page reload and a switch between the
        # Log Viewer and the Skill Library — both of which are new requests.
        self.wifi_source: str = ""
        self.bt_source: str = ""
        self.project_root: Optional[str] = None

    def source_for(self, domain: str) -> str:
        return self.bt_source if domain == "bt" else self.wifi_source

    def set_source(self, domain: str, path: str) -> None:
        if domain == "bt":
            self.bt_source = path or ""
        else:
            self.wifi_source = path or ""

    def set_llm_helper(self, llm_helper: LLM_helper) -> None:
        self.llm_helper = llm_helper

    def set_skills(self, skills: Dict[str, Any]) -> None:
        self.skills = skills

    def set_bt_skills(self, skills: Dict[str, Any]) -> None:
        self.bt_skills = skills

    def set_project_root(self, path: str) -> None:
        self.project_root = path


app_config = GlobalConfig()
