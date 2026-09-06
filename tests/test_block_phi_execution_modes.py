from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as generator
from block_psi_anytime import BaselineCompletionStatus, BaselineSearchMode


class BlockPhiExecutionModeTests(unittest.TestCase):
    def test_fast_mode(self):
        with patch.dict(
            os.environ,
            {
                "DEPLOOM_BASELINE_EXECUTION_MODE": "FAST",
                "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS": "",
                "DEPLOOM_BASELINE_SEARCH_MODE": "AUTO",
            },
            clear=False,
        ):
            self.assertEqual(generator._baseline_max_expensive_attempts(8), 2)
            self.assertTrue(
                generator._baseline_preseal_screening_enabled(has_incumbent=False)
            )
            self.assertFalse(
                generator._baseline_deep_search_allowed(has_incumbent=False)
            )
            self.assertTrue(
                generator._baseline_deep_search_allowed(has_incumbent=True)
            )

    def test_autopilot_mode(self):
        with patch.dict(
            os.environ,
            {
                "DEPLOOM_BASELINE_EXECUTION_MODE": "AUTOPILOT",
                "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS": "",
                "DEPLOOM_BASELINE_SEARCH_MODE": "AUTO",
            },
            clear=False,
        ):
            self.assertEqual(generator._baseline_max_expensive_attempts(8), 4)
            self.assertTrue(
                generator._baseline_preseal_screening_enabled(has_incumbent=False)
            )
            self.assertFalse(
                generator._baseline_deep_search_allowed(has_incumbent=False)
            )

    def test_background_auto_is_incumbent_first_then_allows_improvement(self):
        # Ψ.5 separates unattended execution from search-depth authority.
        # BACKGROUND/AUTO must not spend mandatory deep-localization cost before
        # the first verified incumbent, but can improve it afterwards.
        with patch.dict(
            os.environ,
            {
                "DEPLOOM_BASELINE_EXECUTION_MODE": "BACKGROUND",
                "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS": "",
                "DEPLOOM_BASELINE_SEARCH_MODE": "AUTO",
            },
            clear=False,
        ):
            self.assertEqual(generator._baseline_max_expensive_attempts(8), 8)
            self.assertFalse(
                generator._baseline_preseal_screening_enabled(has_incumbent=False)
            )
            self.assertFalse(
                generator._baseline_deep_search_allowed(has_incumbent=False)
            )
            self.assertTrue(
                generator._baseline_deep_search_allowed(has_incumbent=True)
            )

    def test_explicit_exhaustive_overrides_pre_incumbent_deferral(self):
        for execution_mode in ("FAST", "BACKGROUND"):
            with self.subTest(execution_mode=execution_mode):
                with patch.dict(
                    os.environ,
                    {
                        "DEPLOOM_BASELINE_EXECUTION_MODE": execution_mode,
                        "DEPLOOM_BASELINE_SEARCH_MODE": "EXHAUSTIVE",
                    },
                    clear=False,
                ):
                    self.assertFalse(
                        generator._baseline_preseal_screening_enabled(
                            has_incumbent=False
                        )
                    )
                    self.assertTrue(
                        generator._baseline_deep_search_allowed(
                            has_incumbent=False,
                            search_mode=BaselineSearchMode.EXHAUSTIVE,
                        )
                    )

    def test_partial_scope_status_is_explicit(self):
        self.assertEqual(
            BaselineCompletionStatus.VERIFIED_PARTIAL_SCOPE.value,
            "VERIFIED_PARTIAL_SCOPE",
        )


if __name__ == "__main__":
    unittest.main()
