"""Regressions found during the v0.2.181 re-acceptance."""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import iterative_migration as migration
from baseline_constraint_verifier import BaselineVerifyConfig, BaselineVerifyResult
import importlib.util

def _helpers(filename):
    spec = importlib.util.spec_from_file_location("reaccept_" + filename, Path(__file__).with_name(filename + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

closure = _helpers("test_iterative_cohort_closure")
_plan, _fingerprint = closure._plan, closure._fingerprint
budget = _helpers("test_iterative_materialize_retry_budget")
_config_dict, _run_dict, _checkpoint_dict = budget._config_dict, budget._run_dict, budget._checkpoint_dict
_accepted_checkpoint = _helpers("test_iterative_finish_baseline_and_feedback_validation")._accepted_checkpoint


class ReacceptanceTests(unittest.TestCase):
    def test_cross_half_companions_are_reached_after_exact_split_blocks(self):
        names = list("abcdefgh")
        old, desired = dict.fromkeys(names, "1.0.0"), dict.fromkeys(names, "1.1.0")
        rejected = set()
        def reject(group):
            assignment = dict(old)
            assignment.update({n: desired[n] for n in group})
            rejected.add(_fingerprint(assignment))
            if len(group) > 1:
                middle = len(group) // 2
                reject(group[:middle]); reject(group[middle:])
        reject(names)
        # The only permitted pair straddles the positional split. Other names
        # have deterministic singleton nogoods; a and h only have EXACT blocks.
        plan, _ = _plan(old, desired, blocks=rejected,
                        clauses=[{n: desired[n]} for n in names if n not in "ah"])
        self.assertIsNotNone(plan)
        self.assertEqual({"a", "h"}, set(plan.packages))

    def test_finish_ignores_detached_and_version_neutral_checkpoints(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            migration.save_run(root, _run_dict(activeCheckpointId="C1"))
            migration.save_config(root, _config_dict())
            c0 = _checkpoint_dict()
            c0["audit"] = {"status": "PASS"}
            migration.save_checkpoint(root, c0)
            migration.save_checkpoint(root, _accepted_checkpoint("C1", parent="C0", seq=1,
                assignment=c0["fullAssignment"], changed={}))
            migration.save_checkpoint(root, _accepted_checkpoint("C9", parent="C0", seq=9,
                assignment={"pkg-a": "2.0.0"}, changed={"pkg-a": "2.0.0"}))
            migration._finish_locked(root, migration.load_run(root), migration.load_config(root))
            result = migration.load_run(root)["terminalOutcome"]
            self.assertEqual("NO_VERIFIED_UPGRADE", result["outcome"])
            self.assertEqual(0, result["acceptedCheckpoints"])

    def test_inconclusive_verify_stops_and_retries_the_same_repaired_trial(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            trial = root / "trial"
            trial.mkdir()
            (trial / "package.json").write_text("{}", encoding="utf-8")
            config = _config_dict()
            migration.save_config(root, config)
            migration.save_run(root, _run_dict(activeCandidateId="candidate", phase="VERIFYING"))
            migration.save_checkpoint(root, _checkpoint_dict())
            migration.save_candidate(root, {"schemaVersion": 1, "candidateId": "candidate", "runId": "iter-000000000001",
                "baseCheckpointId": "C0", "stage": "VERIFYING", "attemptId": 1,
                "fullAssignment": {"pkg-a": "2.0.0"},
                "materializationRefs": {"workspaceRoot": str(trial), "projectRelative": "."}})
            result = BaselineVerifyResult(False, "infrastructure", "registry temporarily unavailable")
            with mock.patch.object(migration, "_assert_runtime_unchanged"), \
                 mock.patch.object(migration, "verify_config_from", return_value=BaselineVerifyConfig(commands=("node check.js",))), \
                 mock.patch.object(migration, "verify_assignment", return_value=result) as verify, \
                 mock.patch.object(migration, "_emit_status"):
                for _ in range(3):
                    migration._verify_exact_locked(root, migration.load_run(root), config, SimpleNamespace())
                self.assertEqual(3, verify.call_count)
            run = migration.load_run(root)
            candidate = migration.load_candidate(root)
            self.assertEqual("READY", run["phase"])
            self.assertEqual("verify-exact", run["infraBlocked"]["operation"])
            self.assertEqual("VERIFYING", candidate["stage"])
            self.assertEqual(1, candidate["attemptId"])
            self.assertFalse(migration.load_ledger(root).get("blocks"))
            # Simulate a crash after candidate persistence but before run stop.
            interrupted = dict(run, phase="VERIFYING", infraBlocked=None)
            migration.save_run(root, interrupted)
            with mock.patch.object(migration, "_assert_runtime_unchanged"), mock.patch.object(migration, "verify_assignment") as verify:
                migration._verify_exact_locked(root, interrupted, config, SimpleNamespace())
                verify.assert_not_called()
            run = migration.load_run(root)
            migration._plan_next_locked(root, run, config, SimpleNamespace(retry_infra=True))
            self.assertEqual("VERIFYING", migration.load_run(root)["phase"])
            resumed = migration.load_candidate(root)
            self.assertEqual(candidate["candidateId"], resumed["candidateId"])
            self.assertEqual(candidate["materializationRefs"], resumed["materializationRefs"])
            self.assertEqual(0, resumed["verifyInfraFailures"])

if __name__ == "__main__":
    unittest.main()
