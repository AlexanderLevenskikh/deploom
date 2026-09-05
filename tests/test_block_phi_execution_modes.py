from __future__ import annotations
import os
import unittest
from unittest.mock import patch
import dependency_live_roadmap_generator as generator
from block_psi_anytime import BaselineCompletionStatus, BaselineSearchMode

class BlockPhiExecutionModeTests(unittest.TestCase):
    def test_fast_mode(self):
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_EXECUTION_MODE":"FAST", "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS":"", "DEPLOOM_BASELINE_SEARCH_MODE":"AUTO"}, clear=False):
            self.assertEqual(generator._baseline_max_expensive_attempts(8), 2)
            self.assertTrue(generator._baseline_preseal_screening_enabled(has_incumbent=False))
            self.assertFalse(generator._baseline_deep_search_allowed(has_incumbent=False))
            self.assertTrue(generator._baseline_deep_search_allowed(has_incumbent=True))

    def test_autopilot_mode(self):
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_EXECUTION_MODE":"AUTOPILOT", "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS":"", "DEPLOOM_BASELINE_SEARCH_MODE":"AUTO"}, clear=False):
            self.assertEqual(generator._baseline_max_expensive_attempts(8), 4)
            self.assertTrue(generator._baseline_preseal_screening_enabled(has_incumbent=False))
            self.assertFalse(generator._baseline_deep_search_allowed(has_incumbent=False))

    def test_background_mode(self):
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_EXECUTION_MODE":"BACKGROUND", "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS":"", "DEPLOOM_BASELINE_SEARCH_MODE":"AUTO"}, clear=False):
            self.assertEqual(generator._baseline_max_expensive_attempts(8), 8)
            self.assertFalse(generator._baseline_preseal_screening_enabled(has_incumbent=False))
            self.assertTrue(generator._baseline_deep_search_allowed(has_incumbent=False))

    def test_exhaustive_overrides_fast_deferral(self):
        with patch.dict(os.environ, {"DEPLOOM_BASELINE_EXECUTION_MODE":"FAST", "DEPLOOM_BASELINE_SEARCH_MODE":"EXHAUSTIVE"}, clear=False):
            self.assertFalse(generator._baseline_preseal_screening_enabled(has_incumbent=False))
            self.assertTrue(generator._baseline_deep_search_allowed(has_incumbent=False, search_mode=BaselineSearchMode.EXHAUSTIVE))

    def test_partial_scope_status_is_explicit(self):
        self.assertEqual(BaselineCompletionStatus.VERIFIED_PARTIAL_SCOPE.value, "VERIFIED_PARTIAL_SCOPE")

if __name__ == "__main__": unittest.main()
