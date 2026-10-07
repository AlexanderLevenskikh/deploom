from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import iterative_migration as migration
from baseline_constraint_verifier import assignment_fingerprint
from iterative_adaptive_budget import transient_audit_reason
from iterative_cohort_planner import plan_adaptive_cohort

spec = importlib.util.spec_from_file_location("adaptive_fixture", Path(__file__).with_name("test_iterative_finish_baseline_and_feedback_validation.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class AdaptiveBudgetTests(unittest.TestCase):
    def test_atom_grows_only_for_new_assignment_after_verified_progress(self):
        names = [f"p{i:02}" for i in range(25)]
        old = dict.fromkeys(names, "1.0.0")
        desired = dict.fromkeys(names, "2.0.0")
        checkpoints = [fixture._checkpoint_dict(), fixture._accepted_checkpoint(
            "C1", parent="C0", seq=1, assignment=old, changed={"previous": "2.0.0"})]
        ledger = {"scopeExpansions": [{"baseCheckpointId": "C1", "packages": names[:24], "companions": names[24:]}]}
        config = {"cohortMaxPackages": 24}

        def plan(blocks=(), cfg=config, checks=checkpoints):
            return plan_adaptive_cohort(incumbent=old, desired=desired, config=cfg,
                ledger=ledger, checkpoints=checks, base_checkpoint_id="C1",
                blocked_fingerprints=blocks, learned_nogoods=[], fingerprint_fn=assignment_fingerprint)

        result, details = plan()
        self.assertEqual(set(result.packages), set(names))
        self.assertTrue(details["budgetAdapted"])
        self.assertEqual(details["maxPackages"], 25)
        self.assertEqual(config, {"cohortMaxPackages": 24})
        self.assertIsNone(plan([assignment_fingerprint(desired)])[0])
        self.assertIsNone(plan(cfg={**config, "cohortSizePolicy": "fixed"})[0])
        self.assertIsNone(plan(checks=[])[0])

    def test_durable_audit_retries_transient_error_then_finalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fixture._RunDir(Path(tmp))
            root.write_config(fixture._config_dict())
            root.write_run(fixture._run_dict(phase="TERMINAL", terminal="SCOPE_EXHAUSTED", activeCheckpointId="C1"))
            root.write_checkpoint(fixture._checkpoint_dict())
            root.write_checkpoint(fixture._accepted_checkpoint("C1", parent="C0", seq=1,
                assignment={"pkg-a": "2.0.0", "pkg-b": "1.0.0"}, changed={"pkg-a": "2.0.0"}))
            c = migration.load_checkpoint(root.root, "C1")
            c["audit"] = {"status": "UNKNOWN"}
            migration.save_checkpoint(root.root, c)
            calls = []

            def audit_once(run_dir, run, config, args):
                calls.append(run["auditRecovery"])
                c = migration.load_checkpoint(run_dir, "C1")
                c["audit"] = {"status": "UNKNOWN", "retryableReason": "ECONNRESET"} if len(calls) == 1 else {"status": "PASS"}
                migration.save_checkpoint(run_dir, c)
                return 0

            with patch.object(migration, "_audit_once_locked", side_effect=audit_once), patch.object(migration.time, "sleep"):
                migration._finish_locked(root.root, migration.load_run(root.root), migration.load_config(root.root))
            self.assertEqual(len(calls), 2)
            run = migration.load_run(root.root)
            self.assertEqual(run["terminalOutcome"]["outcome"], "PARTIAL_VERIFIED")
            self.assertEqual(next(iter(run["auditRecovery"].values()))["attempts"], 2)
            self.assertEqual(run["activeCheckpointId"], "C1")

    def test_audit_restart_resumes_last_attempt_and_persistent_outage_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fixture._RunDir(Path(tmp))
            config = fixture._config_dict(budget={"maxInfraRetries": 1, "runBudgetMinutes": 0})
            root.write_config(config)
            root.write_checkpoint(fixture._checkpoint_dict(audit={"status": "UNKNOWN", "retryableReason": "ETIMEDOUT"}))
            scope = "C0:snap-0001:policyhash-0001"
            root.write_run(fixture._run_dict(auditRecovery={scope: {"attempts": 2, "limit": 2, "inFlight": True}}))
            with patch.object(migration, "_audit_once_locked", return_value=0) as once, patch.object(migration.time, "sleep"):
                migration._audit_locked(root.root, migration.load_run(root.root), config, None)
                self.assertEqual(once.call_count, 1)
                self.assertEqual(migration.load_run(root.root)["auditRecovery"][scope]["attempts"], 2)
                migration._audit_locked(root.root, migration.load_run(root.root), config, None)
                self.assertEqual(once.call_count, 1)
            self.assertEqual(migration.load_checkpoint(root.root, "C0")["audit"]["status"], "UNKNOWN")

    def test_persistent_outage_uses_only_reserved_budget_across_finish_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fixture._RunDir(Path(tmp))
            config = fixture._config_dict(budget={"maxInfraRetries": 2, "runBudgetMinutes": 0})
            root.write_config(config)
            root.write_run(fixture._run_dict(phase="TERMINAL", terminal="SCOPE_EXHAUSTED"))
            root.write_checkpoint(fixture._checkpoint_dict(audit={"status": "UNKNOWN", "retryableReason": "ECONNRESET"}))
            with patch.object(migration, "_audit_once_locked", return_value=0) as once, patch.object(migration.time, "sleep"):
                migration._finish_locked(root.root, migration.load_run(root.root), config)
                self.assertEqual(once.call_count, 3)
                migration._finish_locked(root.root, migration.load_run(root.root), config)
                self.assertEqual(once.call_count, 3)
            run = migration.load_run(root.root)
            self.assertEqual(run["terminalOutcome"]["outcome"], "BLOCKED_BASELINE")
            self.assertEqual(next(iter(run["auditRecovery"].values()))["attempts"], 3)

    def test_real_audit_handoff_switches_to_canonical_with_reserved_attempts(self):
        from datetime import datetime, timezone
        import manual_dependency_audit
        with tempfile.TemporaryDirectory() as tmp:
            root = fixture._RunDir(Path(tmp))
            config = fixture._config_dict(budget={"maxInfraRetries": 2, "runBudgetMinutes": 0})
            root.write_config(config)
            root.write_run(fixture._run_dict())
            root.write_checkpoint(fixture._checkpoint_dict(audit={"status": "UNKNOWN"}))
            engines = []

            def build_report(project, name, **kwargs):
                engines.append(kwargs["yarn_audit_engine"])
                audit = {"engine": engines[-1], "trusted": True,
                         "canonicalInventory": {"complete": True}, "lagOkPct": 90,
                         "packageTotals": {"critical": 0, "high": 0, "unknown": 0},
                         "advisoryTotals": {"unknown": 0}}
                if len(engines) == 1:
                    audit.update(engine="npm-lock-bridge", accuracy="npm-resolved-approximation")
                if len(engines) == 2:
                    audit["notes"] = ["ECONNRESET"]
                return {"auditComplete": len(engines) != 2, "lagComplete": True,
                        "dependencyInputHash": "sealed", "generatedAt": datetime.now(timezone.utc).isoformat(),
                        "policy": {"targetLevel": "yellow", "maxKnownCritical": 0,
                                   "maxKnownHigh": 1, "minLagOkPct": 80, "lagPolicyMonths": 12}, "audit": audit}

            args = migration.argparse.Namespace(registry="", lag_months=None, min_lag_ok_pct=None,
                max_known_high=None, yarn_audit_engine="auto")
            with patch.object(migration, "_open_checkpoint_source", return_value=migration.argparse.Namespace(root=str(root.root))), \
                 patch.object(manual_dependency_audit, "build_report", side_effect=build_report), \
                 patch.object(manual_dependency_audit, "markdown", return_value="evidence"), \
                 patch.object(migration.time, "sleep"):
                migration._audit_locked(root.root, migration.load_run(root.root), config, args)
            self.assertEqual(engines, ["auto", "yarn-inventory", "yarn-inventory"])
            self.assertEqual(migration.load_checkpoint(root.root, "C0")["audit"]["status"], "PASS")
            self.assertEqual(next(iter(migration.load_run(root.root)["auditRecovery"].values()))["attempts"], 3)
            self.assertTrue((root.root / "audit/C0/audit-report.json").exists())

    def test_audit_classifier_rejects_findings_and_invalid_evidence(self):
        for text in ("E401", "registry mismatch ETIMEDOUT", "YARN_NPM_BRIDGE_DRIFT", "ETARGET", "EPERM EACCES"):
            self.assertFalse(transient_audit_reason({"auditComplete": False, "audit": {"notes": [text]}}))
        self.assertFalse(transient_audit_reason({"auditComplete": True, "audit": {"notes": ["ETIMEDOUT"]}}))
        self.assertEqual(transient_audit_reason({"auditComplete": False, "audit": {"notes": ["npm error ECONNRESET"]}}), "ECONNRESET")

    def test_adaptive_discovery_extends_useful_wave_but_preserves_explicit_cap(self):
        current = dict.fromkeys(["a", "b", "c"], "1.0.0")
        calls = []
        def probe(project, name, declared, runtime_env, node_version):
            calls.append(name)
            return "2.0.0", {"package": name, "status": "discovered"}
        with patch.object(migration, "_discover_one_target", side_effect=probe):
            targets, rows = migration._discover_targets(Path("."), current, None, max_packages=1, adaptive=True)
            self.assertEqual(targets, dict.fromkeys(current, "2.0.0"))
            self.assertEqual(calls, list(current))
            calls.clear()
            targets, rows = migration._discover_targets(Path("."), current, None, max_packages=1)
            self.assertEqual(targets, {"a": "2.0.0"})
            self.assertEqual(calls, ["a"])
            self.assertEqual(sum(r["status"] == "discovery-budget-skipped" for r in rows), 2)

    def test_discovery_stops_without_new_targets_and_keeps_remainder(self):
        current = dict.fromkeys(["a", "b", "c"], "1.0.0")
        with patch.object(migration, "_discover_one_target", return_value=(None, {"package": "a", "status": "up-to-date"})) as probe:
            targets, rows = migration._discover_targets(Path("."), current, None, max_packages=1, adaptive=True)
        self.assertFalse(targets)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(sum(r["status"] == "discovery-budget-skipped" for r in rows), 2)


if __name__ == "__main__":
    unittest.main()
