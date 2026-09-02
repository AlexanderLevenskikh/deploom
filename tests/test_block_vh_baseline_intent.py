import json
import os
import unittest
from unittest.mock import patch

import dependency_live_roadmap_generator as gen


class BlockVHBaselineIntentTests(unittest.TestCase):
    def test_intent_parser_accepts_only_known_policies(self):
        raw = json.dumps({
            "schemaVersion": 1,
            "policies": {"a": "keep-current", "b": "required", "c": "wat"},
        })
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_INTENT_JSON": raw}, clear=False):
            self.assertEqual("keep-current", gen._baseline_intent_policy("a"))
            self.assertEqual("required", gen._baseline_intent_policy("b"))
            self.assertEqual("auto", gen._baseline_intent_policy("c"))
            self.assertEqual("auto", gen._baseline_intent_policy("missing"))

    def test_focus_requires_three_distinct_confirmed_unary_versions(self):
        learned = [
            {"pkg": "1.0.0"},
            {"pkg": "2.0.0"},
            {"pkg": "3.0.0"},
            {"other": "1.0.0", "pkg": "4.0.0"},
        ]
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_INTENT_JSON": '{"schemaVersion":1,"policies":{}}'}, clear=False):
            focus = gen._baseline_human_decision_focus(learned, {"pkg": "0.9.0"}, min_confirmed=3)
        self.assertIsNotNone(focus)
        self.assertEqual("pkg", focus["package"])
        self.assertEqual(["1.0.0", "2.0.0", "3.0.0"], focus["failedVersions"])

    def test_required_package_is_not_suggested_for_keep_current_prompt(self):
        learned = [{"pkg": "1.0.0"}, {"pkg": "2.0.0"}, {"pkg": "3.0.0"}]
        raw = '{"schemaVersion":1,"policies":{"pkg":"required"}}'
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_INTENT_JSON": raw}, clear=False):
            self.assertIsNone(gen._baseline_human_decision_focus(learned, {"pkg": "0.9.0"}, min_confirmed=3))

    def test_keep_current_is_outside_current_baseline_health_scope(self):
        from types import SimpleNamespace
        row = SimpleNamespace(name="eslint-plugin-sonarjs", scope_excluded=False, exclusion_reason="", exclusion_source="")
        raw = '{"schemaVersion":1,"policies":{"eslint-plugin-sonarjs":"keep-current"}}'
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_INTENT_JSON": raw}, clear=False):
            gen._BASELINE_INTENT_CACHE_RAW = "<unset>"
            gen._apply_baseline_intent_scope({"demo-project": [row]})
        self.assertTrue(row.scope_excluded)
        self.assertEqual("baseline-intent", row.exclusion_source)
        self.assertIn("Baseline", row.exclusion_reason)

    def test_user_continuation_credit_is_not_mislabeled_as_proof_credit(self):
        budget = gen.BaselineLivenessBudget(base_iterations=8, max_learning_extensions=16)
        budget.certified_extensions = 8
        self.assertEqual(8, budget.allowed_iterations)
        self.assertEqual(8, budget.grant_user_extensions(8))
        self.assertEqual(16, budget.allowed_iterations)
        snapshot = budget.snapshot(learned_constraints=0)
        self.assertEqual(8, snapshot["certifiedExtensions"])
        self.assertEqual(8, snapshot["userExtensions"])
        self.assertEqual(24, snapshot["hardIterations"])


if __name__ == '__main__':
    unittest.main()
