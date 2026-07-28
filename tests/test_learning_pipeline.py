import json
import unittest

from services.learning_service import analyze_round, assess_teaching_evidence
from services.session_store import WorkingState
from utils.tat_parser import matched_keywords_for_line


class FakeLlm:
    def __init__(self, response):
        self.response = response

    def chat(self, **_kwargs):
        return json.dumps(self.response)


class LearningPipelineTests(unittest.TestCase):
    def test_round_only_asks_when_behaviors_diverge(self):
        base = {
            "analysis": "The edit has two possible scopes.",
            "readiness": {"score": 50, "note": "Needs scope."},
            "coverage": {"knowledge": 50, "scope": 20, "keywords": 80},
            "gaps": ["Confirm scope"],
            "validation": [],
            "ready_to_export": False,
            "questions": [{"question": "Keep the near-disconnect line?", "type": "choice",
                           "options": ["Keep", "Drop"]}],
        }
        no_divergence = dict(base, ambiguity={
            "requires_clarification": False,
            "divergent_behaviors": [],
        })
        result = analyze_round(FakeLlm(no_divergence), {}, 1)
        self.assertEqual(result["questions"], [])

        divergent = dict(base, ambiguity={
            "requires_clarification": True,
            "divergent_behaviors": ["Keeps line 10", "Drops line 10"],
        })
        result = analyze_round(FakeLlm(divergent), {}, 1)
        self.assertEqual(len(result["questions"]), 1)

    def test_teaching_evidence_reports_counterexamples_without_scoring_correctness(self):
        # This assessment is provenance only -- it must NOT compute TP/FP/FN
        # or judge correctness. That is wireless_ce_avatar's job, run later
        # against real issues/logs.
        result = assess_teaching_evidence(
            {"keywords": ["disconnect"], "exclusive": []},
            {
                "filter_stats": {"per_filter": [
                    {"text": "Disconnect", "hits": 2, "unique_hits": 2, "dropped": None},
                ]},
                "operation_journal": "#1 added include",
                "unreasoned_ops": [],
                "log_annotations": [
                    {"line_no": 1, "label": "counterexample", "text": "benign disconnect notice"},
                    {"line_no": 2, "label": "evidence", "text": "roam rejected"},
                ],
            },
        )
        self.assertEqual(result["status"], "assessed")
        self.assertEqual(result["external_validation"], "not_run")
        self.assertEqual(result["counterexample_count"], 1)
        self.assertNotIn("true_positive", result)
        self.assertFalse(any("TP" in check["note"] or "FP" in check["note"] for check in result["checks"]))

    def test_annotation_evidence_coverage_is_deterministic_not_llm_judged(self):
        # coverage.evidence must move purely from log_annotations, regardless
        # of whatever score the (fake) LLM itself returned for the other
        # three dimensions -- and must reward flagging a counterexample, not
        # just piling up "evidence" clicks.
        base = {
            "analysis": "n/a", "readiness": {"score": 10, "note": ""},
            "coverage": {"knowledge": 10, "scope": 10, "keywords": 10},
            "gaps": [], "validation": [], "ready_to_export": False, "questions": [],
            "ambiguity": {"requires_clarification": False, "divergent_behaviors": []},
        }
        no_annotations = analyze_round(FakeLlm(base), {}, 1)
        self.assertEqual(no_annotations["assessment"]["coverage"]["evidence"], 0)

        context = {"log_annotations": [
            {"line_no": 1, "label": "evidence", "text": "a"},
            {"line_no": 2, "label": "evidence", "text": "b"},
        ]}
        evidence_only = analyze_round(FakeLlm(base), context, 1)
        capped_without_counterexample = evidence_only["assessment"]["coverage"]["evidence"]
        self.assertGreater(capped_without_counterexample, 0)

        context_with_counterexample = {"log_annotations": context["log_annotations"] + [
            {"line_no": 3, "label": "counterexample", "text": "c"},
        ]}
        with_counterexample = analyze_round(FakeLlm(base), context_with_counterexample, 1)
        self.assertGreater(
            with_counterexample["assessment"]["coverage"]["evidence"],
            capped_without_counterexample,
        )

    def test_matched_keywords_for_line_resolves_by_filter_identity_not_history(self):
        # Unlike the old Step-history guess, this must work identically
        # whether the filter was typed in by hand or came in wholesale from
        # a loaded skill/.tat (i.e. with NO per-keyword operation at all).
        filters = [
            {"text": "Mcc", "excluding": False},
            {"text": "BG_SCAN", "excluding": False},
            {"text": "NoisyTag", "excluding": True},
        ]
        # A line co-firing two include filters credits BOTH (mirrors
        # compute_filter_stats' own overlap_count idea) -- and duplicates in
        # the matched-index list must not double-credit the same filter.
        result = matched_keywords_for_line(filters, [0, 1, 0])
        self.assertEqual(result, [
            {"text": "Mcc", "excluding": False},
            {"text": "BG_SCAN", "excluding": False},
        ])
        # No matched filters at all (e.g. the raw "Show all" view) -> nothing.
        self.assertEqual(matched_keywords_for_line(filters, []), [])
        # Out-of-range / bogus indices are ignored rather than raising.
        self.assertEqual(matched_keywords_for_line(filters, [99, "nope"]), [])

    def test_reset_clears_case_specific_teaching_state(self):
        state = WorkingState()
        state.filters = [{"text": "disconnect"}]
        state.chat_history = [{"role": "user", "content": "keep chat reset ownership external"}]
        state.operations = [{"seq": 1}]
        state.log_annotations = [{"line_no": 1, "label": "evidence", "text": "disconnect"}]
        state.reset_teaching_progress()
        self.assertEqual(state.filters, [{"text": "disconnect"}])
        self.assertEqual(state.operations, [])
        self.assertEqual(state.log_annotations, [])


if __name__ == "__main__":
    unittest.main()