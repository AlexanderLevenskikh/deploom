from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from constraint_verify import LocalizationTimeoutError, VerificationUnit, assignment_matches_nogood, merge_nogood_edges, parallel_ddmin


class ConstraintVerifyTests(unittest.TestCase):
    def test_assignment_matches_exact_nogood(self) -> None:
        self.assertTrue(assignment_matches_nogood({"a": "2", "b": "3"}, {"a": "2", "b": "3"}))
        self.assertFalse(assignment_matches_nogood({"a": "2", "b": "4"}, {"a": "2", "b": "3"}))
        self.assertFalse(assignment_matches_nogood({"a": "2"}, {"a": "2", "b": "3"}))

    def test_nogood_connects_independent_solver_components(self) -> None:
        graph = {"a": set(), "b": set(), "c": set()}
        merge_nogood_edges(graph, [{"a": "2", "c": "5"}])
        self.assertEqual({"c"}, graph["a"])
        self.assertEqual({"a"}, graph["c"])
        self.assertEqual(set(), graph["b"])

    def test_parallel_ddmin_finds_interacting_pair(self) -> None:
        units = [
            VerificationUnit("a", ("a",)),
            VerificationUnit("b", ("b",)),
            VerificationUnit("c", ("c",)),
            VerificationUnit("d", ("d",)),
        ]
        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            names = {item.id for item in candidate}
            return {"b", "d"}.issubset(names)

        culprit = parallel_ddmin(units, fails, parallelism=4, max_checks=20)
        self.assertEqual({"b", "d"}, {item.id for item in culprit})

    def test_parallel_ddmin_emits_progress_events(self) -> None:
        units = [VerificationUnit(name, (name,)) for name in ("a", "b", "c", "d")]
        events: list[str] = []

        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {"b", "d"}.issubset({item.id for item in candidate})

        parallel_ddmin(
            units, fails, parallelism=2, max_checks=20,
            progress=lambda event, _details: events.append(event),
            progress_interval_seconds=1,
        )
        self.assertIn("start", events)
        self.assertIn("wave-start", events)
        self.assertIn("check-finish", events)
        self.assertIn("finish", events)

    def test_parallel_ddmin_has_total_watchdog(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]

        def hangs(_candidate: tuple[VerificationUnit, ...]) -> bool:
            time.sleep(0.25)
            return True

        with self.assertRaises(LocalizationTimeoutError):
            parallel_ddmin(
                units, hangs, parallelism=2, max_checks=4,
                timeout_seconds=0.05, progress_interval_seconds=1,
            )


if __name__ == "__main__":
    unittest.main()
