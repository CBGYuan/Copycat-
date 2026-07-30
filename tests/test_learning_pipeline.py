import json
import unittest

from blueprints.learning.learning_routes import _filter_signature, _baseline_delta
from blueprints.chatbot.chatbot_routes import _parse_chat_response
from services.learning_service import (analyze_baseline, analyze_round, assess_teaching_evidence,
                                       clarify_divergence)
from services.session_store import WorkingState
from services import decision_ledger
from utils import divergence, operation_journal
from utils.tat_parser import matched_keywords_for_line


class FakeLlm:
    def __init__(self, response):
        self.response = response
        # Routes echo token usage back to the UI, so the double needs these
        # to stand in for a real LLM_helper.
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.session_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self.is_ready = True

    def chat(self, **_kwargs):
        return json.dumps(self.response)


class LearningPipelineTests(unittest.TestCase):
    def test_chat_divergence_parser_preserves_choice_questions(self):
        raw = json.dumps({
            "reply": "That is new expert knowledge.",
            "clarification": {
                "detected": True,
                "basis": "both",
                "summary": "The threshold differs from the baseline and loaded skill.",
                "question": "Which threshold applies?",
                "type": "choice",
                "options": ["20%", "30%"],
                "recommended_answer": "20%",
                "recommendation_reason": "It matches the measured grade delta.",
            },
        })
        reply, question = _parse_chat_response(raw, expect_structured=True)
        self.assertEqual(reply, "That is new expert knowledge.")
        self.assertEqual(question["type"], "choice")
        self.assertEqual(question["basis"], "both")
        self.assertEqual(question["options"], ["20%", "30%"])
        self.assertEqual(question["recommended_answer"], "20%")

    def test_chat_divergence_parser_falls_back_to_plain_reply(self):
        reply, question = _parse_chat_response("ordinary reply", expect_structured=True)
        self.assertEqual(reply, "ordinary reply")
        self.assertIsNone(question)

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
        state.decision_ledger = [{"id": "D1", "status": "open"}]
        state.reset_teaching_progress()
        self.assertEqual(state.filters, [{"text": "disconnect"}])
        self.assertEqual(state.operations, [])
        self.assertEqual(state.log_annotations, [])
        self.assertEqual(state.decision_ledger, [])

    def test_decision_ledger_is_session_sidecar_and_resolves_answers(self):
        state = WorkingState()
        state.interview_mode = "grill"
        item = decision_ledger.record_question(
            state,
            source="chat",
            question="Is this exclusion global or scenario-specific?",
            qtype="choice",
            options=["Always", "Only here"],
            recommended_answer="Only here",
        )
        self.assertTrue(item["blocking"])
        decision_ledger.resolve(state, item["id"], "Only here")
        payload = decision_ledger.payload(state)
        self.assertEqual(payload["resolved"], 1)
        self.assertEqual(payload["blocking"], 0)

        spec = decision_ledger.build_skill_spec({
            "description": "A bounded connection failure.",
            "keywords": ["ASSOC_RSP"],
            "exclusive": ["periodic"],
            "expert_rules": "1. If ASSOC_RSP fails, report the status.",
            "teaching_evidence": {"labeled_examples": 2, "counterexample_count": 1},
        }, state)
        self.assertEqual(
            spec["avatar_fields"],
            ["name", "description", "keywords", "exclusive", "expert_rules"],
        )
        self.assertEqual(spec["resolved_decisions"][0]["answer"], "Only here")


