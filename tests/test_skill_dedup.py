import unittest

from services.skill_service import Skill
from utils import skill_dedup


class NormalizeTests(unittest.TestCase):
    def test_whitespace_padded_keywords_from_real_skills_yaml_match(self):
        # Both of these are verbatim shapes from wireless_ce_avatar's
        # skills.yaml. Retyping one never reproduces the exact padding, so
        # the pre-existing case-insensitive exact match called them new.
        a = " ------- RESUME FLOW"
        b = "------- RESUME  FLOW"
        self.assertEqual(skill_dedup.normalize(a), skill_dedup.normalize(b))


class KeywordDedupTests(unittest.TestCase):
    PARENT = ["DeAuth", "TASK_DISCONNECT", "WDI_ASSOC_STATUS_SUCCESS", " ------- RESUME FLOW"]

    def test_exact_match_after_normalisation_is_safe_to_drop(self):
        out = skill_dedup.dedupe_keywords(["------- RESUME FLOW", "task_disconnect"], self.PARENT)
        self.assertEqual(sorted(k["text"] for k in out["exact"]),
                         ["------- RESUME FLOW", "task_disconnect"])
        self.assertEqual(out["new"], [])

    def test_longer_keyword_is_covered_by_a_shorter_parent_one(self):
        # Parent filters on "DeAuth"; every line "DeAuth detected from AP"
        # could match is already matched. Provably redundant for substring
        # filtering, regardless of similarity score.
        out = skill_dedup.dedupe_keywords(["DeAuth detected from AP"], self.PARENT)
        self.assertEqual([k["text"] for k in out["covered"]], ["DeAuth detected from AP"])
        self.assertEqual(out["covered"][0]["matched"], "DeAuth")

    def test_shorter_keyword_widens_and_must_not_be_treated_as_redundant(self):
        # The opposite direction: "WDI_ASSOC" matches MORE than the parent's
        # "WDI_ASSOC_STATUS_SUCCESS". Dropping it would narrow the skill.
        out = skill_dedup.dedupe_keywords(["WDI_ASSOC"], self.PARENT)
        self.assertEqual([k["text"] for k in out["widens"]], ["WDI_ASSOC"])
        self.assertEqual(out["covered"], [])

    def test_similar_but_not_containing_goes_to_review_never_auto_dropped(self):
        # Hyphen vs underscore: neither string contains the other, so the
        # substring law says nothing and only similarity is left. That is
        # precisely the case a human must rule on — it could be a typo, or a
        # genuinely different token in a different subsystem.
        out = skill_dedup.dedupe_keywords(["TASK-DISCONNECT"], self.PARENT)
        self.assertEqual([k["text"] for k in out["near"]], ["TASK-DISCONNECT"])
        self.assertEqual(out["exact"], [])
        self.assertEqual(out["covered"], [])

    def test_a_longer_variant_is_covered_not_near(self):
        # "TASK_DISCONNECTED" contains the parent's "TASK_DISCONNECT", so it
        # is provably redundant rather than merely similar — the substring
        # law must win over the similarity score.
        out = skill_dedup.dedupe_keywords(["TASK_DISCONNECTED"], self.PARENT)
        self.assertEqual([k["text"] for k in out["covered"]], ["TASK_DISCONNECTED"])
        self.assertEqual(out["near"], [])

    def test_genuinely_unrelated_keyword_is_new(self):
        out = skill_dedup.dedupe_keywords(["BT_COEX_GRANT"], self.PARENT)
        self.assertEqual([k["text"] for k in out["new"]], ["BT_COEX_GRANT"])


class RuleSplitTests(unittest.TestCase):
    RULES = """1. Ownership check (ownership lines).
    - If the log contains: "HOST owns NIC"
      Then conclude:
        - Ownership = Host

2. Roam grade check.
    - If candidate grade delta > 20%
"""

    def test_numbered_rules_itemise_with_subconditions_attached(self):
        units = skill_dedup.split_rules(self.RULES)
        self.assertEqual(len(units), 2)
        # The indented "- If the log contains" is a condition OF rule 1 and
        # must stay attached; comparing it standalone would be meaningless.
        self.assertIn("HOST owns NIC", units[0])
        self.assertIn("Roam grade check", units[1])

    def test_reworded_rule_is_caught_as_near_not_new(self):
        # This is the case whole-text line matching misses entirely: the
        # rule is present in the parent, just phrased differently.
        parent = "1. Ownership check (ownership lines). Confirm the host holds the NIC semaphore."
        candidate = "1. Ownership check (ownership line). Confirm the host holds the NIC semaphore!"
        out = skill_dedup.dedupe_rules(candidate, parent)
        self.assertEqual(len(out["near"]), 1)
        self.assertEqual(out["new"], [])

    def test_short_fragments_do_not_ratio_match_by_accident(self):
        # Two unrelated short strings can score >0.85 by chance; below the
        # length floor only exact matches count.
        out = skill_dedup.dedupe_rules("- N/A", "- N/B")
        self.assertEqual(len(out["new"]), 1)
        self.assertEqual(out["near"], [])


