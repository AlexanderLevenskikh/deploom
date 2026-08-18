from __future__ import annotations

import unittest

from block_v_predicate_search import PredicateObservation, PredicateProbePolicy
from block_vf_active_search import (
    PROBE_OUTCOME_ABSENT,
    PROBE_OUTCOME_PRESENT,
    ActiveSearchResult,
    ProbeExecution,
    run_active_predicate_search,
)


class ActivePredicateSearchTests(unittest.TestCase):
    def test_repeat_threshold_activates_and_uses_control_point_then_interior(self):
        observations = [
            PredicateObservation("pkg", "5.1.1", "P", True, "a"),
            PredicateObservation("pkg", "5.1.0", "P", True, "b"),
        ]
        seen = []

        def runner(version, assignment):
            seen.append((version, dict(assignment)))
            # The source/current version is a useful known-good-side experiment;
            # the next boundary-oriented point is also predicate-absent.
            return ProbeExecution(
                version=version,
                outcome=PROBE_OUTCOME_ABSENT,
                assignment_fingerprint="probe-" + version,
            )

        result = run_active_predicate_search(
            package="pkg",
            predicate="P",
            base_assignment={"pkg": "5.1.0", "peer": "9.0.0"},
            project_current_version="2.0.0",
            versions=["2.0.0", "3.0.0", "4.0.0", "5.0.0", "5.1.0", "5.1.1"],
            observations=observations,
            policy=PredicateProbePolicy(True, 2, 3),
            run_probe=runner,
        )
        self.assertTrue(result.activated)
        self.assertEqual(result.repeat_count, 2)
        self.assertEqual(seen[0][0], "2.0.0")
        self.assertNotEqual(result.preferred_version, "2.0.0")
        self.assertTrue(result.preferred_version)
        self.assertEqual(len(seen), 2)
        for _version, assignment in seen:
            self.assertEqual(assignment["peer"], "9.0.0")

    def test_present_points_do_not_prune_and_budget_is_bounded(self):
        observations = [
            PredicateObservation("pkg", "5.1.1", "P", True),
            PredicateObservation("pkg", "5.1.0", "P", True),
        ]
        calls = 0

        def runner(version, assignment):
            nonlocal calls
            calls += 1
            return ProbeExecution(version=version, outcome=PROBE_OUTCOME_PRESENT)

        result = run_active_predicate_search(
            package="pkg",
            predicate="P",
            base_assignment={"pkg": "5.1.0"},
            project_current_version="2.0.0",
            versions=["2.0.0", "3.0.0", "4.0.0", "5.0.0", "5.1.0", "5.1.1"],
            observations=observations,
            policy=PredicateProbePolicy(True, 2, 2),
            run_probe=runner,
        )
        self.assertEqual(calls, 2)
        self.assertEqual(result.preferred_version, "")
        self.assertEqual(len(result.executions), 2)
        self.assertGreaterEqual(result.remaining_candidates, 2)

    def test_first_failure_does_not_activate(self):
        called = False

        def runner(version, assignment):
            nonlocal called
            called = True
            return ProbeExecution(version=version, outcome=PROBE_OUTCOME_ABSENT)

        result = run_active_predicate_search(
            package="pkg",
            predicate="P",
            base_assignment={"pkg": "5.1.1"},
            project_current_version="2.0.0",
            versions=["2.0.0", "4.0.0", "5.1.1"],
            observations=[PredicateObservation("pkg", "5.1.1", "P", True)],
            policy=PredicateProbePolicy(True, 2, 3),
            run_probe=runner,
        )
        self.assertFalse(result.activated)
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