class BaselineTests(unittest.TestCase):
    """The baseline read is the null hypothesis every later engineer action is
    diffed against, so the two properties that make that diff trustworthy get
    their own tests: it must not commit to keywords that aren't really in the
    filter, and it must not be considered stale just because the engineer
    toggled something."""

    def _response(self, **overrides):
        base = {
            "analysis": "Default filter targets association failures.",
            "expected_scenario": "Association failure during connect.",
            "expected_key_keywords": [{"text": "assoc", "why": "412 hits, co-fires with auth"}],
            "expected_noise_keywords": [{"text": "Mcc", "why": "periodic housekeeping"}],
            "expected_issue_time_hint": "The first assoc-reject line.",
            "open_unknowns": ["Whether the customer's complaint is roam or initial connect."],
            "readiness": {"score": 15, "note": "Filter only, nothing taught yet."},
            "coverage": {"knowledge": 10, "scope": 20, "keywords": 30},
            "gaps": ["Why these keywords together"],
            "validation": [],
            "ready_to_export": False,
        }
        base.update(overrides)
        return base

    def _context(self, *texts):
        return {"filter_stats": {"per_filter": [{"text": t, "excluding": False, "hits": 1} for t in texts]}}

    def test_baseline_drops_predicted_keywords_absent_from_the_filter(self):
        # "EAPOL" is not in the filter set — committing to it would create a
        # disagreement the engineer could never resolve, since they cannot
        # restore a keyword they never had.
        response = self._response(expected_key_keywords=[
            {"text": "assoc", "why": "real"},
            {"text": "EAPOL", "why": "invented"},
        ])
        result = analyze_baseline(FakeLlm(response), self._context("assoc", "Mcc"))
        self.assertEqual([k["text"] for k in result["expected_key_keywords"]], ["assoc"])

    def test_baseline_snaps_predictions_back_to_the_filters_own_spelling(self):
        response = self._response(expected_key_keywords=[{"text": "ASSOC", "why": "case drift"}])
        result = analyze_baseline(FakeLlm(response), self._context("assoc"))
        self.assertEqual([k["text"] for k in result["expected_key_keywords"]], ["assoc"])

    def test_baseline_records_open_unknowns_for_the_omission_split(self):
        result = analyze_baseline(FakeLlm(self._response()), self._context("assoc", "Mcc"))
        self.assertTrue(result["open_unknowns"])
        self.assertEqual(result["assessment"]["readiness"]["score"], 15)

    def test_filter_signature_is_stable_when_the_engineer_toggles_a_checkbox(self):
        # Toggling is the action the baseline exists to be compared against;
        # if it invalidated the baseline we would re-read against the
        # engineer's own edit and lose the comparison entirely.
        state = WorkingState()
        state.filters = [
            {"text": "assoc", "excluding": False, "enabled": True},
            {"text": "Mcc", "excluding": True, "enabled": True},
        ]
        before = _filter_signature(state)
        state.filters[0]["enabled"] = False
        self.assertEqual(before, _filter_signature(state))

        # Adding/removing keywords after the read is the teaching delta, so it
        # must stay comparable to the same baseline.
        state.filters = [{"text": "roam", "excluding": False, "enabled": True}]
        self.assertEqual(before, _filter_signature(state))

        # A different capture is genuinely new evidence and needs a new read.
        state.log_path = "next_capture.log"
        self.assertNotEqual(before, _filter_signature(state))

        state.log_path = ""
        self.assertEqual(before, _filter_signature(state))
        state.prior_knowledge = True
        self.assertNotEqual(before, _filter_signature(state))

    def test_baseline_survives_clear_because_it_belongs_to_the_filter_set(self):
        state = WorkingState()
        state.baseline = {"expected_scenario": "Association failure."}
        state.baseline_filter_sig = "sig"
        state.reset_teaching_progress()
        self.assertEqual(state.baseline, {"expected_scenario": "Association failure."})
        self.assertEqual(state.baseline_filter_sig, "sig")

    def test_baseline_update_delta_preserves_what_changed(self):
        before = {
            "expected_scenario": "Connect failure.",
            "expected_key_keywords": [{"text": "AUTH_REQ"}],
            "expected_noise_keywords": [{"text": "Mcc"}],
            "open_unknowns": ["Whether this is a roam."],
        }
        after = {
            "expected_scenario": "Roam authentication failure.",
            "expected_key_keywords": [{"text": "AUTH_REQ"}, {"text": "TASK ROAM"}],
            "expected_noise_keywords": [],
            "open_unknowns": [],
        }
        delta = _baseline_delta(before, after)
        self.assertTrue(delta["scenario_changed"])
        self.assertEqual(delta["key_added"], ["TASK ROAM"])
        self.assertEqual(delta["noise_removed"], ["Mcc"])
        self.assertEqual(delta["unknowns_resolved"], ["Whether this is a roam."])


