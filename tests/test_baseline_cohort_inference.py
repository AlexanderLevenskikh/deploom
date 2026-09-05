from __future__ import annotations

import unittest

from baseline_cohort_inference import infer_baseline_cohort


class BaselineCohortInferenceTests(unittest.TestCase):
    def test_rollup_predicate_uses_reverse_consumers_and_vite_neighbors(self) -> None:
        direct = [
            "vite", "@vitejs/plugin-react", "vitest", "@storybook/builder-vite",
            "@sentry/vite-plugin", "vite-plugin-pwa", "react", "date-fns",
        ]
        graph = {
            "vite": {"@vitejs/plugin-react", "vitest", "@storybook/builder-vite", "@sentry/vite-plugin", "vite-plugin-pwa"},
            "@vitejs/plugin-react": {"vite"},
            "vitest": {"vite"},
            "@storybook/builder-vite": {"vite"},
            "@sentry/vite-plugin": {"vite"},
            "vite-plugin-pwa": {"vite"},
        }
        suggestion = infer_baseline_cohort(
            predicate="duplicate-type-universe:rollup",
            direct_packages=direct,
            subject_consumers={"rollup": {"vite"}},
            interaction_graph=graph,
            repeated_count=2,
        )
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual("vite-build", suggestion.cohort_id)
        self.assertIn("vite", suggestion.packages)
        self.assertIn("@vitejs/plugin-react", suggestion.packages)
        self.assertIn("vitest", suggestion.packages)
        self.assertNotIn("react", suggestion.packages)
        self.assertNotIn("date-fns", suggestion.packages)
        self.assertEqual("DIAGNOSTIC_HINT", suggestion.authority)
        self.assertGreaterEqual(suggestion.confidence, 0.85)

    def test_required_and_critical_are_not_suggested_for_deferral(self) -> None:
        suggestion = infer_baseline_cohort(
            predicate="duplicate-type-universe:rollup",
            direct_packages=["vite", "@vitejs/plugin-react", "vitest"],
            subject_consumers={"rollup": {"vite"}},
            interaction_graph={"vite": {"@vitejs/plugin-react", "vitest"}},
            policy_by_package={"vite": "required"},
            package_priority={"@vitejs/plugin-react": "critical"},
        )
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertNotIn("vite", suggestion.packages)
        self.assertNotIn("@vitejs/plugin-react", suggestion.packages)
        self.assertIn("vite", suggestion.blocked_packages)
        self.assertIn("@vitejs/plugin-react", suggestion.blocked_packages)
        self.assertIn("vitest", suggestion.packages)

    def test_high_security_is_visible_warning_but_user_may_choose(self) -> None:
        suggestion = infer_baseline_cohort(
            predicate="ts-module-resolution:@vitejs/plugin-react",
            direct_packages=["vite", "@vitejs/plugin-react"],
            focus_package="@vitejs/plugin-react",
            interaction_graph={"vite": {"@vitejs/plugin-react"}},
            package_priority={"vite": "high"},
        )
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertIn("vite", suggestion.warning_packages)

    def test_repeated_same_cohort_expands_one_boundary_shell(self) -> None:
        graph = {
            "vite": {"@vitejs/plugin-react", "@storybook/builder-vite"},
            "@vitejs/plugin-react": {"vite"},
            "@storybook/builder-vite": {"vite", "storybook"},
            "storybook": {"@storybook/builder-vite"},
        }
        suggestion = infer_baseline_cohort(
            predicate="duplicate-type-universe:rollup",
            direct_packages=graph,
            subject_consumers={"rollup": {"vite"}},
            interaction_graph=graph,
            previous_deferred=[{
                "id": "vite-build", "predicate": "duplicate-type-universe:rollup",
                "packages": ["vite", "@vitejs/plugin-react"],
            }],
            repeated_count=3,
        )
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual("vite-build", suggestion.expanded_from)
        self.assertIn("@storybook/builder-vite", suggestion.packages)

    def test_unknown_transitive_without_structural_path_does_not_invent_group(self) -> None:
        suggestion = infer_baseline_cohort(
            predicate="duplicate-type-universe:totally-unrelated",
            direct_packages=["react", "date-fns", "eslint"],
        )
        self.assertIsNone(suggestion)


if __name__ == "__main__":
    unittest.main()
