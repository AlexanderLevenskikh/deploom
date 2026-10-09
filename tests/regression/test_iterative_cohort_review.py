import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import iterative_migration as core
from iterative_cohort_review import review, recover, TRANSACTION
from iterative_audit_policy import audit_policy_status
from iterative_delivery import summary
from datetime import datetime, timezone

def evidence():
    return {"auditComplete": True, "lagComplete": True, "dependencyInputHash": "sealed",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "policy": {"targetLevel": "yellow", "maxKnownCritical": 0, "maxKnownHigh": 1,
                       "minLagOkPct": 80, "lagPolicyMonths": 12},
            "audit": {"engine": "yarn-inventory", "trusted": True,
                      "canonicalInventory": {"complete": True}, "lagOkPct": 90,
                      "packageTotals": {"critical": 0, "high": 1, "unknown": 0},
                      "advisoryTotals": {"unknown": 0}}}


class CohortReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deploom-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        core.save_run(self.root, {"schemaVersion": core.SCHEMA_VERSION, "runId": "fixture", "phase": "PLANNING",
                                  "activeCandidateId": "candidate", "activeCheckpointId": "C1"})
        self.candidate = {"schemaVersion": core.SCHEMA_VERSION, "runId": "fixture", "candidateId": "candidate",
                          "baseCheckpointId": "C1", "policyHash": "policy", "stage": "PLANNED", "attemptId": 0,
                          "fullAssignment": {"one": "2.0.0", "two": "2.0.0", "three": "2.0.0"},
                          "delta": {"changed": {"one": "2.0.0", "two": "2.0.0", "three": "2.0.0"}},
                          "atomicGroups": [["one", "two"]]}
        core.save_candidate(self.root, self.candidate)
        core.save_checkpoint(self.root, {"schemaVersion": core.SCHEMA_VERSION, "checkpointId": "C1", "status": "VERIFIED",
                                         "fullAssignment": {"one": "1.0.0", "two": "1.0.0", "three": "1.0.0"}})

    def test_production_cli_records_review_under_the_real_run_lock(self):
        import subprocess
        import sys
        payload = self.root / "review-input.json"
        payload.write_text(json.dumps({"candidateId": "candidate", "selected": ["one", "two", "three"]}), encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(core.__file__).resolve()), "--run-dir", str(self.root),
                                 "review-cohort", "--input-file", str(payload)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads((self.root / "cohort-approval.json").read_text())["fullAssignment"])

    def test_exact_approval_survives_reload_and_does_not_apply_source(self):
        self.assertTrue(review(self.root, "candidate", ["one", "two", "three"])["approved"])
        approval = json.loads((self.root / "cohort-approval.json").read_text())
        self.assertEqual(approval["fullAssignment"], self.candidate["fullAssignment"])
        self.assertEqual(core.load_candidate(self.root)["stage"], "PLANNED")
        self.assertFalse((self.root / "trial" / "workspace").exists())

    def test_exclusion_defers_atomic_group_without_false_incompatibility(self):
        result = review(self.root, "candidate", ["two", "three"])
        self.assertEqual(result["deferred"], ["one", "two"])
        self.assertEqual(core.load_run(self.root)["phase"], "READY")
        ledger = core.load_ledger(self.root)
        self.assertEqual(ledger["userDeferredPackages"], ["one", "two"])
        self.assertEqual(ledger["blocks"], [])
        self.assertEqual(core.load_candidate(self.root)["stage"], "REJECTED")

    def test_interrupted_deferral_recovers_without_reproposing_declined_bytes(self):
        with patch.object(core, "save_candidate", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError): review(self.root, "candidate", ["three"])
        self.assertTrue((self.root / TRANSACTION).is_file())
        recover(self.root)
        self.assertFalse((self.root / TRANSACTION).exists())
        self.assertEqual(core.load_candidate(self.root)["stage"], "REJECTED")
        self.assertEqual(core.load_run(self.root)["phase"], "READY")
        self.assertEqual(core.load_ledger(self.root)["userDeferredPackages"], ["one", "two"])
        recover(self.root)

    def test_review_includes_companion_delta_missing_from_display_summary(self):
        self.candidate["delta"] = {"changed": {"one": "2.0.0"}}
        core.save_candidate(self.root, self.candidate)
        result = review(self.root, "candidate", ["one"])
        self.assertEqual(result["deferred"], ["one", "three", "two"])
        self.assertFalse((self.root / "cohort-approval.json").exists())

    def test_acceptance_cannot_override_a_human_exclusion(self):
        core.save_ledger(self.root, {"schemaVersion": core.SCHEMA_VERSION, "userDeferredPackages": ["one"]})
        with patch.object(core, "capture_durable_source_snapshot") as capture:
            with self.assertRaisesRegex(core.InvalidInputError, "USER_DEFERRED_PACKAGE_CHANGED"):
                core._accept_checkpoint(self.root, {}, {}, {"fullAssignment": {"one": "2.0.0"}}, {"fullAssignment": {"one": "1.0.0"}}, None)
            capture.assert_not_called()

    def test_stale_identity_or_unlisted_package_is_rejected_without_mutation(self):
        before = (self.root / "run.json").read_bytes()
        for candidate, selection in [("old", []), ("candidate", ["foreign"])]:
            with self.assertRaises(core.InvalidInputError): review(self.root, candidate, selection)
            self.assertEqual((self.root / "run.json").read_bytes(), before)
        self.assertFalse((self.root / "cohort-approval.json").exists())

    def test_satisfied_audit_goal_stops_before_expansion_and_solver(self):
        run = core.load_run(self.root); run["activeCandidateId"] = None
        core.clear_candidate(self.root)
        core.save_checkpoint(self.root, {"schemaVersion": core.SCHEMA_VERSION, "checkpointId": "C1",
                                         "status": "VERIFIED", "fullAssignment": {"one": "1.0.0"}, "audit": {"status": "PASS"}})
        config = {"goalMode": "audit-policy", "targets": {"one": "9.0.0"}, "packagePolicies": {"one": "auto"}}
        with patch.object(core, "run_budget_ok", return_value=True), patch.object(core, "_assert_runtime_unchanged"), patch("iterative_scope_expansion.prepare_expansions") as expansions:
            self.assertEqual(core._plan_next_locked(self.root, run, config), 0)
            expansions.assert_not_called()

    def test_partial_metadata_can_prove_failure_but_never_success(self):
        report = evidence(); report["lagComplete"] = False
        report["audit"]["packageTotals"]["critical"] = 2
        self.assertEqual(audit_policy_status(report), "FAIL")
        report["audit"]["packageTotals"]["critical"] = 0
        self.assertEqual(audit_policy_status(report), "UNKNOWN")
        report["audit"].update(lagOk=54, lagTotal=84, lagUnknown=1)
        self.assertEqual(audit_policy_status(report), "FAIL")
        report["audit"]["packages"] = {"vulnerable": {"critical": 2}}
        report["lag"] = [{"name": "private-lib", "current": "2.0.0", "status": "unknown", "error": "missing publish date"}]
        result = summary(report, self.root)
        self.assertTrue(result["securityComplete"])
        self.assertFalse(result["auditComplete"])
        self.assertEqual(result["vulnerablePackages"][0]["severity"], "critical")
        self.assertEqual(result["unknownPackages"][0]["package"], "private-lib")
