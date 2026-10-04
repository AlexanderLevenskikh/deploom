from __future__ import annotations

import hashlib
import json
import unittest

from iterative_cohort_planner import plan_adaptive_cohort


def _fingerprint(assignment: dict) -> str:
    return hashlib.sha256(json.dumps(assignment, sort_keys=True).encode()).hexdigest()


def _plan(incumbent, desired, *, clauses=(), blocks=(), config=None, checkpoints=()):
    plan, details = plan_adaptive_cohort(
        incumbent=incumbent,
        desired=desired,
        config=config or {},
        ledger={},
        checkpoints=checkpoints,
        blocked_fingerprints=set(blocks),
        learned_nogoods=list(clauses),
        fingerprint_fn=_fingerprint,
        base_checkpoint_id=checkpoints[-1]["checkpointId"] if checkpoints else "C0",
    )
    return plan, details


class CohortClosureTests(unittest.TestCase):
    def test_mutually_exclusive_pair_closes_the_gap(self) -> None:
        # B4 cross-group closure trap: a+h cannot be in one group (RED). The
        # planner must separate them and RE-COMBINE the conflict-free subgroups
        # into a maximal cover instead of shrinking to a positional half.
        names = ["a", "b", "c", "d", "e", "f", "g", "h"]
        incumbent = {n: "1.0.0" for n in names}
        desired = {n: "1.1.0" for n in names}
        clauses = [{"a": "1.1.0", "h": "1.1.0"}]  # a+h incompatible at target
        plan, details = _plan(incumbent, desired, clauses=clauses)
        self.assertIsNotNone(plan)
        packages = set(plan.packages)
        self.assertTrue(packages.issubset(set(names)))
        self.assertFalse(
            {"a", "h"}.issubset(packages),
            "the mutually exclusive pair must never be proposed together",
        )
        # The gap is closed: the cohort covers all but at most the deferred
        # conflict members, never a single positional half.
        self.assertGreaterEqual(len(packages), 6,
                                "complementary subgroups must be recombined, not split in half")
        self.assertEqual(len(packages), len(set(packages)), "no duplicate packages")

    def test_pair_resolution_is_order_independent(self) -> None:
        # B4: reproducing the pair conflict must not depend on the order the
        # packages appear in the input/registry listing.
        names = ["a", "b", "c", "d", "e", "f", "g", "h"]
        clauses = [{"c": "1.1.0", "f": "1.1.0"}]
        forward = _plan({n: "1.0.0" for n in names}, {n: "1.1.0" for n in names},
                        clauses=clauses)[0]
        backward = _plan({n: "1.0.0" for n in reversed(names)},
                         {n: "1.1.0" for n in reversed(names)}, clauses=clauses)[0]
        shuffled = _plan({n: "1.0.0" for n in ["h", "b", "f", "a", "d", "c", "g", "e"]},
                         {n: "1.1.0" for n in ["h", "b", "f", "a", "d", "c", "g", "e"]},
                         clauses=clauses)[0]
        self.assertIsNotNone(forward)
        self.assertEqual(tuple(sorted(forward.packages)), tuple(sorted(backward.packages)))
        self.assertEqual(tuple(sorted(forward.packages)), tuple(sorted(shuffled.packages)))

    def test_multiple_independent_pairs_close_without_overlap(self) -> None:
        # Two independent mutually exclusive pairs (a+h, b+g): the cover must
        # protect BOTH pairs and still maximize coverage with no duplicates.
        names = ["a", "b", "c", "d", "e", "f", "g", "h"]
        incumbent = {n: "1.0.0" for n in names}
        desired = {n: "1.1.0" for n in names}
        clauses = [{"a": "1.1.0", "h": "1.1.0"}, {"b": "1.1.0", "g": "1.1.0"}]
        plan, _ = _plan(incumbent, desired, clauses=clauses)
        self.assertIsNotNone(plan)
        packages = set(plan.packages)
        self.assertEqual(len(packages), len(set(packages)))
        self.assertFalse({"a", "h"}.issubset(packages))
        self.assertFalse({"b", "g"}.issubset(packages))
        self.assertGreaterEqual(len(packages), 6)

    def test_dense_conflict_graph_stays_bounded_and_never_loops(self) -> None:
        # A fully mutually-exclusive pair graph (complete graph) admits only
        # singleton cohorts. The bounded closure must terminate with a single
        # package, never hang and never propose a forbidden pair.
        names = [f"p{i}" for i in range(8)]
        incumbent = {n: "1.0.0" for n in names}
        desired = {n: "1.1.0" for n in names}
        clauses = [dict.fromkeys((p, q), "1.1.0") for p in names for q in names if p < q]
        plan, _ = _plan(incumbent, desired, clauses=clauses)
        self.assertIsNotNone(plan)
        packages = set(plan.packages)
        self.assertEqual(1, len(packages))
        self.assertTrue(packages.issubset(set(names)))


if __name__ == "__main__":
    unittest.main()
