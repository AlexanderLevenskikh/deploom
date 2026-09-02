from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import dependency_live_roadmap_generator as roadmap
from constraint_verify import GlobalExactExclusionError, RankedComponentAlternative, coordinate_global_exact_exclusions


class BlockSConvergenceTerminalTests(unittest.TestCase):
    def test_fresh_exact_exclusion_guarantees_followup_solve_before_hard_ceiling(self) -> None:
        budget = roadmap.BaselineLivenessBudget(base_iterations=1, max_learning_extensions=2)
        self.assertEqual(1, budget.allowed_iterations)
        self.assertTrue(budget.record_exact_exclusion())
        self.assertEqual(1, budget.allowed_iterations)
        snapshot = budget.snapshot(learned_constraints=0)
        self.assertEqual(1, snapshot["exactExtensionCredits"])
        self.assertEqual(1, snapshot["certifiedExtensions"])

    def test_exact_and_generalized_authority_share_hard_safety_ceiling(self) -> None:
        budget = roadmap.BaselineLivenessBudget(base_iterations=1, max_learning_extensions=2)
        self.assertTrue(budget.record_exact_exclusion())
        budget.observe_learned_constraints(1)
        self.assertEqual(1, budget.allowed_iterations)
        self.assertEqual(3, budget.hard_iterations)
        self.assertFalse(budget.record_exact_exclusion())
        snapshot = budget.snapshot(learned_constraints=1)
        self.assertEqual(2, snapshot["certifiedExtensions"])
        self.assertEqual(1, snapshot["learnedExtensions"])
        self.assertEqual(1, snapshot["exactExtensionCredits"])
        self.assertEqual(2, snapshot["exactExclusions"])

    def test_exact_solver_status_mapping_is_typed(self) -> None:
        expected = {
            "optimal": roadmap.BaselineTerminalStatus.SAT_PROVEN,
            "unsat": roadmap.BaselineTerminalStatus.UNSAT_PROVEN,
            "unknown": roadmap.BaselineTerminalStatus.SOLVER_UNKNOWN,
            "sat_unproven": roadmap.BaselineTerminalStatus.SOLVER_UNKNOWN,
            "unknown_refinement_budget": roadmap.BaselineTerminalStatus.BUDGET_EXHAUSTED,
            "unavailable": roadmap.BaselineTerminalStatus.SOLVER_UNAVAILABLE,
            "error": roadmap.BaselineTerminalStatus.SOLVER_UNKNOWN,
        }
        for raw, terminal in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(terminal, roadmap._terminal_status_for_exact_solver(raw))

    def test_typed_baseline_error_preserves_machine_fields(self) -> None:
        error = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.UNSAT_PROVEN,
            "EXACT_SOLVER_UNSAT_PROVEN",
            "Demo/yellow: no assignment",
            source="z3",
        )
        self.assertEqual("UNSAT_PROVEN", error.terminal_status)
        self.assertEqual("z3", error.terminal_source)
        self.assertEqual("EXACT_SOLVER_UNSAT_PROVEN", error.stop_code)
        self.assertIn("terminalStatus=UNSAT_PROVEN", str(error))

    def test_global_coordinator_reports_proven_exhaustion_not_generic_error(self) -> None:
        initial = (
            RankedComponentAlternative({"a": "2"}, (10,)),
            RankedComponentAlternative({"b": "2"}, (10,)),
        )
        with self.assertRaises(GlobalExactExclusionError) as captured:
            coordinate_global_exact_exclusions(initial, [{"a": "2", "b": "2"}], lambda _index, _existing: None)
        self.assertEqual("unsat-proven", captured.exception.reason)

    def test_global_coordinator_budget_is_not_unsat(self) -> None:
        initial = (
            RankedComponentAlternative({"a": "2"}, (10,)),
            RankedComponentAlternative({"b": "2"}, (10,)),
        )
        alternatives = {
            0: RankedComponentAlternative({"a": "1"}, (9,)),
            1: RankedComponentAlternative({"b": "1"}, (9,)),
        }
        def next_alt(index, existing):
            return alternatives[index] if len(existing) == 1 else None
        with self.assertRaises(GlobalExactExclusionError) as captured:
            coordinate_global_exact_exclusions(
                initial,
                [{"a": "2", "b": "2"}, {"a": "1", "b": "2"}, {"a": "2", "b": "1"}],
                next_alt,
                max_states=1,
            )
        self.assertEqual("budget-exhausted", captured.exception.reason)

    def test_solver_terminal_progress_is_not_overwritten_by_late_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            reporter = roadmap.BaselineProgressReporter(path)
            reporter.emit("Demo", "yellow", "solver-terminal", terminalStatus="UNSAT_PROVEN", stopCode="EXACT_SOLVER_UNSAT_PROVEN")
            reporter.emit("Demo", "yellow", "localization-check-running", check=99)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("solver-terminal", payload["phase"])
            self.assertEqual("UNSAT_PROVEN", payload["terminalStatus"])

    def test_production_exact_exclusion_records_extension_before_continue(self) -> None:
        source = Path(roadmap.__file__).read_text(encoding="utf-8")
        marker = "global_exact_exclusions[project][mode].append(exact_nogood)"
        start = source.index(marker)
        section = source[start: source.index("continue", start) + len("continue")]
        self.assertIn("extension_granted = liveness.record_exact_exclusion()", section)
        self.assertIn("extensionGranted=extension_granted", section)

    def test_terminal_model_distinguishes_soft_and_hard_stops(self) -> None:
        source = Path(roadmap.__file__).read_text(encoding="utf-8")
        for marker in (
            "BaselineTerminalStatus.PLATEAU", "BaselineTerminalStatus.HARD_SAFETY_LIMIT",
            "EXACT_SOLVER_UNSAT_PROVEN", "EXACT_SOLVER_UNKNOWN",
            "EXACT_SOLVER_UNAVAILABLE", "EXACT_SOLVER_BUDGET_EXHAUSTED",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
