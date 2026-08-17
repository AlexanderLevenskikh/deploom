from __future__ import annotations

import unittest

from examples.synthetic_baseline_scenarios import SCENARIOS


class SyntheticBaselineScenarioTests(unittest.TestCase):
    def test_sat_peer_chain(self) -> None:
        result = SCENARIOS["sat-peer-chain"]()
        self.assertEqual("SAT_PROVEN", result["terminalStatus"])
        self.assertEqual({"plugin": "2.0.0", "host": "2.0.0", "tool": "2.0.0"}, result["assignment"])

    def test_unsat_peer_contradiction(self) -> None:
        result = SCENARIOS["unsat-peer-contradiction"]()
        self.assertEqual("UNSAT_PROVEN", result["terminalStatus"])
        self.assertEqual("unsat", result["solverStatus"])

    def test_exact_exclusion_gets_second_solve(self) -> None:
        result = SCENARIOS["exact-exclusion-followup"]()
        self.assertTrue(result["extensionGranted"])
        self.assertEqual(1, result["allowedIterationsBefore"])
        self.assertEqual(2, result["allowedIterationsAfter"])
        self.assertNotEqual(result["firstAssignment"], result["secondAssignment"])
        self.assertEqual("SAT_PROVEN", result["terminalStatus"])

    def test_soft_plateau_and_hard_limit_are_distinct(self) -> None:
        plateau = SCENARIOS["plateau-without-authority"]()
        hard = SCENARIOS["hard-safety-limit"]()
        self.assertEqual("PLATEAU", plateau["terminalStatus"])
        self.assertEqual("HARD_SAFETY_LIMIT", hard["terminalStatus"])
        self.assertEqual([True, True, False], hard["extensionGranted"])

    def test_global_exact_exhaustion_is_unsat_proven(self) -> None:
        result = SCENARIOS["global-exact-unsat"]()
        self.assertEqual("unsat-proven", result["coordinatorReason"])
        self.assertEqual("UNSAT_PROVEN", result["terminalStatus"])

    def test_real_dependency_rows_extract_to_reference_model(self) -> None:
        result = SCENARIOS["dependency-rows-model"]()
        self.assertEqual("SAT_PROVEN", result["terminalStatus"])
        self.assertEqual({"plugin": "2.0.0", "host": "2.0.0", "tool": "2.0.0"}, result["assignment"])
        self.assertGreaterEqual(result["constraints"], 2)


if __name__ == "__main__":
    unittest.main()
