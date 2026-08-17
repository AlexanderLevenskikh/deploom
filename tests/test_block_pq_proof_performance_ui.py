from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BlockPQContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.roadmap = load_module("block_pq_roadmap", ROOT / "dependency_live_roadmap_generator.py")
        cls.performance = load_module("block_pq_perf", ROOT / "baseline_performance_summary.py")

    def test_adaptive_predicate_families_are_independent_and_deterministic(self) -> None:
        split = self.roadmap._adaptive_predicate_families(
            "ts-module-resolution:@vitejs/plugin-react|esm-cjs:stylelint-selector-tag-no-without-class",
            adaptive_project=True,
        )
        self.assertEqual(split, (
            "esm-cjs:stylelint-selector-tag-no-without-class",
            "ts-module-resolution:@vitejs/plugin-react",
        ))
        self.assertEqual(
            self.roadmap._adaptive_predicate_families("resolver-signature|with-pipe", adaptive_project=False),
            ("resolver-signature|with-pipe",),
        )

    def test_ddmin_still_uses_certify_as_only_authority_gate(self) -> None:
        seen: list[tuple[str, ...]] = []
        def certify(trial: dict[str, str], _check: int) -> str:
            seen.append(tuple(sorted(trial)))
            return "sig" if {"a", "b"}.issubset(trial) else ""
        result = self.roadmap._proof_preserving_minimize_nogood(
            {"a": "1", "b": "1", "c": "1", "d": "1"},
            certify,
            initial_predicate="sig",
        )
        self.assertEqual(result.minimized, {"a": "1", "b": "1"})
        self.assertTrue(seen)
        self.assertEqual(result.predicate, "sig")

    def test_q_prefetch_is_fresh_and_fail_closed_by_contract(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn("OSV evidence prefetch completed", source)
        self.assertIn("OSV_PREFETCH_INCONSISTENT", source)
        self.assertIn("falling back to sequential fresh queries", source)

    def test_ntfs_fastpath_rejection_retries_with_full_copy_without_learning(self) -> None:
        verifier = (ROOT / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        fastpath = (ROOT / "prepared_workspace_fastpath.py").read_text(encoding="utf-8")
        self.assertIn("verify.project-check.fastpath-rejected", verifier)
        self.assertIn("_allow_prepared_fastpath=False", verifier)
        self.assertIn("_disable_prepared_snapshot_fastpath", verifier)
        self.assertIn("guard_result.notification_only", verifier)
        self.assertIn("build_dependency_integrity_manifest", verifier)
        self.assertIn("integrityMatchedNotifications", verifier)
        self.assertIn("confirmed-content-mutation", verifier)
        self.assertIn("notification-only", fastpath)
        self.assertIn("_fingerprint_path", fastpath)
        self.assertNotIn('endswith("esbuild.exe")', verifier)

    def test_predicate_family_minimization_reuses_only_exact_trial_evidence(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn("trial_proof_cache: Dict[str, List[Tuple[str, str, str]]]", source)
        self.assertIn("resolver_trial_identity = build_resolver_trial_key(", source)
        self.assertIn("cached_evidence_key", source)
        self.assertIn("trial_fingerprint = assignment_fingerprint(trial)", source)
        self.assertIn('"constraint-minimization-proof-reused"', source)
        self.assertIn("required_predicate in {", source)
        self.assertIn("for generalized in new_constraints", source)

    def test_resolver_purpose_summary_supports_before_after_comparison(self) -> None:
        events = [
            {"event": "verify.resolver.finish", "durationMs": 283000, "label": "Baseline nogood minimization yellow check 6 proof 1/2 x", "projectPath": "p"},
            {"event": "verify.resolver.finish", "durationMs": 150000, "label": "Baseline control yarn lint:types", "projectPath": "p"},
        ]
        summary = self.performance.summarize_verification_events(events)
        purposes = summary["overall"]["resolverByPurpose"]
        self.assertEqual(purposes["minimization"]["executions"], 1)
        self.assertEqual(purposes["minimization"]["durationMs"], 283000)
        self.assertEqual(purposes["control"]["durationMs"], 150000)

    def test_monitor_has_live_minimization_progress_not_stale_2_of_2(self) -> None:
        monitor = (ROOT / "desktop/src/data/processMonitor.ts").read_text(encoding="utf-8")
        view = (ROOT / "desktop/src/components/RunMonitor.tsx").read_text(encoding="utf-8")
        ru = (ROOT / "desktop/src/i18n/locales/ru.ts").read_text(encoding="utf-8")
        self.assertIn("projectCheck: undefined", monitor)
        self.assertIn("currentCheck?: number", monitor)
        self.assertIn("maxChecks?: number", monitor)
        self.assertIn("monitor.minimizationProgress", view)
        self.assertIn("monitor.currentCandidateSize", view)
        self.assertIn("Уточняю доказанный конфликт", ru)
        self.assertNotIn("Усиливаю доказанный конфликт", ru)


if __name__ == "__main__":
    unittest.main()
