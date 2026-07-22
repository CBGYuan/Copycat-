from typing import Optional, Dict, Any

from services.llm_service import LLM_helper


class GlobalConfig:
    """Process-wide state: the configured LLM client and the in-memory skill
    knowledge base (mirrored from data/skills/skills.yaml)."""

    def __init__(self):
        self.llm_helper: Optional[LLM_helper] = None
        self.skills: Dict[str, Any] = {}       # WiFi: shared baseline + this user's contribution + local edits (data/skills/skills.yaml)
        self.bt_skills: Dict[str, Any] = {}    # Bluetooth: shared baseline + local edits (data/skills/bt_skills.yaml)
        self.project_root: Optional[str] = None

    def set_llm_helper(self, llm_helper: LLM_helper) -> None:
        self.llm_helper = llm_helper

    def set_skills(self, skills: Dict[str, Any]) -> None:
        self.skills = skills

    def set_bt_skills(self, skills: Dict[str, Any]) -> None:
        self.bt_skills = skills

    def set_project_root(self, path: str) -> None:
        self.project_root = path


app_config = GlobalConfig()
