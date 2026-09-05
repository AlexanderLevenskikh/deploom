from __future__ import annotations

import unittest

from baseline_cohort_inference import infer_baseline_cohort


class BaselineCohortInferenceTests(unittest.TestCase):
    def test_rollup_type_universe_maps_to_actionable_vite_direct_cohort(self) -> None:
        suggestion = infer_baseline_cohort(
            predicate="duplicate-type-universe:rollup",
            direct_packages=[
                "vite", "@vitejs/plugin-react", "@sentry/vite-plugin",
                "@storybook/builder-vite", "vite-plugin-pwa", "vitest",
                "unplugin-detect-duplicated-deps", "react", "typescript", "eslint",
            ],
            repeated_count=2,
        )
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual("vite-build", suggestion.cohort_id)
        self.assertEqual("DIAGNOSTIC_HINT", suggestion.authority)
        self.assertIn("vite", suggestion.packages)
        self.assertIn("@vitejs/plugin-react", suggestion.packages)
        self.assertIn("vitest", suggestion.packages)
        self.assertNotIn("react", suggestion.packages)
        self.assertGreaterEqual(suggestion.confidence, 0.85)

    def test_vite_module_resolution_maps_to_same_cohort(self) -> None:
        suggestion = infer_baseline_cohort(
            predicate="ts-module-resolution:@vitejs/plugin-react",
            direct_packages=["vite", "@vitejs/plugin-react", "vite-plugin-pwa", "date-fns"],
        )
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual("vite-build", suggestion.cohort_id)
        self.assertIn("@vitejs/plugin-react", suggestion.packages)
        self.assertNotIn("date-fns", suggestion.packages)

    def test_required_package_is_never_silently_deferred(self) -> None:
        suggestion = infer_baseline_cohort(
            predicate="duplicate-type-universe:rollup",
            direct_packages=["vite", "@vitejs/plugin-react", "vitest"],
            policy_by_package={"vite": "required"},
            repeated_count=3,
        )
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertNotIn("vite", suggestion.packages)
        self.assertIn("vite", suggestion.required_packages)

    def test_unknown_transitive_predicate_does_not_invent_cohort(self) -> None:
        self.assertIsNone(infer_baseline_cohort(
            predicate="duplicate-type-universe:totally-unrelated-transitive",
            direct_packages=["react", "date-fns", "eslint"],
        ))


if __name__ == "__main__":
    unittest.main()
