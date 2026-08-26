from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import constraint_verify
import peer_solver_z3
import prepared_workspace_fastpath
import source_snapshot
import verification_workspace_backend
from analyze_verification_telemetry import build_report, load_events
from peer_solver_model import ExactSolveResult
from verification_observability import (
    configure_observability_path,
    emit_run_summary,
    run_summary_payload,
)

# BLOCK_Y_FULL_OBSERVABILITY_V1


class _FakePackage:
    def __init__(self, name: str, domain: tuple[str, ...]) -> None:
        self.name = name
        self.domain = domain


class _FakeModel:
    packages = (
        _FakePackage("a", ("1.0.0", "2.0.0")),
        _FakePackage("b", ("1.0.0",)),
    )
    constraints = (object(), object())
    requirements = (object(),)
    objective_width = 3

    def state_count_upper_bound(self) -> int:
        return 2


class BlockYFullObservabilityTests(unittest.TestCase):
    def _events(self, path: Path) -> list[dict[str, object]]:
        return load_events(path)

    def test_z3_wrapper_emits_model_dimensions_without_requiring_z3(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "telemetry.jsonl"
            configure_observability_path(path, reset=True, context={"project": "test"})
            original = peer_solver_z3._solve_z3_exact_impl
            peer_solver_z3._solve_z3_exact_impl = lambda model, *, timeout_ms=30000: ExactSolveResult(
                backend="z3",
                status="optimal",
                assignment={"a": "2.0.0", "b": "1.0.0"},
                score=(1, 2, 3),
                elapsed_ms=7,
            )
            try:
                result = peer_solver_z3.solve_z3_exact(_FakeModel(), timeout_ms=1234)
            finally:
                peer_solver_z3._solve_z3_exact_impl = original
            self.assertEqual("optimal", result.status)
            finish = [
                event
                for event in self._events(path)
                if event.get("event") == "solver.z3.finish"
            ][-1]
            self.assertEqual(2, finish["packageCount"])
            self.assertEqual(3, finish["candidateCount"])
            self.assertEqual(2, finish["hardConstraintCount"])
            self.assertEqual(1, finish["requirementCount"])
            self.assertEqual(3, finish["objectiveWidth"])
            self.assertEqual("optimal", finish["status"])

    def test_private_tree_materialization_reports_method_without_extra_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            (source / "a.txt").write_text("hello\n", encoding="utf-8")
            telemetry = root / "telemetry.jsonl"
            configure_observability_path(telemetry, reset=True)
            method = verification_workspace_backend.materialize_private_tree(
                source,
                target,
                timeout_seconds=30,
            )
            self.assertTrue((target / "a.txt").is_file())
            finish = [
                event
                for event in self._events(telemetry)
                if event.get("event") == "filesystem.materialize.finish"
            ][-1]
            self.assertEqual(method, finish["method"])
            self.assertGreaterEqual(int(finish["durationMs"]), 0)

    def test_source_manifest_reports_existing_scan_counts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "one.txt").write_text("12345", encoding="utf-8")

            telemetry = root / "telemetry.jsonl"
            configure_observability_path(telemetry, reset=True)
            manifest = source_snapshot.build_source_tree_manifest(source)
            self.assertEqual(1, manifest.file_count)
            finish = [
                event
                for event in self._events(telemetry)
                if event.get("event") == "source.manifest.finish"
            ][-1]
            self.assertEqual(1, finish["fileCount"])
            self.assertEqual(5, finish["byteCount"])

    def test_unsafe_telemetry_sink_inside_source_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw)
            (source / "one.txt").write_text("12345", encoding="utf-8")
            telemetry = source / "semantic-telemetry.jsonl"
            configure_observability_path(telemetry, reset=True)
            manifest = source_snapshot.build_source_tree_manifest(source)

            self.assertEqual(1, manifest.file_count)
            self.assertFalse(telemetry.exists())

    def test_dependency_integrity_reports_hashed_bytes_from_existing_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = root / "node_modules" / "demo"
            package.mkdir(parents=True)
            (package / "index.js").write_text("abcdef", encoding="utf-8")
            telemetry = root / "telemetry.jsonl"
            configure_observability_path(telemetry, reset=True)
            manifest = prepared_workspace_fastpath.build_dependency_integrity_manifest(root)
            self.assertEqual(1, len(manifest))
            finish = [
                event
                for event in self._events(telemetry)
                if event.get("event") == "filesystem.integrity.finish"
            ][-1]
            self.assertEqual(1, finish["fileCount"])
            self.assertEqual(6, finish["byteCount"])

    def test_ddmin_emits_machine_events_without_progress_callback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            telemetry = Path(raw) / "telemetry.jsonl"
            configure_observability_path(telemetry, reset=True)
            units = tuple(
                constraint_verify.VerificationUnit(f"u{index}", (f"p{index}",))
                for index in range(4)
            )

            def fails(candidate):
                return any(unit.id == "u0" for unit in candidate)

            result = constraint_verify.parallel_ddmin(
                units,
                fails,
                parallelism=2,
                max_checks=12,
                progress=None,
            )
            self.assertTrue(result)
            events = self._events(telemetry)
            self.assertTrue(
                any(event.get("event") == "localization.ddmin.start" for event in events)
            )
            self.assertTrue(
                any(
                    event.get("event") == "localization.ddmin.check-finish"
                    for event in events
                )
            )
            self.assertTrue(
                any(event.get("event") == "localization.ddmin.finish" for event in events)
            )

    def test_summary_and_analyzer_use_interval_union_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            telemetry = Path(raw) / "telemetry.jsonl"
            configure_observability_path(telemetry, reset=True, context={"project": "demo"})
            from verification_observability import emit_observability_event

            emit_observability_event(
                "solver.z3.finish",
                durationMs=100,
                packageCount=2,
                candidateCount=5,
                status="optimal",
            )
            emit_observability_event(
                "filesystem.materialize.finish",
                durationMs=50,
                method="synthetic",
            )
            emit_run_summary(final=True, reason="test")
            summary = run_summary_payload(final=True)
            self.assertIn("performanceBreakdown", summary)
            self.assertIn("accounting", summary)
            self.assertEqual("interval-union-v1", summary["accounting"]["model"])

            report = build_report(self._events(telemetry))
            categories = {
                item["category"] for item in report["performanceBreakdown"]
            }
            self.assertIn("solver-z3", categories)
            self.assertIn("filesystem-materialize", categories)

    def test_block_y_does_not_change_proof_schema(self) -> None:
        import verification_proof

        self.assertEqual(
            "baseline-proof-v7-tool-build",
            verification_proof.PROOF_SCHEMA_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
