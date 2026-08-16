from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier
import baseline_performance_summary as perf
import dependency_live_roadmap_generator as roadmap
import verification_proof


class BlockMFinalClosureTests(unittest.TestCase):
    def test_performance_summary_aggregates_without_becoming_authority(self) -> None:
        events = [
            {"event": "verify.attempt.start", "projectPath": "p", "assignmentKey": "a"},
            {"event": "verify.resolver.finish", "projectPath": "p", "durationMs": 1200, "outcome": "passed", "assignmentKey": "a"},
            {"event": "proof.cache.hit", "projectPath": "p", "proofType": "resolver", "assignmentKey": "a"},
            {"event": "verify.preparation.finish", "projectPath": "p", "durationMs": 3000, "outcome": "passed", "assignmentKey": "a"},
            {"event": "verify.preparation.snapshot-hit", "projectPath": "p", "assignmentKey": "a"},
            {"event": "verify.project-check.clone.finish", "projectPath": "p", "durationMs": 70, "command": "check-a", "assignmentKey": "a"},
            {"event": "verify.project-check.finish", "projectPath": "p", "durationMs": 900, "command": "check-a", "outcome": "passed", "assignmentKey": "a"},
            {"event": "verify.attempt.finish", "projectPath": "p", "outcome": "passed", "assignmentKey": "a"},
        ]
        summary = perf.summarize_verification_events(events, malformed_lines=2)
        overall = summary["overall"]
        self.assertEqual(1, overall["attempts"])
        self.assertEqual(1, overall["completedAttempts"])
        self.assertEqual(1200, overall["durationsMs"]["resolverMs"])
        self.assertEqual(3000, overall["durationsMs"]["preparationMs"])
        self.assertEqual(900, overall["durationsMs"]["projectChecksMs"])
        self.assertEqual(1, overall["proofCacheHits"]["resolver"])
        self.assertEqual(1, overall["avoidedWork"]["lifecyclePreparationsBySnapshotHit"])
        self.assertEqual(2, summary["malformedTelemetryLines"])
        rendered = perf.render_performance_markdown(summary)
        self.assertIn("Telemetry is observability only", rendered)

    def test_telemetry_loader_ignores_malformed_lines_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.jsonl"
            path.write_text(
                "{bad json}\n"
                + json.dumps({"event": "verify.attempt.start", "projectPath": "z"}) + "\n"
                + json.dumps({"notEvent": True}) + "\n",
                encoding="utf-8",
            )
            loaded = perf.load_verification_telemetry(path)
            self.assertEqual(1, len(loaded.events))
            self.assertEqual(2, loaded.malformed_lines)

    def test_summary_is_order_independent_for_aggregate_metrics(self) -> None:
        events = [
            {"event": "verify.resolver.finish", "projectPath": "b", "durationMs": 11, "outcome": "passed"},
            {"event": "verify.resolver.finish", "projectPath": "a", "durationMs": 7, "outcome": "passed"},
            {"event": "proof.cache.hit", "projectPath": "a", "proofType": "project"},
        ]
        first = perf.summarize_verification_events(events)
        shuffled = list(events)
        random.Random(7).shuffle(shuffled)
        second = perf.summarize_verification_events(shuffled)
        self.assertEqual(first, second)
        self.assertEqual(["a", "b"], list(first["projects"]))

    def test_corrupt_resolver_proof_is_a_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = verification_proof.VerificationProofStore(Path(tmp))
            key = "a" * 32
            path = Path(tmp) / "resolver" / f"{key}.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "proofSchema": verification_proof.PROOF_SCHEMA_VERSION,
                "proofType": "resolver",
                "key": key,
                "outcome": "passed",
                "identity": {},
                "metadata": {
                    "observedResolvedVersions": {"x": "1.0.0"},
                    "observedResolvedHash": "0" * 64,
                },
            }), encoding="utf-8")
            self.assertIsNone(store.lookup_pass("resolver", key))

    def test_scale_fixed_constant_projection_is_deterministic_for_12000_candidates(self) -> None:
        managed_names = [f"pkg-{index:03d}" for index in range(120)]
        managed = {
            name: SimpleNamespace(name=name, current_version="1.0.0")
            for name in managed_names
        }
        fixed = {"fixed-host": SimpleNamespace(name="fixed-host", current_version="1.5.0")}
        domains = {
            name: [f"1.{index}.0" for index in range(50)] + [f"2.{index}.0" for index in range(50)]
            for name in managed_names
        }

        def peers(row, version, _client):
            if row.name == "fixed-host":
                return []
            # Half of the candidate domain is deliberately incompatible with
            # the immutable fixed provider. This exercises real unary pruning.
            required = "^2.0.0" if str(version).startswith("2.") else "^1.0.0"
            return [("fixed-host", required, False)]

        with mock.patch.object(roadmap, "_peer_entries", side_effect=peers):
            first, stats_first = roadmap._apply_fixed_peer_constant_constraints(
                managed, fixed, domains, SimpleNamespace()
            )
            reversed_managed = dict(reversed(list(managed.items())))
            reversed_domains = dict(reversed(list(domains.items())))
            second, stats_second = roadmap._apply_fixed_peer_constant_constraints(
                reversed_managed, fixed, reversed_domains, SimpleNamespace()
            )

        self.assertEqual(first, second)
        self.assertEqual(stats_first, stats_second)
        self.assertEqual(120 * 50, stats_first["excluded"])
        self.assertEqual(120, len(first))
        self.assertTrue(all(all(version.startswith("1.") for version in values) for values in first.values()))

    def test_architecture_tripwires_remain_present(self) -> None:
        verifier_source = Path(verifier.__file__).read_text(encoding="utf-8")
        roadmap_source = Path(roadmap.__file__).read_text(encoding="utf-8")
        self.assertIn('"ntfs-junction-guarded"', verifier_source)
        self.assertIn('"fresh-prepared-snapshot-clone"', verifier_source)
        self.assertIn("guarded_clone_is_active(command_root)", verifier_source)
        self.assertIn("stop_guarded_clone(command_root)", verifier_source)
        self.assertIn("PREPARED_DEPENDENCY_TREE_MUTATION", verifier_source)
        self.assertIn("OBSERVED_RESOLVED_ASSIGNMENT_DRIFT: project check ", verifier_source)
        self.assertIn("_apply_fixed_peer_constant_constraints", roadmap_source)
        self.assertIn('seed_source = "bounded-fresh"', roadmap_source)
        self.assertIn("EVIDENCE_DIAGNOSTIC_HINT", roadmap_source)
        self.assertIn("EVIDENCE_CONFIRMED_CONSTRAINT", roadmap_source)
        self.assertIn("BASELINE_SOLVER_REPEATED_FAILED_ASSIGNMENT", roadmap_source)


if __name__ == "__main__":
    unittest.main()
