"""IDF-weighted retrieval — the cheap half of AutoSkill's hybrid (dense+BM25)
scoring, without a model or a network call.

Plain Jaccard counts every token equally, so in a pool where every skill's
prose repeats the same domain boilerplate ("wifi", "log", "analysis"), that
boilerplate contributes as much as the one token that actually identifies a
skill. Weighting by how rare a token is in the pool fixes that.
"""
import unittest

from services import skill_retrieval
from services.skill_service import Skill

# Boilerplate every skill in this pool repeats.
BOILER = "wifi log analysis check driver status flow"

POOL = {
    "coex": Skill(name="Coex", description=BOILER, keywords=["BT_COEX_DENY"]),
    "roam": Skill(name="Roam", description=BOILER, keywords=["candidate_grade"]),
    "auth": Skill(name="Auth", description=BOILER, keywords=["EAPOL_TIMEOUT"]),
    "scan": Skill(name="Scan", description=BOILER, keywords=["SCAN_ABORT"]),
    # Shares no identifying token, just a lot of the boilerplate.
    "generic": Skill(name="Generic Wifi Log Analysis Check Driver Status Flow",
                     description=BOILER + " " + BOILER, keywords=["misc"]),
}

# Belongs to "coex" — shares its rare keyword — but its prose is boilerplate.
DRAFT = {"name": "Wifi Log Analysis Check", "description": BOILER,
         "keywords": ["BT_COEX_DENY"]}


def _ranked(idf):
    out = [(k, skill_retrieval.score_against(DRAFT, s, idf)) for k, s in POOL.items()]
    out.sort(key=lambda t: t[1], reverse=True)
    return out


class IdfWeightingTests(unittest.TestCase):
    def setUp(self):
        self.idf = skill_retrieval.build_idf(POOL)

    def test_pool_wide_boilerplate_scores_lower_than_an_identifying_token(self):
        # Note the tokenizer splits on non-alphanumerics, so BT_COEX_DENY
        # contributes "bt"/"coex"/"deny" rather than one atom.
        weights = self.idf
        for common in ("wifi", "log", "analysis", "driver"):
            for rare in ("coex", "deny"):
                self.assertLess(weights[common], weights[rare],
                                f"{common!r} appears in every skill and must "
                                f"not weigh as much as {rare!r}")

    def test_it_widens_the_margin_between_the_right_match_and_the_noise(self):
        """The property that actually matters here.

        Both scorings already rank `coex` first in this pool, so the win is
        not a flipped ranking — it is that the correct match pulls further
        away from a skill that merely repeats the boilerplate. route_draft
        turns these scores into decisions against fixed thresholds
        (_CONTINUITY_MIN_SCORE / _CONTINUITY_FORCE_SCORE) and hands the top
        few to an LLM judge, so a wider margin means fewer look-alikes reach
        the judge at all.
        """
        plain, weighted = _ranked(None), _ranked(self.idf)
        self.assertEqual(plain[0][0], "coex")
        self.assertEqual(weighted[0][0], "coex")

        plain_margin = (plain[0][1] - plain[1][1]) / plain[0][1]
        idf_margin = (weighted[0][1] - weighted[1][1]) / weighted[0][1]
        self.assertGreater(idf_margin, plain_margin * 1.5)

    def test_a_token_the_pool_has_never_seen_is_treated_as_distinctive(self):
        # It appears in zero skills, so by the same formula it is maximally
        # rare. Scoring it 0 would discard exactly what makes a new draft new.
        self.assertGreater(skill_retrieval._default_idf(self.idf),
                           max(self.idf.values()) * 0.5)

    def test_an_exact_match_still_scores_one(self):
        sk = Skill(name="X", description="y", keywords=["a", "b"])
        draft = {"name": "X", "description": "y", "keywords": ["a", "b"]}
        self.assertEqual(skill_retrieval.score_against(draft, sk), 1.0)
        self.assertEqual(skill_retrieval.score_against(draft, sk, self.idf), 1.0)

    def test_omitting_the_idf_map_is_plain_jaccard(self):
        # Callers comparing against a single skill with no pool in hand still
        # work unchanged.
        sk = POOL["coex"]
        unweighted = skill_retrieval.score_against(DRAFT, sk)
        self.assertGreater(unweighted, 0)
        self.assertNotEqual(unweighted, skill_retrieval.score_against(DRAFT, POOL["generic"]))

    def test_an_empty_pool_does_not_crash(self):
        self.assertEqual(skill_retrieval.build_idf({}), {})
        self.assertEqual(skill_retrieval.retrieve_top_m(DRAFT, {}), [])

    def test_retrieval_weights_are_stable_across_exclusions(self):
        # Token rarity is a property of the library, not of one search — if it
        # were rebuilt per call, ruling one skill out would silently rescore
        # every other candidate.
        minus = {k: sc for k, _, sc in
                 skill_retrieval.retrieve_top_m(DRAFT, POOL, top_m=5,
                                                exclude_keys={"generic"})}
        for key, _sk, score in skill_retrieval.retrieve_top_m(DRAFT, POOL, top_m=5):
            if key != "generic":
                self.assertAlmostEqual(score, minus[key], places=6)


if __name__ == "__main__":
    unittest.main()
