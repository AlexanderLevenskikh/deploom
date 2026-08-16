from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from constraint_verify import (
    GlobalExactExclusionError,
    LocalizationTimeoutError,
    RankedComponentAlternative,
    VerificationUnit,
    assignment_matches_nogood,
    coordinate_global_exact_exclusions,
    merge_nogood_edges,
    parallel_ddmin,
)


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

    def test_parallel_screening_failure_cannot_shrink_without_serial_confirmation(self) -> None:
        units = [
            VerificationUnit("a", ("a",)),
            VerificationUnit("b", ("b",)),
        ]

        def parallel_screen(candidate: tuple[VerificationUnit, ...]) -> bool:
            # Model a false positive that exists only while the parallel wave is
            # running (shared daemon/cache/port/resource interference).
            return {item.id for item in candidate} == {"a"}

        def isolated_confirmation(_candidate: tuple[VerificationUnit, ...]) -> bool:
            return False

        culprit = parallel_ddmin(
            units,
            parallel_screen,
            parallelism=2,
            max_checks=8,
            confirm_failure=isolated_confirmation,
        )
        self.assertEqual({"a", "b"}, {item.id for item in culprit})

    def test_parallel_screening_failure_shrinks_after_serial_confirmation(self) -> None:
        units = [
            VerificationUnit("a", ("a",)),
            VerificationUnit("b", ("b",)),
        ]

        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            return "a" in {item.id for item in candidate}

        culprit = parallel_ddmin(
            units,
            fails,
            parallelism=2,
            max_checks=8,
            confirm_failure=fails,
        )
        self.assertEqual({"a"}, {item.id for item in culprit})

    def test_serial_confirmation_emits_heartbeat(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        events: list[str] = []

        def parallel_screen(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        def slow_confirmation(_candidate: tuple[VerificationUnit, ...]) -> bool:
            time.sleep(1.15)
            return True

        culprit = parallel_ddmin(
            units,
            parallel_screen,
            parallelism=2,
            max_checks=8,
            confirm_failure=slow_confirmation,
            progress=lambda event, _details: events.append(event),
            progress_interval_seconds=1,
            timeout_seconds=5,
        )
        self.assertEqual({"a"}, {item.id for item in culprit})
        self.assertIn("confirmation-heartbeat", events)

    def test_resume_reuses_screening_but_still_confirms_fail(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        parallel_calls = 0
        confirmation_calls = 0

        def parallel_screen(_candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal parallel_calls
            parallel_calls += 1
            raise AssertionError("cached screening result should be reused")

        def confirm(candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal confirmation_calls
            confirmation_calls += 1
            return {item.id for item in candidate} == {"a"}

        state = {
            "schemaVersion": 1,
            "initialUnitIds": ["a", "b"],
            "currentUnitIds": ["a", "b"],
            "granularity": 2,
            "checksStarted": 2,
            "cache": [
                {"unitIds": ["a"], "failed": True, "confirmedFailure": False},
                {"unitIds": ["b"], "failed": False, "confirmedFailure": False},
            ],
            "finished": False,
        }

        culprit = parallel_ddmin(
            units,
            parallel_screen,
            parallelism=2,
            max_checks=8,
            confirm_failure=confirm,
            resume_state=state,
        )
        self.assertEqual(0, parallel_calls)
        self.assertEqual(1, confirmation_calls)
        self.assertEqual({"a"}, {item.id for item in culprit})

    def test_checkpoint_can_resume_finished_localization_without_rechecks(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        states: list[dict[str, object]] = []

        def fails(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        culprit = parallel_ddmin(
            units,
            fails,
            parallelism=2,
            max_checks=8,
            confirm_failure=fails,
            checkpoint=lambda state: states.append(dict(state)),
        )
        self.assertEqual({"a"}, {item.id for item in culprit})
        final_state = states[-1]
        self.assertEqual(["a"], final_state["currentUnitIds"])
        self.assertTrue(final_state["finished"])

        calls = 0
        def should_not_run(_candidate: tuple[VerificationUnit, ...]) -> bool:
            nonlocal calls
            calls += 1
            return True

        resumed = parallel_ddmin(
            units,
            should_not_run,
            parallelism=2,
            max_checks=8,
            confirm_failure=should_not_run,
            resume_state=final_state,
        )
        self.assertEqual(0, calls)
        self.assertEqual({"a"}, {item.id for item in resumed})

    def test_serial_confirmation_obeys_total_watchdog(self) -> None:
        units = [VerificationUnit("a", ("a",)), VerificationUnit("b", ("b",))]
        events: list[str] = []

        def parallel_screen(candidate: tuple[VerificationUnit, ...]) -> bool:
            return {item.id for item in candidate} == {"a"}

        def hangs(_candidate: tuple[VerificationUnit, ...]) -> bool:
            time.sleep(0.5)
            return True

        with self.assertRaises(LocalizationTimeoutError):
            parallel_ddmin(
                units,
                parallel_screen,
                parallelism=2,
                max_checks=8,
                confirm_failure=hangs,
                progress=lambda event, _details: events.append(event),
                progress_interval_seconds=1,
                timeout_seconds=0.1,
            )
        self.assertIn("confirmation-heartbeat", events)
        self.assertIn("timeout", events)

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

    def test_global_exact_exclusion_keeps_components_independent(self) -> None:
        initial = (
            RankedComponentAlternative({"a": "2"}, (10,)),
            RankedComponentAlternative({"b": "2"}, (10,)),
        )
        alternatives = {
            0: [RankedComponentAlternative({"a": "1"}, (7,))],
            1: [RankedComponentAlternative({"b": "1"}, (9,))],
        }

        def next_alt(index, existing):
            rank = len(existing) - 1
            values = alternatives[index]
            return values[rank] if rank < len(values) else None

        assignment, explored = coordinate_global_exact_exclusions(
            initial,
            [{"a": "2", "b": "2"}],
            next_alt,
        )
        self.assertEqual({"a": "2", "b": "1"}, assignment)
        self.assertGreaterEqual(explored, 2)

    def test_global_exact_exclusion_handles_multiple_blocked_tuples(self) -> None:
        initial = (
            RankedComponentAlternative({"a": "2"}, (10,)),
            RankedComponentAlternative({"b": "2"}, (10,)),
        )
        alternatives = {
            0: [RankedComponentAlternative({"a": "1"}, (8,))],
            1: [RankedComponentAlternative({"b": "1"}, (9,))],
        }

        def next_alt(index, existing):
            rank = len(existing) - 1
            values = alternatives[index]
            return values[rank] if rank < len(values) else None

        assignment, _explored = coordinate_global_exact_exclusions(
            initial,
            [
                {"a": "2", "b": "2"},
                {"a": "2", "b": "1"},
            ],
            next_alt,
        )
        self.assertEqual({"a": "1", "b": "2"}, assignment)

    def test_global_exact_exclusion_never_strengthens_to_local_block(self) -> None:
        initial = (
            RankedComponentAlternative({"a": "2"}, (10,)),
            RankedComponentAlternative({"b": "2"}, (10,)),
        )
        calls: list[tuple[int, tuple[tuple[tuple[str, str], ...], ...]]] = []

        def next_alt(index, existing):
            frozen = tuple(
                tuple(sorted(item.items()))
                for item in existing
            )
            calls.append((index, frozen))
            if index == 0 and len(existing) == 1:
                return RankedComponentAlternative({"a": "1"}, (1,))
            if index == 1 and len(existing) == 1:
                return RankedComponentAlternative({"b": "1"}, (9,))
            return None

        assignment, _ = coordinate_global_exact_exclusions(
            initial,
            [{"a": "2", "b": "2"}],
            next_alt,
        )
        self.assertEqual({"a": "2", "b": "1"}, assignment)
        # The callback receives only the local component history. The global
        # a@2+b@2 exclusion is never projected into an authoritative local a@2
        # or b@2 constraint.
        self.assertIn((1, ((("b", "2"),),)), calls)



if __name__ == "__main__":
    unittest.main()
