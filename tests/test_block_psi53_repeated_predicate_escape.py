from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as generator
from block_psi_anytime import BaselineSearchMode
from block_v_predicate_search import (
    PredicateObservation,
    PredicateProbePolicy,
    predicate_activation_repeat_count,
)
from block_vf_active_search import (
    PROBE_OUTCOME_ABSENT,
    ProbeExecution,
    run_active_predicate_search,
)
from deploom_failure import classify_failure


class Psi53RepeatedPredicateActivationTests(unittest.TestCase):
    def test_same_version_independent_exact_failures_activate_search(self) -> None:
        package = "@vitejs/plugin-react"
        predicate = f"ts-module-resolution:{package}"
        observations = (
            PredicateObservation(package, "5.1.2", predicate, True, "assignment-a"),
            PredicateObservation(package, "5.1.2", predicate, True, "assignment-b"),
        )
        self.assertEqual(
            2,
            predicate_activation_repeat_count(
                package=package,
                predicate=predicate,
                observations=observations,
            ),
        )

    def test_duplicate_or_conflicting_point_does_not_inflate_activation(self) -> None:
        package = "@vitejs/plugin-react"
        predicate = f"ts-module-resolution:{package}"
        duplicate = (
            PredicateObservation(package, "5.1.2", predicate, True, "same"),
            PredicateObservation(package, "5.1.2", predicate, True, "same"),
        )
        self.assertEqual(
            1,
            predicate_activation_repeat_count(
                package=package,
                predicate=predicate,
                observations=duplicate,
            ),
        )
        conflicting = duplicate + (
            PredicateObservation(package, "5.1.2", predicate, False, "same"),
        )
        self.assertEqual(
            0,
            predicate_activation_repeat_count(
                package=package,
                predicate=predicate,
                observations=conflicting,
            ),
        )

    def test_active_search_targets_untried_direct_version_only_as_navigation(self) -> None:
        package = "@vitejs/plugin-react"
        predicate = f"ts-module-resolution:{package}"
        base = {
            package: "5.1.2",
            "vite": "4.3.9",
            "typescript": "5.9.3",
        }
        observations = (
            PredicateObservation(package, "5.1.2", predicate, True, "assignment-a"),
            PredicateObservation(package, "5.1.2", predicate, True, "assignment-b"),
        )
        executed = []

        def run_probe(version, assignment):
            executed.append((version, dict(assignment)))
            self.assertEqual("2.0.0", version)
            self.assertEqual("2.0.0", assignment[package])
            self.assertEqual("4.3.9", assignment["vite"])
            self.assertEqual("5.9.3", assignment["typescript"])
            return ProbeExecution(
                version=version,
                outcome=PROBE_OUTCOME_ABSENT,
                assignment_fingerprint="targeted-probe",
            )

        result = run_active_predicate_search(
            package=package,
            predicate=predicate,
            base_assignment=base,
            project_current_version="1.0.0",
            versions=("2.0.0", "5.1.2"),
            observations=observations,
            policy=PredicateProbePolicy(
                enabled=True,
                repeat_threshold=2,
                probe_budget=1,
            ),
            run_probe=run_probe,
        )
        self.assertTrue(result.activated)
        self.assertEqual(2, result.repeat_count)
        self.assertEqual("2.0.0", result.preferred_version)
        self.assertEqual(1, len(executed))
        self.assertEqual("5.1.2", base[package])


class Psi53PreIncumbentEscapeTests(unittest.TestCase):
    def test_auto_escape_is_conservative(self) -> None:
        self.assertFalse(
            generator._baseline_deep_search_allowed(
                has_incumbent=False,
                search_mode=BaselineSearchMode.AUTO,
                repeated_predicate_count=2,
            )
        )
        self.assertTrue(
            generator._baseline_deep_search_allowed(
                has_incumbent=False,
                search_mode=BaselineSearchMode.AUTO,
                repeated_predicate_count=3,
            )
        )
        self.assertTrue(
            generator._baseline_deep_search_allowed(
                has_incumbent=False,
                search_mode=BaselineSearchMode.EXHAUSTIVE,
                repeated_predicate_count=0,
            )
        )
        self.assertTrue(
            generator._baseline_deep_search_allowed(
                has_incumbent=True,
                search_mode=BaselineSearchMode.AUTO,
                repeated_predicate_count=0,
            )
        )

    def test_full_intent_can_supply_exhaustive_search_mode(self) -> None:
        payload = json.dumps({
            "schemaVersion": 1,
            "policies": {},
            "searchMode": "EXHAUSTIVE",
            "executionMode": "BACKGROUND",
            "deferredCohorts": [],
        })
        old_raw = generator._BASELINE_INTENT_CACHE_RAW
        old_cache = dict(generator._BASELINE_INTENT_CACHE)
        try:
            with patch.dict(
                os.environ,
                {
                    "DEPLOOM_BASELINE_INTENT_JSON": payload,
                    "DEPLOOM_BASELINE_SEARCH_MODE": "",
                },
                clear=False,
            ):
                generator._BASELINE_INTENT_CACHE_RAW = "<unset>"
                generator._BASELINE_INTENT_CACHE = {
                    "schemaVersion": 1,
                    "policies": {},
                }
                self.assertEqual(
                    BaselineSearchMode.EXHAUSTIVE,
                    generator._baseline_search_mode(),
                )
        finally:
            generator._BASELINE_INTENT_CACHE_RAW = old_raw
            generator._BASELINE_INTENT_CACHE = old_cache


class Psi53DesktopContracts(unittest.TestCase):
    def test_exhaustive_and_deferred_scope_survive_continuation(self) -> None:
        source = (ROOT / "desktop" / "electron" / "main.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "DEPLOOM_BASELINE_SEARCH_MODE: effectiveIntent.searchMode ?? 'AUTO'",
            source,
        )
        self.assertNotIn(
            "DEPLOOM_BASELINE_SEARCH_MODE: explicitIntent?.searchMode ?? 'AUTO'",
            source,
        )
        self.assertNotIn(
            "{ ...durable, decisionGrantIterations: 0, searchMode: 'AUTO' }",
            source,
        )
        self.assertIn(
            "deferredCohorts: effectiveIntent.deferredCohorts ?? []",
            source,
        )
        self.assertIn(
            "searchMode: effectiveIntent.searchMode ?? 'AUTO'",
            source,
        )

    def test_background_copy_is_autonomy_not_implicit_exhaustive(self) -> None:
        source = (
            ROOT / "desktop" / "src" / "components" / "BaselineIntentDialog.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("Режим Baseline", source)
        self.assertIn("Как выполнять", source)
        self.assertIn("Работать автономно", source)
        self.assertIn("Глубина поиска", source)
        self.assertIn("AUTO · Рекомендуется", source)
        self.assertIn("EXHAUSTIVE", source)
        self.assertIn("Готов ждать: исчерпывающий поиск в фоне", source)
        self.assertNotIn("Запустить автономный глубокий поиск", source)
        self.assertNotIn("Запустить глубокий поиск", source)


class Psi53FailureTaxonomyTests(unittest.TestCase):
    def test_hard_safety_limit_is_actionable_search_limit(self) -> None:
        category, retryability, recovery = classify_failure(
            RuntimeError(
                "BASELINE_VERIFICATION_HARD_SAFETY_LIMIT: "
                "reason=absolute-hard-safety-ceiling"
            )
        )
        self.assertEqual("SEARCH_LIMIT", category)
        self.assertEqual("user-action-required", retryability)
        self.assertIn("Do not retry unchanged", recovery)


if __name__ == "__main__":
    unittest.main()