class DivergenceTests(unittest.TestCase):
    """The gate that decides whether an interruption is justified at all. The
    ordering it enforces — measured materiality first, baseline opinion only
    to characterise — is what keeps a language model's shifting read from
    driving when the engineer gets interrupted."""

    def _state(self, ops, baseline=None, focus=""):
        state = WorkingState()
        state.operations = ops
        state.baseline = baseline if baseline is not None else {
            "expected_key_keywords": [{"text": "DisconnectIndication", "why": "terminal event"}],
            "expected_noise_keywords": [{"text": "Mcc", "why": "periodic housekeeping"}],
            "expected_issue_time_hint": "The first deauth line.",
        }
        state.focus_center_iso = focus
        return state

    def _op(self, seq, action, text, reason="", effect=None):
        return {"seq": seq, "action": action, "text": text, "reason": reason,
                "effect": effect if effect is not None else {"unique_hits": 40},
                "excluding": False, "red_flags": []}

    def test_cutting_a_keyword_the_baseline_defended_is_a_contradiction(self):
        state = self._state([self._op(1, "toggle_off", "DisconnectIndication")])
        result = divergence.detect(state)
        self.assertEqual([c["text"] for c in result["contradictions"]], ["DisconnectIndication"])
        self.assertEqual(result["contradictions"][0]["baseline_stance"], "key")
        self.assertEqual(result["omissions"], [])

    def test_promoting_a_keyword_the_baseline_dismissed_is_a_contradiction(self):
        state = self._state([self._op(1, "add_include", "Mcc")])
        result = divergence.detect(state)
        self.assertEqual([c["text"] for c in result["contradictions"]], ["Mcc"])
        self.assertEqual(result["contradictions"][0]["baseline_stance"], "noise")

    def test_agreeing_with_the_baseline_is_not_a_divergence_at_all(self):
        # Engineer cuts what the baseline already called noise — the read was
        # confirmed. Interrupting here would be pure noise.
        state = self._state([self._op(1, "add_exclude", "Mcc")])
        result = divergence.detect(state)
        self.assertEqual(result["contradictions"], [])
        self.assertEqual(result["omissions"], [])

    def test_keyword_the_baseline_had_no_view_on_is_an_omission_not_a_question(self):
        state = self._state([self._op(1, "add_include", "BG_SCAN")])
        result = divergence.detect(state)
        self.assertEqual(result["contradictions"], [])
        self.assertEqual([o["text"] for o in result["omissions"]], ["BG_SCAN"])

    def test_immaterial_and_already_explained_edits_never_surface(self):
        state = self._state([
            # No measured effect -> not material.
            self._op(1, "add_include", "Trivial", effect={}),
            # Material but the engineer already explained it.
            self._op(2, "toggle_off", "DisconnectIndication", reason="not relevant here"),
        ])
        result = divergence.detect(state)
        self.assertEqual(result["contradictions"], [])
        self.assertEqual(result["omissions"], [])

    def test_without_a_baseline_material_edits_fall_back_to_omissions(self):
        state = self._state([self._op(1, "toggle_off", "DisconnectIndication")], baseline={})
        result = divergence.detect(state)
        self.assertFalse(result["has_baseline"])
        self.assertEqual(result["contradictions"], [])
        self.assertEqual([o["text"] for o in result["omissions"]], ["DisconnectIndication"])

    def test_edits_that_built_the_baselined_filter_set_are_not_divergences(self):
        """Regression: the adds that assembled the filter set are what the
        baseline READ, so scoring them against it reported a .tat's own
        keywords as contradicting the read of those same keywords."""
        state = self._state([
            self._op(1, "add_include", "Mcc"),                  # pre-baseline
            self._op(2, "toggle_off", "DisconnectIndication"),  # post-baseline
        ])
        state.baseline_op_seq = 1
        result = divergence.detect(state)
        self.assertEqual([c["text"] for c in result["contradictions"]], ["DisconnectIndication"])
        self.assertEqual(result["omissions"], [])

    def test_disabling_a_hit_carrying_keyword_counts_as_material(self):
        """Regression: a disabled filter has no unique_hits (only enabled ones
        do), so toggle_off fell through the materiality gate entirely — losing
        the single most important 'the engineer disagrees' action."""
        state = self._state([
            self._op(1, "toggle_off", "DisconnectIndication", effect={"hits": 47}),
        ])
        self.assertEqual([c["text"] for c in divergence.detect(state)["contradictions"]],
                         ["DisconnectIndication"])

    def test_disabling_a_keyword_that_matched_nothing_stays_immaterial(self):
        state = self._state([
            self._op(1, "toggle_off", "DisconnectIndication", effect={"hits": 0}),
        ])
        result = divergence.detect(state)
        self.assertEqual(result["contradictions"], [])
        self.assertEqual(result["omissions"], [])

    def test_focus_is_its_own_signal_carrying_the_baselines_locating_hint(self):
        state = self._state([], focus="04/20/2026-09:41:52.610")
        result = divergence.detect(state)
        self.assertEqual(result["focus"]["center"], "04/20/2026-09:41:52.610")
        self.assertEqual(result["focus"]["baseline_hint"], "The first deauth line.")
        # and it must not have leaked into the keyword comparison
        self.assertEqual(result["contradictions"], [])
        self.assertEqual(result["omissions"], [])


