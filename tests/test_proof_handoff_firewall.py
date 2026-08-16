from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

import dependency_live_roadmap_generator as roadmap
from peer_solver_z3 import _objective_bounds_proven


ROOT = Path(__file__).resolve().parents[1]


class _FakeOptimize:
    def __init__(self, bounds):
        self.bounds = bounds

    def lower(self, handle):
        return self.bounds[handle][0]

    def upper(self, handle):
        return self.bounds[handle][1]


class ProofHandoffFirewallTests(unittest.TestCase):
    REGISTRY = "https://nexus.example/repository/npm-group"

    def test_transient_registry_metadata_failure_is_fatal_and_not_negative_cached(self) -> None:
        client = roadmap.LiveDataClient(self.REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
        client.session.get = mock.Mock(side_effect=requests.Timeout("temporary registry timeout"))
        with mock.patch.object(roadmap.time, "sleep"):
            with self.assertRaisesRegex(roadmap.RegistryInfrastructureError, "REGISTRY_METADATA_UNAVAILABLE"):
                client.fetch_npm_metadata("demo")
        self.assertNotIn("demo", client.npm_cache)
        self.assertEqual(roadmap.REGISTRY_METADATA_MAX_ATTEMPTS, client.session.get.call_count)

    def test_registry_404_is_a_deterministic_negative_fact(self) -> None:
        client = roadmap.LiveDataClient(self.REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
        response = mock.Mock()
        response.status_code = 404
        client.session.get = mock.Mock(return_value=response)
        self.assertIsNone(client.fetch_npm_metadata("missing-demo"))
        self.assertIn("missing-demo", client.npm_cache)
        self.assertIsNone(client.npm_cache["missing-demo"])
        self.assertEqual(1, client.session.get.call_count)

    def test_transient_artifact_probe_failure_is_not_cached_as_unavailable(self) -> None:
        client = roadmap.LiveDataClient(self.REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
        client.session.get = mock.Mock(side_effect=requests.ConnectionError("connection reset"))
        meta = {
            "versions": {
                "2.0.0": {
                    "dist": {
                        "tarball": f"{self.REGISTRY}/demo/-/demo-2.0.0.tgz"
                    }
                }
            }
        }
        with self.assertRaisesRegex(roadmap.RegistryInfrastructureError, "REGISTRY_ARTIFACT_UNAVAILABLE"):
            client.registry_version_artifact("demo", meta, "2.0.0")
        self.assertNotIn(("demo", "2.0.0"), client.registry_artifact_cache)

    def test_proven_assignment_conformance_is_exact(self) -> None:
        row = SimpleNamespace(
            name="demo",
            current_version="1.0.0",
            target_yellow="2.0.0",
        )
        proven = {"Demo": {"yellow": {"demo": "2.0.0"}}}
        roadmap.assert_proven_assignment_conformance(
            {"Demo": [row]},
            proven,
            modes=("yellow",),
        )
        row.target_yellow = roadmap.NO_ACTION
        with self.assertRaisesRegex(
            roadmap.BaselineConstraintVerificationError,
            "PROVEN_ASSIGNMENT_MUTATED",
        ):
            roadmap.assert_proven_assignment_conformance(
                {"Demo": [row]},
                proven,
                modes=("yellow",),
            )

    def test_objective_bounds_must_be_equal_before_optimal_is_claimed(self) -> None:
        proven, detail = _objective_bounds_proven(
            _FakeOptimize({"a": ("10", "10"), "b": ("3", "3")}),
            ["a", "b"],
        )
        self.assertTrue(proven, detail)

        proven, detail = _objective_bounds_proven(
            _FakeOptimize({"a": ("9", "10")}),
            ["a"],
        )
        self.assertFalse(proven)
        self.assertIn("objective[0]", detail)

    def test_host_node_runtime_is_not_a_solver_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{}", encoding="utf-8")
            roadmap._PROJECT_NODE_VERSION_CACHE.clear()
            with mock.patch.object(roadmap.subprocess, "run") as active_node:
                self.assertEqual([], roadmap._project_node_versions(str(root)))
            active_node.assert_not_called()

    def test_production_handoff_contract_is_structurally_fail_closed(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        main = source[source.index("def main() -> None:"):]
        self.assertIn(
            "proven_assignments = resolve_peer_compatibility_with_verification(",
            main,
        )
        self.assertNotIn("minimize_yellow_plan_after_compatibility(", main)
        self.assertIn("allow_target_mutation=False", main)
        self.assertIn("immutable_targets=True", main)
        self.assertIn(
            "assert_proven_assignment_conformance(rows_by_project, proven_assignments)",
            main,
        )

        baseline = source[
            source.index("def resolve_peer_compatibility_with_verification("):
            source.index("def _peer_scope_blocker(")
        ]
        self.assertIn(
            "global_exact_exclusions_by_project_mode=global_exact_exclusions",
            baseline,
        )
        self.assertIn("PROVEN_ASSIGNMENT_REOPENED", baseline)
        self.assertIn("return final_assignments", baseline)
        self.assertIn("target_{mode}_dynamic_locked", baseline)

        self.assertIn(
            "result.dynamicLocked || (typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('desktop-export'))",
            source,
        )
        self.assertIn("EXACT_SOLVER_PROOF_REQUIRED", source)

        node_function = source[
            source.index("def _project_node_versions("):
            source.index("def _project_environment_constraint_issue(")
        ]
        self.assertNotIn("node --version", node_function)
        self.assertNotIn("subprocess.run(", node_function)

        migration = (ROOT / "desktop" / "electron" / "migration-progress.ts").read_text(encoding="utf-8")
        self.assertIn("function versionExactlyMatches(", migration)
        self.assertNotIn("function versionAtLeast(", migration)

    def test_proof_envelope_transport_is_on_production_path(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        main = source[source.index("def main() -> None:"):]
        self.assertIn("proven_dependency_envelopes", main)
        self.assertIn("write_proven_dependency_state(", main)
        self.assertIn("proven_dependency_state=proven_dependency_state", main)

        baseline = source[
            source.index("def resolve_peer_compatibility_with_verification("):
            source.index("def _peer_scope_blocker(")
        ]
        self.assertIn("if not changed and not removals:", baseline)
        self.assertIn("BASELINE_VERIFICATION_REQUIRED", baseline)
        self.assertIn("external-evidence-exact-assignment-blocked", baseline)
        self.assertIn("external_evidence.exact_assignment", baseline)
        self.assertIn("PROVEN_REMOVE_TARGET", baseline)

        migration = (ROOT / "desktop" / "electron" / "migration-progress.ts").read_text(encoding="utf-8")
        self.assertIn("validateScopeProofEnvelope(", migration)
        self.assertIn("proofEnvelopeContentKey(", migration)

        materialization = (ROOT / "desktop" / "electron" / "materialization-proof.ts").read_text(encoding="utf-8")
        self.assertIn("schemaVersion: 2", materialization)
        self.assertIn("observedResolvedVersions", materialization)
        self.assertIn("observedResolvedDirectAssignmentHash", materialization)
        self.assertIn("provenObservedResolvedHash", materialization)
        self.assertIn("provenEnvelopeKey", materialization)

        desktop = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_DRIFT", desktop)
        self.assertIn("proofEnvelope.envelopeKey", desktop)



if __name__ == "__main__":
    unittest.main()
