from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import dependency_live_roadmap_generator as generator


class IterativeCohortIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        generator._BASELINE_INTENT_CACHE_RAW = "<unset>"
        generator._BASELINE_INTENT_CACHE = {"schemaVersion": 1, "policies": {}}

    def test_default_execution_mode_is_fast(self) -> None:
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_EXECUTION_MODE": "", "DEPLOOM_BASELINE_INTENT_JSON": ""}, clear=False):
            generator._BASELINE_INTENT_CACHE_RAW = "<unset>"
            self.assertEqual("FAST", generator._baseline_execution_mode())

    def test_deferred_cohort_and_invocation_action_are_parsed_but_not_authority(self) -> None:
        payload = {
            "schemaVersion": 1,
            "policies": {"vite": "keep-current"},
            "executionMode": "FAST",
            "deferredCohorts": [{
                "id": "vite-build", "label": "Vite / build tooling",
                "packages": ["vite", "@vitejs/plugin-react"],
                "predicate": "duplicate-type-universe:rollup",
                "confidence": 0.9, "authority": "DIAGNOSTIC_HINT",
            }],
            "cohortAction": {
                "kind": "DEFER", "cohortId": "vite-build", "label": "Vite / build tooling",
                "packages": ["vite", "@vitejs/plugin-react"], "decisionId": "abc",
            },
        }
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_INTENT_JSON": json.dumps(payload)}, clear=False):
            generator._BASELINE_INTENT_CACHE_RAW = "<unset>"
            self.assertEqual("keep-current", generator._baseline_intent_policy("vite"))
            cohorts = generator._baseline_deferred_cohorts()
            self.assertEqual("vite-build", cohorts[0]["id"])
            action = generator._baseline_cohort_action()
            self.assertEqual("DEFER", action["kind"])


if __name__ == "__main__":
    unittest.main()