class FocusEffectAttributionTests(unittest.TestCase):
    def test_survivor_delta_is_withheld_when_the_focus_window_moved(self):
        """A focused run and an unfocused run count different populations.
        Diffing them would hand the whole narrowing to whichever edit happened
        to be pending, which then drives materiality downstream."""
        state = WorkingState()
        state.prev_survivors = 40000
        state.prev_focus_sig = "|5"          # previous run was unfocused
        state.focus_center_iso = "04/20/2026-09:41:52.610"   # this one is focused
        state.operations = [{"seq": 1, "action": "add_include", "text": "assoc", "reason": "",
                             "effect": None, "pending": True, "excluding": False, "red_flags": []}]
        operation_journal.annotate_effects(state, {
            "surviving_count": 120,
            "per_filter": [{"text": "assoc", "hits": 12, "unique_hits": 12}],
            "co_occurrence": [],
        })
        self.assertIsNone(state.operations[0]["effect"]["survivor_delta"])
        # Per-keyword figures are measured within the one run, so they stay.
        self.assertEqual(state.operations[0]["effect"]["unique_hits"], 12)

    def test_survivor_delta_is_computed_when_the_window_did_not_move(self):
        state = WorkingState()
        state.prev_survivors = 200
        state.prev_focus_sig = "|5"
        state.operations = [{"seq": 1, "action": "add_include", "text": "assoc", "reason": "",
                             "effect": None, "pending": True, "excluding": False, "red_flags": []}]
        operation_journal.annotate_effects(state, {
            "surviving_count": 240,
            "per_filter": [{"text": "assoc", "hits": 40, "unique_hits": 40}],
            "co_occurrence": [],
        })
        self.assertEqual(state.operations[0]["effect"]["survivor_delta"], 40)


