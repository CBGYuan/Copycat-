"""Skill Library — lineage persistence, version-trail semantics, and the
local-only guarantee of Export.

These cover the seam between three pieces that each looked correct alone but
weren't wired together: skill_dedup builds a child carrying `parent`/`lineage`,
the YAML writer knows how to emit those keys, and save_skill decides where the
file goes — but `_skill_to_raw` sat in the middle and silently dropped them, so
every exported extension came out looking standalone.
"""
import os
import shutil
import tempfile
import unittest

from configs import path_configs
from services import skill_service
from services.skill_service import Skill
from utils import skill_dedup


class _LocalFileCase(unittest.TestCase):
    """Redirects the LOCAL skill files (the only ones this app writes) into a
    temp dir, so tests exercise the real save path without touching the
    engineer's own data/skills/local files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (path_configs.SKILLS_LOCAL_DIR,
                       path_configs.SKILLS_YAML_PATH,
                       path_configs.SKILLS_BT_YAML_PATH)
        path_configs.SKILLS_LOCAL_DIR = self.tmp
        path_configs.SKILLS_YAML_PATH = os.path.join(self.tmp, "skills.yaml")
        path_configs.SKILLS_BT_YAML_PATH = os.path.join(self.tmp, "bt_skills.yaml")

    def tearDown(self):
        (path_configs.SKILLS_LOCAL_DIR,
         path_configs.SKILLS_YAML_PATH,
         path_configs.SKILLS_BT_YAML_PATH) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def local_text(self):
        with open(path_configs.SKILLS_YAML_PATH, encoding="utf-8") as f:
            return f.read()


PARENT = Skill(
    name="Connection Flow", description="Generic Wi-Fi connectivity flow.",
    keywords=["DeAuth", "TASK_DISCONNECT"], exclusive=["SCAN_REQUEST"],
    expert_rules="1. Ownership check. Confirm the host holds the NIC semaphore.",
    version="0.3.1",
    version_history=[{"version": "0.2.0", "name": "Connection Flow",
                      "description": "old", "keywords": ["DeAuth"],
                      "exclusive": [], "expert_rules": "",
                      "saved_at": "2026-05-01T10:00:00"}],
)
DRAFT = {
    "name": "Roam Grade Analysis",
    "description": "Roam decisions driven by candidate grade delta.",
    "keywords": ["candidate grade", "BT_COEX_GRANT"],
    "exclusive": [],
    "expert_rules": "1. Roam grade check. If grade delta > 20%, expect a roam.",
}


class ExportIsLocalOnlyTests(_LocalFileCase):
    def test_without_a_loaded_skill_the_draft_is_written_verbatim(self):
        key = skill_service.save_skill(None, Skill(**DRAFT), domain="wifi",
                                       base={"connection_flow": PARENT})
        local = skill_service.load_all_skills("wifi")
        self.assertEqual(list(local), [key])
        self.assertEqual(local[key].keywords, DRAFT["keywords"])
        self.assertIsNone(local[key].parent)

    def test_the_loaded_cloud_skill_is_never_copied_into_the_local_file(self):
        # The whole point of the local file: it holds only what this workbench
        # originated. A copy of the parent here would shadow the shared entry
        # and freeze its version/version_history at whatever they were when the
        # child was exported.
        diff = skill_dedup.diff_against_parent(DRAFT, PARENT)
        ext = skill_dedup.build_extension_skill(DRAFT, PARENT, "connection_flow", diff)
        skill_service.save_skill(
            None, Skill(**{k: v for k, v in ext.items() if k in Skill.model_fields}),
            domain="wifi", base={"connection_flow": PARENT})

        text = self.local_text()
        self.assertNotIn("\nconnection_flow:", text)
        self.assertNotIn('"0.3.1"', text)   # the parent's version line
        self.assertNotIn("0.2.0", text)     # the parent's history
        self.assertEqual(list(skill_service.load_all_skills("wifi")),
                         ["Roam_Grade_Analysis"])

    def test_an_inheriting_child_starts_its_own_version_line_at_zero(self):
        diff = skill_dedup.diff_against_parent(DRAFT, PARENT)
        ext = skill_dedup.build_extension_skill(DRAFT, PARENT, "connection_flow", diff)
        key = skill_service.save_skill(
            None, Skill(**{k: v for k, v in ext.items() if k in Skill.model_fields}),
            domain="wifi", base={"connection_flow": PARENT})
        child = skill_service.load_all_skills("wifi")[key]
        self.assertEqual(child.version, "0.1.0")
        self.assertEqual(child.version_history, [])


class LineagePersistenceTests(_LocalFileCase):
    def _save_child(self):
        diff = skill_dedup.diff_against_parent(DRAFT, PARENT)
        ext = skill_dedup.build_extension_skill(DRAFT, PARENT, "connection_flow", diff)
        return skill_service.save_skill(
            None, Skill(**{k: v for k, v in ext.items() if k in Skill.model_fields}),
            domain="wifi", base={"connection_flow": PARENT})

    def test_parent_and_lineage_survive_the_yaml_round_trip(self):
        # The regression: _skill_to_raw used to omit both keys, so the chain
        # built by build_extension_skill never reached the file.
        key = self._save_child()
        child = skill_service.load_all_skills("wifi")[key]
        self.assertEqual(child.parent, "connection_flow")
        self.assertEqual(child.lineage, ["connection_flow"])

    def test_lineage_survives_a_later_edit(self):
        key = self._save_child()
        child = skill_service.load_all_skills("wifi")[key]
        edited = Skill(**{**child.model_dump(), "description": "edited"})
        skill_service.save_skill(key, edited, domain="wifi", base={})
        again = skill_service.load_all_skills("wifi")[key]
        self.assertEqual(again.parent, "connection_flow")
        self.assertEqual(again.lineage, ["connection_flow"])


class RestoreVersionTests(_LocalFileCase):
    def _skill_with_trail(self):
        key = skill_service.save_skill(None, Skill(**DRAFT, parent="connection_flow",
                                                   lineage=["connection_flow"]),
                                       domain="wifi", base={})
        first = skill_service.load_all_skills("wifi")[key]
        skill_service.save_skill(
            key, Skill(**{**first.model_dump(), "keywords": first.keywords + ["ADDED"]}),
            domain="wifi", base={})
        return key

    def test_restore_moves_the_version_forward_instead_of_rewinding(self):
        # A rollback that rewound the counter would make restore the one
        # operation capable of losing work — the state being rolled back FROM
        # has to stay in the trail.
        key = self._skill_with_trail()
        self.assertEqual(skill_service.load_all_skills("wifi")[key].version, "0.1.1")

        skill_service.restore_version(key, "0.1.0", domain="wifi", base={})
        after = skill_service.load_all_skills("wifi")[key]
        self.assertEqual(after.version, "0.1.2")
        self.assertNotIn("ADDED", after.keywords)
        self.assertEqual([h["version"] for h in after.version_history], ["0.1.0", "0.1.1"])

    def test_restore_leaves_lineage_alone(self):
        # No past revision of a skill's own body can change where it came from.
        key = self._skill_with_trail()
        skill_service.restore_version(key, "0.1.0", domain="wifi", base={})
        after = skill_service.load_all_skills("wifi")[key]
        self.assertEqual(after.parent, "connection_flow")
        self.assertEqual(after.lineage, ["connection_flow"])

    def test_unknown_version_and_unknown_skill_are_refused(self):
        key = self._skill_with_trail()
        self.assertIsNone(skill_service.restore_version(key, "9.9.9", domain="wifi", base={}))
        self.assertIsNone(skill_service.restore_version("no_such_skill", "0.1.0",
                                                        domain="wifi", base={}))

    def test_a_skill_that_is_not_local_cannot_be_restored(self):
        # Shared / contribution skills are a read-only mirror of the corp
        # drive; their history is viewable but this app never rewrites it.
        self.assertIsNone(skill_service.restore_version("connection_flow", "0.2.0",
                                                        domain="wifi", base={}))


class ConvergeInheritanceRouteTests(unittest.TestCase):
    """The two guards on the export path, checked through the real route:
    a chain at max depth falls back to standalone (without losing the taught
    knowledge), and an inheriting draft carries a description verdict."""

    DRAFT_RESPONSE = {"skills": [{
        "name": "Roam Grade Analysis",
        # A near-copy of GEN0's description on purpose.
        "description": "Generic Wi-Fi association, authentication and roam baselines.",
        "keywords": ["candidate grade", "BT_COEX_GRANT"],
        "exclusive": [],
        "expert_rules": "1. Roam grade check. If grade delta > 20%, expect a roam.",
    }]}
    GEN0 = Skill(name="Connection Flow",
                 description="Generic Wi-Fi association, authentication and roam baseline.",
                 keywords=["DeAuth", "TASK_DISCONNECT"], exclusive=["SCAN_REQUEST"],
                 expert_rules="1. Ownership check. Confirm the host holds the NIC semaphore.",
                 lineage=[])
    GEN2 = Skill(name="Coex Deep Dive", description="Deep BT coex contention analysis.",
                 keywords=["DeAuth", "BT_COEX_DENY"],
                 expert_rules="1. Deny pattern check.",
                 parent="gen1", lineage=["connection_flow", "gen1"])

    def setUp(self):
        import json as _json
        from app import create_app
        from configs.global_configs import app_config
        from services import learning_service

        class _FakeLlm:
            def __init__(self, response):
                self.response = response
                self.prompts = []
                self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                self.session_usage = {"prompt_tokens": 0, "completion_tokens": 0,
                                      "total_tokens": 0, "calls": 0}
                self.is_ready = True

            def chat(self, **kwargs):
                self.prompts.append(_json.dumps(kwargs, default=str))
                return _json.dumps(self.response)

        self.app = create_app()
        self.cfg = app_config
        self._real_llm = app_config.llm_helper
        self.llm = _FakeLlm(self.DRAFT_RESPONSE)
        app_config.llm_helper = self.llm
        self._real_pool = dict(app_config.skills)
        # route_draft is its own LLM call and not what these tests are about.
        self._real_route = learning_service.route_draft
        learning_service.route_draft = lambda llm, draft, pool, cont, **kw: dict(
            draft, judge={"action": "add", "target_skill_key": None,
                          "reason": "new", "confidence": 0.9})

    def tearDown(self):
        from configs.global_configs import app_config
        from services import learning_service
        app_config.llm_helper = self._real_llm
        app_config.set_skills(self._real_pool)
        learning_service.route_draft = self._real_route

    def _converge_with(self, parent, key):
        from services import session_store
        pool = dict(self._real_pool)
        pool[key] = parent
        self.cfg.set_skills(pool)
        client = self.app.test_client()
        client.post("/log_viewer/load_skill", json={"skill_key": key})
        with client.session_transaction() as sess:
            state = session_store._STORE[sess["wsid"]]
        state.chat_history = [
            {"role": "user", "content": "The roam fired when the grade delta crossed 20%."},
            {"role": "assistant", "content": "Noted — grade-driven roam."},
        ]
        state.filtered_preview = ["00:01:02 candidate grade 55 -> 78"]
        return client.post("/learning/converge", json={}).get_json()

    def test_a_root_parent_is_inherited_and_the_prompt_is_told_so(self):
        d = self._converge_with(self.GEN0, "connection_flow")
        self.assertTrue(d["success"])
        self.assertEqual(d["inherited_from"], "connection_flow")
        self.assertIsNone(d["lineage_blocked"])
        self.assertIn("DeAuth", d["drafts"][0]["keywords"])
        # The synthesis must be told about the baseline BEFORE it writes the
        # description — that description is the only thing that will
        # distinguish parent from child downstream.
        self.assertIn("BASELINE SKILL (currently loaded)", "\n".join(self.llm.prompts))

    def test_a_description_that_merely_rewords_the_parent_is_flagged(self):
        d = self._converge_with(self.GEN0, "connection_flow")
        conflict = d["drafts"][0]["lineage_info"]["description_conflict"]
        self.assertTrue(conflict["too_similar"])
        self.assertEqual(conflict["parent_description"], self.GEN0.description)

    def test_at_max_depth_inheritance_is_refused_but_the_export_still_happens(self):
        # Refusing to deepen the chain must never mean discarding what the
        # engineer just taught — it lands as a fresh root instead.
        d = self._converge_with(self.GEN2, "gen2")
        self.assertTrue(d["success"])
        self.assertIsNone(d["inherited_from"])
        self.assertEqual(len(d["drafts"]), 1)
        self.assertIsNone(d["drafts"][0].get("lineage_info"))
        self.assertNotIn("DeAuth", d["drafts"][0]["keywords"])
        blocked = d["lineage_blocked"]
        self.assertEqual(blocked["child_depth"], 3)
        self.assertEqual(blocked["max_depth"], skill_dedup.MAX_LINEAGE_DEPTH)

    def test_a_refused_chain_never_promises_inheritance_in_the_prompt(self):
        self._converge_with(self.GEN2, "gen2")
        self.assertNotIn("BASELINE SKILL (currently loaded)", "\n".join(self.llm.prompts))


class DurableWriteTests(_LocalFileCase):
    """The local skills file is the only copy of every locally-taught skill
    AND of every version_history trail behind the Skill Library's rollback.
    Nothing that goes wrong during a save may be allowed to shorten it."""

    def _seed(self):
        return skill_service.save_skill(None, Skill(**DRAFT), domain="wifi", base={})

    def test_a_failure_while_serializing_leaves_the_file_untouched(self):
        # The original bug: open(path, "w") truncates before the argument is
        # evaluated, so ANY error while building the YAML emptied the file.
        key = self._seed()
        before = self.local_text()
        self.assertIn(key, before)

        real_dump = skill_service._dump_skills_yaml
        skill_service._dump_skills_yaml = lambda raw: (_ for _ in ()).throw(
            ValueError("boom while serializing"))
        try:
            with self.assertRaises(ValueError):
                skill_service.save_skill(None, Skill(**{**DRAFT, "name": "Second"}),
                                         domain="wifi", base={})
        finally:
            skill_service._dump_skills_yaml = real_dump

        self.assertEqual(self.local_text(), before)
        self.assertIn(key, skill_service.load_all_skills("wifi"))

    def test_a_failure_while_writing_leaves_the_previous_file_intact(self):
        # The replace is atomic, so a crash mid-write can only ever leave the
        # temp file behind — never a half-written skills file.
        key = self._seed()
        before = self.local_text()

        real_replace = skill_service.os.replace
        skill_service.os.replace = lambda *a, **k: (_ for _ in ()).throw(
            OSError("boom while replacing"))
        try:
            with self.assertRaises(OSError):
                skill_service.save_skill(None, Skill(**{**DRAFT, "name": "Second"}),
                                         domain="wifi", base={})
        finally:
            skill_service.os.replace = real_replace

        self.assertEqual(self.local_text(), before)
        self.assertIn(key, skill_service.load_all_skills("wifi"))

    def test_no_temp_files_are_left_lying_around_after_a_normal_save(self):
        self._seed()
        leftovers = [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_the_previous_version_is_kept_as_a_backup(self):
        first = self._seed()
        skill_service.save_skill(None, Skill(**{**DRAFT, "name": "Second skill"}),
                                 domain="wifi", base={})
        backup = path_configs.SKILLS_YAML_PATH + skill_service.BACKUP_SUFFIX
        self.assertTrue(os.path.exists(backup))
        with open(backup, encoding="utf-8") as f:
            self.assertIn(first, f.read())

    def test_an_unparseable_local_file_refuses_the_write_instead_of_erasing_it(self):
        # Without this, a corrupt file parses as "no skills", and the next
        # save persists that emptiness — turning a recoverable mistake into
        # permanent loss.
        self._seed()
        with open(path_configs.SKILLS_YAML_PATH, "w", encoding="utf-8") as f:
            f.write("skills: [unclosed\n  bracket: :::\n")
        mangled = self.local_text()

        with self.assertRaises(skill_service.SkillStoreError):
            skill_service.load_all_skills("wifi")
        with self.assertRaises(skill_service.SkillStoreError):
            skill_service.save_skill(None, Skill(**DRAFT), domain="wifi", base={})
        with self.assertRaises(skill_service.SkillStoreError):
            skill_service.delete_skill("anything", domain="wifi")
        # Still exactly as the engineer left it, so the .bak beside it is
        # still the right thing to recover from.
        self.assertEqual(self.local_text(), mangled)

    def test_the_save_route_reports_a_refusal_rather_than_a_server_error(self):
        from app import create_app
        self._seed()
        with open(path_configs.SKILLS_YAML_PATH, "w", encoding="utf-8") as f:
            f.write("nope: [unclosed\n  ::: \n")
        client = create_app().test_client()
        resp = client.post("/skills/save", json={"name": "X", "description": "y",
                                                 "keywords": ["a"], "domain": "wifi"})
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertFalse(body["success"])
        self.assertIn(skill_service.BACKUP_SUFFIX, body["message"])


class TriggerSetTests(_LocalFileCase):
    """Trigger conditions are edited as a structured list but have to end up
    INSIDE the description, because that is the only field Avatar's agent
    reads when choosing between skills. Structured in, compiled out."""

    DESC = "Roam decisions driven by candidate grade delta."
    TRG = ["platform is LNL", "resume from S4"]

    def test_compiling_is_idempotent(self):
        # Without stripping first, every re-export would stack another
        # "Applies when:" clause onto the one line the agent selects on.
        once = skill_service.compiled_description(self.DESC, self.TRG)
        self.assertEqual(skill_service.compiled_description(once, self.TRG), once)
        self.assertEqual(skill_service.base_description(once), self.DESC)

    def test_triggers_reach_the_field_avatar_actually_reads(self):
        import yaml
        key = skill_service.save_skill(
            None, Skill(name="Roam Grade", description=self.DESC,
                        keywords=["candidate grade"], triggers=self.TRG),
            domain="wifi", base={})
        entry = yaml.safe_load(self.local_text())[key]
        self.assertIn("Applies when: platform is LNL; resume from S4.", entry["description"])
        self.assertEqual(entry["triggers"], self.TRG)

    def test_the_in_memory_description_stays_what_the_engineer_wrote(self):
        # Otherwise the editor would show generated text as if it were typed,
        # and the next save would compile it a second time.
        key = skill_service.save_skill(
            None, Skill(name="Roam Grade", description=self.DESC, triggers=self.TRG),
            domain="wifi", base={})
        back = skill_service.load_all_skills("wifi")[key]
        self.assertEqual(back.description, self.DESC)
        self.assertEqual(back.triggers, self.TRG)

    def test_resaving_does_not_stack_the_clause(self):
        import yaml
        key = skill_service.save_skill(
            None, Skill(name="Roam Grade", description=self.DESC, triggers=self.TRG),
            domain="wifi", base={})
        back = skill_service.load_all_skills("wifi")[key]
        skill_service.save_skill(key, back, domain="wifi", base={})
        desc = yaml.safe_load(self.local_text())[key]["description"]
        self.assertEqual(desc.count("Applies when:"), 1)

    def test_a_restored_revision_brings_its_own_triggers_back(self):
        key = skill_service.save_skill(
            None, Skill(name="Roam Grade", description=self.DESC, triggers=self.TRG),
            domain="wifi", base={})
        first = skill_service.load_all_skills("wifi")[key]
        skill_service.save_skill(
            key, Skill(**{**first.model_dump(), "triggers": ["something else"]}),
            domain="wifi", base={})
        skill_service.restore_version(key, "0.1.0", domain="wifi", base={})
        self.assertEqual(skill_service.load_all_skills("wifi")[key].triggers, self.TRG)

    def test_a_pre_trigger_snapshot_does_not_silently_empty_them(self):
        # History written before triggers existed has no `triggers` key at
        # all; restoring one must keep the current set rather than wiping it.
        key = skill_service.save_skill(
            None, Skill(name="Roam Grade", description=self.DESC, triggers=self.TRG),
            domain="wifi", base={})
        current = skill_service.load_all_skills("wifi")[key]
        legacy = {"version": "0.0.9", "name": "Roam Grade", "description": self.DESC,
                  "keywords": [], "exclusive": [], "expert_rules": "",
                  "saved_at": "2026-01-01T00:00:00"}
        skill_service.save_skill(
            key, Skill(**{**current.model_dump(), "version_history": [legacy]}),
            domain="wifi", base={})
        skill_service.restore_version(key, "0.0.9", domain="wifi", base={})
        self.assertEqual(skill_service.load_all_skills("wifi")[key].triggers, self.TRG)


class DescriptionConflictWithTriggersTests(unittest.TestCase):
    PARENT = Skill(name="Connection Flow",
                   description="Generic Wi-Fi association, authentication and roam baseline.",
                   triggers=["any platform"])
    NEAR_COPY = "Generic Wi-Fi association, authentication and roam baselines."

    def test_without_triggers_a_reworded_copy_is_still_flagged(self):
        out = skill_dedup.description_conflict(self.NEAR_COPY, self.PARENT, [])
        self.assertTrue(out["too_similar"])

    def test_a_trigger_the_parent_lacks_resolves_the_conflict(self):
        # It ends up in the saved description, so it genuinely distinguishes
        # them downstream — warning anyway trains the engineer to ignore it.
        out = skill_dedup.description_conflict(self.NEAR_COPY, self.PARENT, ["resume from S4"])
        self.assertFalse(out["too_similar"])
        self.assertEqual(out["distinguishing_triggers"], ["resume from s4"])

    def test_repeating_the_parents_own_trigger_distinguishes_nothing(self):
        out = skill_dedup.description_conflict(self.NEAR_COPY, self.PARENT, ["any platform"])
        self.assertTrue(out["too_similar"])
        self.assertEqual(out["distinguishing_triggers"], [])


class BaselineSourceTests(_LocalFileCase):
    """Which YAML a domain's pool is read from is now an explicit choice.

    It used to be an implicit three-way merge (team file -> this engineer's
    contribution -> local) that nobody could see or reproduce: a skill could
    resolve from any of the three, and the UI could only report which had won
    after the fact. The Export path promises that an inherited skill's
    inherited half is exactly one file's content, which that merge made
    untrue."""

    def setUp(self):
        super().setUp()
        self.cache = tempfile.mkdtemp()
        self.contrib_dir = os.path.join(self.cache, "user_contributions")
        os.makedirs(self.contrib_dir, exist_ok=True)
        self._saved_cache = (path_configs.SKILLS_CACHE_DIR,
                             path_configs.SKILLS_CACHE_WIFI_PATH,
                             path_configs.SKILLS_CACHE_BT_PATH,
                             path_configs.SKILLS_CACHE_USER_CONTRIB_DIR)
        path_configs.SKILLS_CACHE_DIR = self.cache
        path_configs.SKILLS_CACHE_WIFI_PATH = os.path.join(self.cache, "skills.yaml")
        path_configs.SKILLS_CACHE_BT_PATH = os.path.join(self.cache, "bt_skills.yaml")
        path_configs.SKILLS_CACHE_USER_CONTRIB_DIR = self.contrib_dir

        self._write(path_configs.SKILLS_CACHE_WIFI_PATH, {"team_skill": "Team Skill"})
        self._write(path_configs.SKILLS_CACHE_BT_PATH, {"bt_team": "BT Team"})
        self._write(os.path.join(self.contrib_dir, "someone__skills_2026-05-13.yaml"),
                    {"contrib_skill": "Contributed Skill", "team_skill": "Overridden"})

    def tearDown(self):
        (path_configs.SKILLS_CACHE_DIR,
         path_configs.SKILLS_CACHE_WIFI_PATH,
         path_configs.SKILLS_CACHE_BT_PATH,
         path_configs.SKILLS_CACHE_USER_CONTRIB_DIR) = self._saved_cache
        shutil.rmtree(self.cache, ignore_errors=True)
        super().tearDown()

    @staticmethod
    def _write(path, entries):
        raw = {k: {"name": n, "description": "d", "keywords": ["kw"], "expert_rules": ""}
               for k, n in entries.items()}
        with open(path, "w", encoding="utf-8") as f:
            f.write(skill_service._dump_skills_yaml(raw))

    def test_wifi_offers_the_team_file_and_every_contribution(self):
        srcs = skill_service.list_skill_sources("wifi")
        self.assertEqual([s["kind"] for s in srcs], ["shared", "contribution"])
        self.assertEqual(srcs[0]["path"], path_configs.SKILLS_CACHE_WIFI_PATH)
        self.assertEqual(srcs[0]["skill_count"], 1)

    def test_bt_offers_only_its_team_file(self):
        # There is no per-engineer contribution file for Bluetooth on the share.
        srcs = skill_service.list_skill_sources("bt")
        self.assertEqual([s["kind"] for s in srcs], ["shared"])

    def test_the_default_baseline_is_the_team_file(self):
        pools = skill_service.load_shared_skills()
        self.assertEqual(set(pools["wifi"]), {"team_skill"})
        self.assertEqual(pools["wifi"]["team_skill"].name, "Team Skill")

    def test_choosing_a_contribution_replaces_the_baseline_rather_than_layering(self):
        contrib = skill_service.list_skill_sources("wifi")[1]["path"]
        pools = skill_service.load_shared_skills(wifi_source=contrib)
        # The team file's own entries are GONE, not merged underneath — and
        # team_skill resolves to the contribution's version of it.
        self.assertEqual(set(pools["wifi"]), {"contrib_skill", "team_skill"})
        self.assertEqual(pools["wifi"]["team_skill"].name, "Overridden")

    def test_local_skills_still_layer_on_top_of_any_chosen_baseline(self):
        # Local is this workbench's own output, not part of any baseline.
        skill_service.save_skill(None, Skill(**DRAFT), domain="wifi", base={})
        contrib = skill_service.list_skill_sources("wifi")[1]["path"]
        pools = skill_service.load_shared_skills(wifi_source=contrib)
        self.assertIn("Roam_Grade_Analysis", pools["wifi"])
        self.assertIn("contrib_skill", pools["wifi"])

    def test_an_unreadable_source_falls_back_instead_of_emptying_the_pool(self):
        pools = skill_service.load_shared_skills(wifi_source=os.path.join(self.cache, "nope.yaml"))
        self.assertEqual(set(pools["wifi"]), {"team_skill"})

    def test_origins_report_the_kind_of_baseline_actually_chosen(self):
        contrib = skill_service.list_skill_sources("wifi")[1]["path"]
        self.assertEqual(skill_service.skill_origins("wifi")["team_skill"], "shared")
        self.assertEqual(skill_service.skill_origins("wifi", contrib)["contrib_skill"],
                         "contribution")

    def test_switching_the_source_never_touches_the_local_file(self):
        key = skill_service.save_skill(None, Skill(**DRAFT), domain="wifi", base={})
        before = self.local_text()
        contrib = skill_service.list_skill_sources("wifi")[1]["path"]
        skill_service.load_shared_skills(wifi_source=contrib)
        skill_service.load_shared_skills()
        self.assertEqual(self.local_text(), before)
        self.assertIn(key, skill_service.load_all_skills("wifi"))


class SkillOriginTests(_LocalFileCase):
    def test_a_locally_saved_skill_reports_as_local(self):
        key = skill_service.save_skill(None, Skill(**DRAFT), domain="wifi", base={})
        self.assertEqual(skill_service.skill_origins("wifi").get(key), "local")

    def test_a_key_only_in_the_shared_cache_is_not_reported_as_local(self):
        origins = skill_service.skill_origins("wifi")
        for key, origin in origins.items():
            if origin != "local":
                self.assertIn(origin, ("shared", "contribution"))
                break


if __name__ == "__main__":
    unittest.main()