class DiffAgainstParentTests(unittest.TestCase):
    def test_summary_separates_provably_redundant_from_needs_review(self):
        parent = Skill(
            name="Connection Flow", description="Generic Wi-Fi connectivity flow.",
            keywords=["DeAuth", "TASK_DISCONNECT"], exclusive=["SCAN_REQUEST"],
            expert_rules="1. Ownership check. Confirm the host holds the NIC semaphore.",
        )
        draft = {
            "keywords": [
                "TASK_DISCONNECT",          # exact   -> auto-removable
                "DeAuth detected from AP",  # covered -> auto-removable
                "TASK-DISCONNECT",          # near    -> needs review
                "BT_COEX_GRANT",            # new
            ],
            "exclusive": ["SCAN_REQUEST"],  # exact   -> auto-removable
            "expert_rules": "1. Ownership check. Confirm the host holds the NIC semaphore.",
        }
        d = skill_dedup.diff_against_parent(draft, parent)
        # 2 keywords + 1 exclusive + 1 rule are provably redundant.
        self.assertEqual(d["summary"]["auto_removable"], 4)
        # The merely-similar keyword is NOT counted as removable.
        self.assertEqual(d["summary"]["needs_review"], 1)
        self.assertEqual([k["text"] for k in d["keywords"]["new"]], ["BT_COEX_GRANT"])


if __name__ == "__main__":
    unittest.main()


class LineageDepthTests(unittest.TestCase):
    """The chain has to stop somewhere. Extension exports are FLAT, so each
    generation's keyword list is a superset of its parent's — by gen 3 the
    filter matches most of the log, Avatar truncates the evidence payload
    head+tail, and the skill has stopped focusing anything."""

    def test_a_root_parent_can_take_a_child(self):
        root = Skill(name="Connection Flow", description="d", lineage=[])
        out = skill_dedup.lineage_depth_check(root)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["child_depth"], 1)

    def test_a_gen_one_parent_can_still_take_a_child(self):
        gen1 = Skill(name="Roam Grade", description="d", parent="root", lineage=["root"])
        self.assertTrue(skill_dedup.lineage_depth_check(gen1)["allowed"])

    def test_a_gen_two_parent_is_refused_so_no_gen_three_is_created(self):
        gen2 = Skill(name="Coex Deep", description="d", parent="gen1",
                     lineage=["root", "gen1"])
        out = skill_dedup.lineage_depth_check(gen2)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["child_depth"], 3)
        self.assertEqual(out["max_depth"], skill_dedup.MAX_LINEAGE_DEPTH)


class DescriptionConflictTests(unittest.TestCase):
    """Avatar's agent chooses a skill from `name: description` alone and can
    only pass a key from an enum — it never sees keywords at selection time.
    A child inherits every parent keyword, so the description is the ONLY
    field left that can distinguish them."""

    PARENT = Skill(name="Connection Flow",
                   description="Generic Wi-Fi association, authentication and roam baseline.")

    def test_a_reworded_copy_of_the_parent_description_is_flagged(self):
        out = skill_dedup.description_conflict(
            "Generic Wi-Fi association, authentication and roam baselines.", self.PARENT)
        self.assertTrue(out["too_similar"])
        self.assertEqual(out["parent_description"], self.PARENT.description)

    def test_a_genuinely_narrower_description_passes(self):
        out = skill_dedup.description_conflict(
            "Roam decisions where the candidate grade delta crossed the 20% threshold.",
            self.PARENT)
        self.assertFalse(out["too_similar"])

    def test_a_missing_description_is_not_reported_as_a_conflict(self):
        # Empty is a different problem (the strength meter's), and reporting it
        # here would put two unrelated warnings on the same field.
        self.assertFalse(skill_dedup.description_conflict("", self.PARENT)["too_similar"])
        self.assertFalse(
            skill_dedup.description_conflict("anything", Skill(name="x", description=""))["too_similar"])


