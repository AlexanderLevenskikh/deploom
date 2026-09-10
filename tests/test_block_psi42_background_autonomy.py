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
from block_psi_anytime import BaselineAnytimeState, BaselineSearchMode


class Psi42BackgroundAutonomyTests(unittest.TestCase):
    def test_fast_keeps_requested_auto_search_mode(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEPLOOM_BASELINE_EXECUTION_MODE": "FAST",
                "DEPLOOM_BASELINE_SEARCH_MODE": "AUTO",
            },
            clear=False,
        ):
            self.assertFalse(generator._baseline_background_autonomous())
            self.assertEqual(
                BaselineSearchMode.AUTO,
                generator._baseline_effective_search_mode(),
            )

    def test_background_keeps_requested_auto_search_depth(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEPLOOM_BASELINE_EXECUTION_MODE": "BACKGROUND",
                "DEPLOOM_BASELINE_SEARCH_MODE": "AUTO",
            },
            clear=False,
        ):
            self.assertTrue(generator._baseline_background_autonomous())
            self.assertEqual(
                BaselineSearchMode.AUTO,
                generator._baseline_effective_search_mode(),
            )
            state = BaselineAnytimeState(
                search_mode=generator._baseline_effective_search_mode()
            )
            self.assertFalse(state.exhaustive_authorized)

    def test_background_keeps_bounded_request_too(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEPLOOM_BASELINE_EXECUTION_MODE": "BACKGROUND",
                "DEPLOOM_BASELINE_SEARCH_MODE": "BOUNDED_IMPROVEMENT",
            },
            clear=False,
        ):
            self.assertEqual(
                BaselineSearchMode.BOUNDED_IMPROVEMENT,
                generator._baseline_effective_search_mode(),
            )

    def test_explicit_exhaustive_remains_exhaustive(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEPLOOM_BASELINE_EXECUTION_MODE": "BACKGROUND",
                "DEPLOOM_BASELINE_SEARCH_MODE": "EXHAUSTIVE",
            },
            clear=False,
        ):
            self.assertEqual(
                BaselineSearchMode.EXHAUSTIVE,
                generator._baseline_effective_search_mode(),
            )

    def test_generator_source_has_zero_dialog_background_contract(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("PSI5_BACKGROUND_AUTONOMOUS", source)
        self.assertIn(
            "and not _baseline_background_autonomous()",
            source,
        )
        self.assertGreaterEqual(
            source.count("not _baseline_background_autonomous()"),
            2,
        )
        self.assertIn(
            "anytime.exhaustive_authorized = True",
            source,
        )
        self.assertIn(
            "time-to-first-verified-usable-result",
            source,
        )

    def test_background_does_not_relabel_cohort_as_user_policy(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        marker_at = source.index("BLOCK_PSI42_BACKGROUND_AUTONOMY_V1")
        helper_region = source[marker_at: marker_at + 2400]
        self.assertNotIn('"keep-current"', helper_region)
        self.assertNotIn("_apply_baseline_intent_scope(", helper_region)
        self.assertNotIn("_baseline_cohort_action(", helper_region)
        self.assertNotIn("cohortAction", helper_region)
        self.assertNotIn("exclusion_source", helper_region)
        self.assertNotIn("scope_excluded =", helper_region)

    def test_desktop_does_not_force_background_to_exhaustive(self) -> None:
        source = (
            ROOT / "desktop" / "src" / "components" / "BaselineIntentDialog.tsx"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "normalizedExecutionMode(nextExecutionMode) === 'BACKGROUND' ? 'EXHAUSTIVE' : searchMode",
            source,
        )
        self.assertIn("searchMode,", source)
        self.assertIn("Автономно", source)
        self.assertIn("controlMode === 'AUTONOMOUS'", source)
        self.assertNotIn("setExecutionMode", source)
        self.assertIn("Технически: продолжить EXHAUSTIVE автономно", source)
        self.assertNotIn("Запустить автономный глубокий поиск", source)

    def test_draft_prompt_export_is_external_agent_handoff(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("BLOCK_PSI42_DRAFT_EXTERNAL_HANDOFF_V1", source)
        self.assertIn("verificationStatus: 'NOT_VERIFIED'", source)
        self.assertIn("authority: 'PLANNING_ONLY'", source)
        self.assertIn("compatibility: 'UNKNOWN'", source)
        self.assertIn("Draft prompt для внешнего агента", source)
        self.assertIn("formatSelect.value = 'compact'", source)

    def test_verified_prompt_still_requires_real_proof_envelope(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "if (!draft) throw new Error(`PROVEN_DEPENDENCY_PROOF_MISSING: ${project}/${mode}`)",
            source,
        )
        self.assertIn(
            "No DepLoom ProofEnvelope exists for Draft Baseline",
            source,
        )

    def test_draft_modal_explains_external_agent_and_context_formats(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "Промпт для внешнего агента, если вы хотите продолжить вне DepLoom",
            source,
        )
        self.assertIn(
            "Компактный формат рекомендуется для небольшого контекстного окна",
            source,
        )
        self.assertIn(
            '<option value="compact">Компактный — для моделей с малым контекстом</option>',
            source,
        )
        self.assertIn(
            '<option value="full">Полный — диагностический</option>',
            source,
        )


if __name__ == "__main__":
    unittest.main()
