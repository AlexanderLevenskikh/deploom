from __future__ import annotations

import unittest

from cohort_telemetry_report import summarize


class CohortTelemetryReportTests(unittest.TestCase):
    def test_pairs_user_defer_with_first_verified_incumbent_in_same_run(self) -> None:
        events = [
            {"event": "baseline.cohort.suggested", "runId": "r1", "runOffsetMs": 100, "cohortId": "vite-build"},
            {"event": "baseline.cohort.user-action", "runId": "r1", "runOffsetMs": 200, "cohortId": "vite-build", "action": "DEFER"},
            {"event": "baseline.cohort.incumbent", "runId": "r1", "runOffsetMs": 5200, "completionStatus": "VERIFIED_PARTIAL_SCOPE", "timeToFirstVerifiedUsableResultMs": 4800},
        ]
        report = summarize(events)
        self.assertEqual(1, report["deferrals"])
        self.assertEqual(1.0, report["deferralToIncumbentRate"])
        self.assertEqual(5.0, report["medianDeferralToIncumbentSeconds"])
        self.assertEqual(4.8, report["medianTTFVURSeconds"])


if __name__ == "__main__":
    unittest.main()