class ClarifyTests(unittest.TestCase):
    QUESTION = {
        "question": {"question": "Is 'connect attempt' always redundant here, or only when DHCP renews cleanly?",
                     "type": "choice",
                     "options": ["Always drop it", "Drop only when DHCP renews cleanly"]},
        "captures": "Whether the exclusion generalises or is scenario-bound.",
    }

    def test_contradiction_prompt_carries_both_readings_and_the_measured_effect(self):
        """The question can only discriminate if the model is actually given
        the two competing readings plus the measurement that justified
        interrupting — so assert they reach the prompt."""
        seen = {}

        class Spy(FakeLlm):
            def chat(self, **kwargs):
                seen.update(kwargs)
                return json.dumps(ClarifyTests.QUESTION)

        result = clarify_divergence(Spy(None), {
            "kind": "contradiction", "domain": "wifi", "text": "connect attempt",
            "action_phrase": "disabled", "effect_phrase": "720 hits, survivors -720",
            "baseline_stance": "key", "baseline_why": "720 unique hits, the initiating event",
        })
        prompt = seen["messages"][0]["content"]
        self.assertIn("connect attempt", prompt)
        self.assertIn("disabled", prompt)
        self.assertIn("720 hits, survivors -720", prompt)      # the measured justification
        self.assertIn("the initiating event", prompt)          # the baseline's own reading
        self.assertIn("load-bearing", prompt)                  # its stance, in words
        self.assertEqual(result["question"]["type"], "choice")

    def test_focus_prompt_contrasts_against_the_baselines_own_locating_hint(self):
        seen = {}

        class Spy(FakeLlm):
            def chat(self, **kwargs):
                seen.update(kwargs)
                return json.dumps(ClarifyTests.QUESTION)

        clarify_divergence(Spy(None), {
            "kind": "focus", "domain": "wifi", "center": "04/20/2026-09:41:52.610",
            "window_min": 5, "baseline_hint": "The first deauth line.",
        })
        prompt = seen["messages"][0]["content"]
        self.assertIn("04/20/2026-09:41:52.610", prompt)
        self.assertIn("The first deauth line.", prompt)

    def test_unusable_llm_output_yields_no_question_rather_than_a_generic_one(self):
        # A non-discriminating fallback question is precisely what the
        # ambiguity gate exists to prevent, so None is the correct outcome.
        self.assertIsNone(clarify_divergence(FakeLlm({"question": None}), {
            "kind": "contradiction", "domain": "wifi", "text": "x",
            "action_phrase": "disabled", "effect_phrase": "", "baseline_stance": "key",
            "baseline_why": "",
        }))


class ClarifyRoutePolicyTests(unittest.TestCase):
    """The routing policy: which divergence (if any) becomes a question."""

    def setUp(self):
        from app import create_app
        from configs.global_configs import app_config
        self.app = create_app()
        self._real_llm = app_config.llm_helper
        app_config.llm_helper = FakeLlm(ClarifyTests.QUESTION)
        app_config.llm_helper.is_ready = True
        self._cfg = app_config

    def tearDown(self):
        self._cfg.llm_helper = self._real_llm

    def _seed(self, client, ops, baseline):
        with client.session_transaction():
            pass
        with self.app.test_request_context():
            pass
        # Drive state through the store the same way a request would.
        from services import session_store
        with client.application.test_request_context():
            pass
        return ops, baseline

    def test_an_omission_alone_never_produces_a_question(self):
        """Omissions have no competing reading to discriminate between, so
        asking would be low-information-gain prompting (GATE). They belong to
        the Steps panel's passive hint instead."""
        from services import session_store
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["wsid"] = "omission-only"
        session_store._STORE["omission-only"] = st = WorkingState()
        st.log_domain = "wifi"
        st.baseline = {"expected_key_keywords": [{"text": "assoc", "why": "anchor"}],
                       "expected_noise_keywords": []}
        st.baseline_op_seq = 0
        st.operations = [{"seq": 1, "action": "add_include", "text": "BG_SCAN", "reason": "",
                          "effect": {"unique_hits": 40}, "excluding": False, "red_flags": []}]
        resp = client.post("/learning/clarify", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["question"])

    def test_a_contradiction_produces_one_question_and_is_not_re_asked(self):
        from services import session_store
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["wsid"] = "contradiction"
        session_store._STORE["contradiction"] = st = WorkingState()
        st.log_domain = "wifi"
        st.baseline = {"expected_key_keywords": [{"text": "connect attempt", "why": "initiating event"}],
                       "expected_noise_keywords": []}
        st.baseline_op_seq = 0
        st.operations = [{"seq": 1, "action": "toggle_off", "text": "connect attempt", "reason": "",
                          "effect": {"hits": 720}, "excluding": False, "red_flags": []}]

        first = client.post("/learning/clarify", json={}).get_json()
        self.assertIsNotNone(first["question"])
        self.assertEqual(first["kind"], "contradiction")
        self.assertEqual(first["seq"], 1)

        # Skipping (not answering) must not re-prompt: being asked and
        # declining is itself a decision.
        second = client.post("/learning/clarify", json={}).get_json()
        self.assertIsNone(second["question"])


if __name__ == "__main__":
    unittest.main()
