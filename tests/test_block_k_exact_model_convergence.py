from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as roadmap


class BlockKExactModelConvergenceTests(unittest.TestCase):
    def _row(self, name: str, spec: str, *, current: str, package_dir: str = ".", target: str | None = None) -> roadmap.DependencyRow:
        desired = target or current
        return roadmap.DependencyRow(
            project="Demo", package_dir=package_dir, name=name, kind="dev",
            requested_spec=spec, current_version=current, current_source="lockfile",
            latest_version=desired, current_vulns="0", min_no_critical=current,
            min_no_high=current, min_no_vuln=current, min_lag_12m=current,
            min_lag_9m=current, min_lag_6m=current, min_lag_3m=current,
            group=4, reason="block-k", notes="", target_default=desired,
            target_yellow=desired, target_green=desired,
            desired_target_default=desired, desired_target_yellow=desired,
            desired_target_green=desired,
        )

    def _client(self) -> roadmap.LiveDataClient:
        return roadmap.LiveDataClient(
            "https://registry.example.test/npm", timeout=1, batch_size=10, sleep_sec=0
        )

    def test_fixed_source_peer_prunes_incompatible_managed_candidates_but_keeps_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "node_modules" / "fixed-plugin" / "package.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({
                "name": "fixed-plugin", "version": "3.0.0",
                "peerDependencies": {"react": "^18.0.0"},
            }), encoding="utf-8")
            fixed = self._row("fixed-plugin", "file:../fixed-plugin", current="3.0.0", package_dir=str(root))
            react = self._row("react", "^18.0.0", current="18.2.0", package_dir=str(root), target="19.0.0")
            domains, stats = roadmap._apply_fixed_peer_constant_constraints(
                {"react": react}, {"fixed-plugin": fixed},
                {"react": ["19.0.0", "18.3.1", "18.2.0"]}, self._client(),
            )
        self.assertEqual(["18.3.1", "18.2.0"], domains["react"])
        self.assertEqual(1, stats["excluded"])
        self.assertGreaterEqual(stats["evaluated"], 2)

    def test_managed_candidate_peer_on_fixed_provider_is_unary_hard_restriction(self) -> None:
        client = self._client()
        client.npm_cache["managed-plugin"] = {"versions": {
            "2.0.0": {"peerDependencies": {"fixed-host": "^4.0.0"}},
            "1.5.0": {"peerDependencies": {"fixed-host": "^3.0.0"}},
            "1.0.0": {},
        }}
        managed = self._row("managed-plugin", "^1.0.0", current="1.0.0", target="2.0.0")
        fixed = self._row("fixed-host", "workspace:*", current="3.2.0")
        domains, stats = roadmap._apply_fixed_peer_constant_constraints(
            {"managed-plugin": managed}, {"fixed-host": fixed},
            {"managed-plugin": ["2.0.0", "1.5.0", "1.0.0"]}, client,
        )
        self.assertEqual(["1.5.0", "1.0.0"], domains["managed-plugin"])
        self.assertEqual(1, stats["excluded"])

    def test_unknown_fixed_version_never_becomes_false_hard_constraint(self) -> None:
        client = self._client()
        client.npm_cache["managed-plugin"] = {"versions": {
            "2.0.0": {"peerDependencies": {"fixed-host": "^3.0.0"}}, "1.0.0": {},
        }}
        managed = self._row("managed-plugin", "^1.0.0", current="1.0.0", target="2.0.0")
        fixed = self._row("fixed-host", "workspace:*", current="unknown")
        domains, stats = roadmap._apply_fixed_peer_constant_constraints(
            {"managed-plugin": managed}, {"fixed-host": fixed},
            {"managed-plugin": ["2.0.0", "1.0.0"]}, client,
        )
        self.assertEqual(["2.0.0", "1.0.0"], domains["managed-plugin"])
        self.assertEqual(0, stats["excluded"])
        self.assertGreaterEqual(stats["unknown"], 1)

    def test_first_oversized_structural_family_gets_bounded_diagnostic_seed(self) -> None:
        assignment = {name: "2.0.0" for name in ("a", "b", "c", "d", "e")}
        result = roadmap.BaselineVerifyResult(False, "project", "project preflight failed", output="stable structural failure")
        with mock.patch.object(roadmap, "_graph_guided_generalization_candidate", return_value=None),              mock.patch.object(roadmap, "_failure_package_hints", return_value={"a", "b"}),              mock.patch.object(roadmap, "_bounded_graph_guided_generalization_candidate", return_value={"a": "2.0.0", "b": "2.0.0", "c": "2.0.0"}):
            proposal = roadmap._adaptive_graph_guided_generalization_proposal(
                {}, assignment, "yellow", self._client(), [], result,
                project_key="Demo", repeat_tracker={}, failed_candidates=set(), seed_packages_by_family={},
            )
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual("bounded-fresh", proposal.seed_source)
        self.assertTrue(proposal.bounded_slice)
        self.assertLess(len(proposal.candidate), len(assignment))

    def test_production_path_applies_fixed_constants_before_graph_and_keeps_fresh_certification(self) -> None:
        source = Path(roadmap.__file__).read_text(encoding="utf-8")
        prune = source.index("domains, fixed_peer_stats = _apply_fixed_peer_constant_constraints(")
        graph = source.index("graph = _potential_peer_graph(solver_rows_by_name, domains, client)", prune)
        self.assertLess(prune, graph)
        self.assertIn('seed_source = "bounded-fresh"', source)
        self.assertIn("graph certification", source)
        self.assertIn("constraint-minimization-check-started", source)
        self.assertIn("authority=EVIDENCE_DIAGNOSTIC_HINT", source)
        self.assertIn("authority=EVIDENCE_CONFIRMED_CONSTRAINT", source)


if __name__ == "__main__":
    unittest.main()