class ExportBuilderTests(unittest.TestCase):
    PARENT = Skill(
        name="Connection Flow", description="Generic Wi-Fi connectivity flow.",
        keywords=["DeAuth", "TASK_DISCONNECT"], exclusive=["SCAN_REQUEST"],
        expert_rules="1. Ownership check. Confirm the host holds the NIC semaphore.",
        lineage=[],
    )
    DRAFT = {
        "name": "Roam Grade Analysis",
        "description": "Roam decisions driven by candidate grade delta.",
        "keywords": ["TASK_DISCONNECT", "DeAuth detected from AP", "candidate grade", "TASK-DISCONNECT"],
        "exclusive": ["SCAN_REQUEST", "PROP_GET_STATISTICS"],
        "expert_rules": ("1. Ownership check. Confirm the host holds the NIC semaphore.\n\n"
                         "2. Roam grade check. If candidate grade delta > 20%, expect a roam."),
    }

    def setUp(self):
        self.diff = skill_dedup.diff_against_parent(self.DRAFT, self.PARENT)

    def test_deduped_export_keeps_only_what_the_parent_lacks(self):
        out = skill_dedup.build_deduped_skill(self.DRAFT, self.diff)
        # exact ("TASK_DISCONNECT") and covered ("DeAuth detected from AP") go;
        # the genuinely new one and the unreviewed near-match stay.
        self.assertEqual(out["keywords"], ["candidate grade", "TASK-DISCONNECT"])
        self.assertEqual(out["exclusive"], ["PROP_GET_STATISTICS"])
        self.assertIn("Roam grade check", out["expert_rules"])
        self.assertNotIn("Ownership check", out["expert_rules"])

    def test_unreviewed_near_matches_are_never_dropped_by_default(self):
        kept = skill_dedup.build_deduped_skill(self.DRAFT, self.diff)
        self.assertIn("TASK-DISCONNECT", kept["keywords"])
        # ...but the engineer can opt in to dropping them once reviewed.
        dropped = skill_dedup.build_deduped_skill(self.DRAFT, self.diff, keep_near=False)
        self.assertNotIn("TASK-DISCONNECT", dropped["keywords"])

    def test_extension_export_is_fully_resolved_so_avatar_can_run_it(self):
        out = skill_dedup.build_extension_skill(self.DRAFT, self.PARENT, "connection_flow", self.diff)
        # Every parent keyword must survive — a delta-only child would load
        # into Avatar with a fraction of its filters and analyse wrongly.
        for kw in self.PARENT.keywords:
            self.assertIn(kw, out["keywords"])
        self.assertIn("candidate grade", out["keywords"])
        # Redundant ones still don't get duplicated in.
        self.assertEqual(out["keywords"].count("TASK_DISCONNECT"), 1)
        self.assertNotIn("DeAuth detected from AP", out["keywords"])

    def test_extension_marks_inherited_vs_added_rules(self):
        out = skill_dedup.build_extension_skill(self.DRAFT, self.PARENT, "connection_flow", self.diff)
        self.assertIn("Ownership check", out["expert_rules"])       # inherited, verbatim
        self.assertIn("Roam grade check", out["expert_rules"])      # added
        self.assertIn("Extension:", out["expert_rules"])            # boundary marker
        self.assertLess(out["expert_rules"].index("Ownership check"),
                        out["expert_rules"].index("Extension:"))

    def test_extension_records_the_vertical_chain(self):
        out = skill_dedup.build_extension_skill(self.DRAFT, self.PARENT, "connection_flow", self.diff)
        self.assertEqual(out["parent"], "connection_flow")
        self.assertEqual(out["lineage"], ["connection_flow"])

    def test_chain_deepens_across_generations(self):
        gen1 = Skill(name="Gen1", description="d", keywords=["A"],
                     parent="root", lineage=["root"])
        out = skill_dedup.build_extension_skill(
            {"name": "Gen2", "description": "d", "keywords": ["B"], "expert_rules": ""},
            gen1, "gen1", skill_dedup.diff_against_parent({"keywords": ["B"]}, gen1))
        self.assertEqual(out["lineage"], ["root", "gen1"])
        self.assertEqual(out["parent"], "gen1")
