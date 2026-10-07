from datetime import datetime, timezone
import unittest

from iterative_migration import _audit_status_from_report, _terminal_outcome
from iterative_audit_policy import audit_engine_for_retry, audit_recovery_reason


def evidence():
    return {"auditComplete": True, "lagComplete": True, "dependencyInputHash": "sealed",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "policy": {"targetLevel": "yellow", "maxKnownCritical": 0, "maxKnownHigh": 1,
                       "minLagOkPct": 80, "lagPolicyMonths": 12},
            "audit": {"engine": "yarn-inventory", "trusted": True,
                      "canonicalInventory": {"complete": True}, "lagOkPct": 90,
                      "packageTotals": {"critical": 0, "high": 1, "unknown": 0},
                      "advisoryTotals": {"unknown": 0}}}


class AuditPolicyTests(unittest.TestCase):
    def test_collection_success_cannot_accept_vulnerabilities_or_lag(self):
        for critical, high, lag in ((5, 34, 51.5), (0, 2, 90), (0, 0, 79)):
            report = evidence()
            report["audit"]["packageTotals"].update(critical=critical, high=high)
            report["audit"]["lagOkPct"] = lag
            status = _audit_status_from_report(report)
            self.assertEqual(status, "FAIL")
            self.assertNotEqual(_terminal_outcome(satisfied=True, audit_status=status, accepted_count=29), "COMPLETE")
        self.assertEqual(_audit_status_from_report(evidence()), "PASS")

    def test_missing_unrated_stale_or_approximate_evidence_is_unknown(self):
        cases = []
        for key in ("auditComplete", "lagComplete", "dependencyInputHash", "generatedAt", "policy"):
            report = evidence(); report.pop(key); cases.append(report)
        report = evidence(); report["audit"]["packageTotals"].pop("high"); cases.append(report)
        report = evidence(); report["audit"]["advisoryTotals"]["unknown"] = 1; cases.append(report)
        report = evidence(); report["generatedAt"] = "2000-01-01T00:00:00Z"; cases.append(report)
        report = evidence(); report["audit"].update(engine="npm-lock-bridge", accuracy="npm-resolved-approximation"); cases.append(report)
        report = evidence(); report["policy"]["maxKnownCritical"] = False; cases.append(report)
        report = evidence(); report["audit"]["lagOkPct"] = float("nan"); cases.append(report)
        for report in cases:
            self.assertEqual(_audit_status_from_report(report), "UNKNOWN")

    def test_automatic_canonical_recovery_preserves_explicit_engine(self):
        report = evidence()
        report["audit"].update(engine="npm-lock-bridge", accuracy="npm-resolved-approximation")
        reason = audit_recovery_reason(report)
        self.assertEqual(reason, "CANONICAL_YARN_AUDIT_REQUIRED")
        self.assertFalse(audit_recovery_reason(report, allow_canonical=False))
        self.assertEqual(audit_engine_for_retry("auto", {"retryableReason": reason}), "yarn-inventory")
        self.assertEqual(audit_engine_for_retry("npm-lock-bridge", {"retryableReason": reason}), "npm-lock-bridge")
        report = {"auditComplete": False, "audit": {"engine": "yarn-inventory", "notes": ["ECONNRESET"]}}
        reason = audit_recovery_reason(report)
        self.assertEqual(audit_engine_for_retry("auto", {"engine": "yarn-inventory", "retryableReason": reason}), "yarn-inventory")
        self.assertEqual(audit_recovery_reason(report, allow_canonical=False), "ECONNRESET")

    def test_green_and_optional_limits_are_enforced(self):
        report = evidence(); report["policy"].update(targetLevel="green", maxKnownHigh=0)
        self.assertEqual(_audit_status_from_report(report), "FAIL")
        report["audit"].update(lagOkPct=100)
        report["audit"]["packageTotals"]["high"] = 0
        self.assertEqual(_audit_status_from_report(report), "PASS")
        report["policy"]["maxKnownModerate"] = 0
        self.assertEqual(_audit_status_from_report(report), "UNKNOWN")
        report["audit"]["packageTotals"]["moderate"] = 1
        self.assertEqual(_audit_status_from_report(report), "FAIL")


if __name__ == "__main__":
    unittest.main()
