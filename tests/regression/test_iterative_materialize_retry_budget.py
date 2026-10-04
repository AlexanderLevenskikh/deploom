"""B1/B2: bounded infra retries, ETARGET exact-target deferral, per-revision
repair budgets, and durable structured materialize attempt results.

Reproductions addressed by this file:

* ``materialize.failed`` returned exit 0 while leaving the candidate PLANNED,
  so the Desktop coordinator re-invoked materialize forever (B1).
* ``maxInfraRetries`` was never applied: identical infra installs replayed
  unbounded (B1).
* ``repairAttemptsForRevision`` was a global ledger counter, so a NEW candidate
  inherited the previous revision's spent budget and could be rejected after a
  single failure (B2).
* ETARGET was only deferred by the driver sending INCONCLUSIVE feedback; the
  remaining packages could not reach a verified checkpoint on their own (B1#3).

All state transitions are exercised through the REAL Python controller with
patched outer boundaries (install/verify/snapshot), which is the documented
audit method for the control loop.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from baseline_constraint_verifier import BaselineVerifyConfig, BaselineVerifyResult

from iterative_migration import (
    SCHEMA_VERSION,
    _plan_next_locked,
    _materialize_locked,
    _verify_exact_locked,
    _apply_feedback_locked,
    _precheck_locked,
    assignment_fingerprint,
    _attempt_revision_key,
    load_candidate,
    load_ledger,
    load_run,
    save_candidate,
    save_checkpoint,
    save_config,
    save_run,
)


def _config_dict(**overrides) -> dict:
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "projectDir": "C:/projects/sample-app",
        "projectName": "sample-app",
        "workspaceId": "ws-1",
        "projectId": "sample-app",
        "targetLevel": "yellow",
        "cohortMaxPackages": 24,
        "targets": {"pkg-a": "2.0.0"},
        "verifyConfig": {"commands": ["node check.js"]},
        "policyHash": "policyhash-0001",
        "budget": {
            "runBudgetMinutes": 120,
            "maxRepairAttemptsPerRevision": 2,
            "maxInfraRetries": 2,
            "phaseTimeoutSeconds": 1200,
        },
        "createdAt": "2026-10-04T00:00:00Z",
    }
    value.update(overrides)
    return value


def _run_dict(**overrides) -> dict:
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": "iter-000000000001",
        "workspaceId": "ws-1",
        "projectId": "sample-app",
        "generation": 1,
        "targetPolicyHash": "policyhash-0001",
        "initialSnapshotKey": "snap-0001",
        "activeCheckpointId": "C0",
        "activeCandidateId": None,
        "phase": "READY",
        "terminal": None,
        "budgetLedger": {
            "candidateAttempts": 0,
            "repairAttempts": 0,
            "repairAttemptsForRevision": 0,
            "infraRetries": 0,
            "repeatedAttempts": 0,
        },
        "leaseOwner": "",
        "deadlineAt": "2999-01-01T00:00:00Z",
        "heartbeatAt": "2026-10-04T00:00:00Z",
        "createdAt": "2026-10-04T00:00:00Z",
        "updatedAt": "2026-10-04T00:00:00Z",
        "toolBuildId": "tool-0001",
    }
    value.update(overrides)
    return value


def _checkpoint_dict(assignment=None, **overrides) -> dict:
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "checkpointId": "C0",
        "seq": 0,
        "parentCheckpointId": None,
        "status": "VERIFIED",
        "sourceSnapshotKey": "snap-0001",
        "sourceSnapshotContainer": "C:/project/snapshots/snap-0001",
        "projectRelative": ".",
        "fullAssignment": assignment if assignment is not None else {"pkg-a": "1.0.0", "pkg-b": "1.0.0"},
        "lockfileHash": "lock-1",
        "manifestHash": "manifest-1",
        "observedResolvedHash": "resolved-1",
        "proofRefs": {},
        "audit": {"status": "UNKNOWN", "evidenceRef": ""},
        "verification": {"kind": "passed", "status": "passed", "commands": ["node check.js"], "failingCommands": []},
        "createdAt": "2026-10-04T00:00:00Z",
    }
    value.update(overrides)
    return value


class MaterializeRetryBudgetBase(unittest.TestCase):
    def make_run_dir(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="iter-materialize-"))
        (root / "trial").mkdir(parents=True, exist_ok=True)
        (root / "checkpoints").mkdir(parents=True, exist_ok=True)
        save_run(root, _run_dict())
        save_config(root, _config_dict())
        save_checkpoint(root, _checkpoint_dict())
        return root

    def install_failure(self, stdout: str, returncode: int = 1) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def install_success(self) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="added 1 package", stderr="")

    @staticmethod
    def materialize_trial_tree_ok(snapshot, ws_root, timeout_seconds):
        ws_root = Path(ws_root)
        ws_root.mkdir(parents=True, exist_ok=True)
        (ws_root / "package.json").write_text('{"name": "trial", "private": true}', encoding="utf-8")

    @contextmanager
    def materialize_patches(self, install):
        patches = [
            mock.patch("iterative_migration.run_budget_ok", return_value=True),
            mock.patch("iterative_migration._assert_runtime_unchanged"),
            mock.patch("iterative_migration.open_source_snapshot", return_value=SimpleNamespace(root=str(self.tmp))),
            mock.patch("iterative_migration._materialize_trial_tree", side_effect=self.materialize_trial_tree_ok),
            mock.patch("iterative_migration._apply_assignment", return_value={}),
            mock.patch("iterative_migration._run_install", return_value=install),
            mock.patch("iterative_migration._runtime_env", return_value={}),
            mock.patch("iterative_migration._emit_status"),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            yield

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="iter-patches-"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)


class InfraRetryBoundingTests(MaterializeRetryBudgetBase):
    def test_infra_retries_bounded_no_third_hidden_install_and_checkpoint_intact(self) -> None:
        run_dir = self.make_run_dir()
        config = _config_dict(targets={"pkg-a": "2.0.0"})
        save_config(run_dir, config)
        _plan_next_locked(run_dir, load_run(run_dir), config)
        candidate = load_candidate(run_dir)
        self.assertEqual("2.0.0", candidate["fullAssignment"]["pkg-a"])

        install = self.install_failure("npm ERR! code ECONNRESET\nnpm ERR! network request to registry failed\n")
        events: list[dict] = []
        with self.materialize_patches(install):
            with mock.patch("iterative_migration._emit_status", side_effect=events.append) as emit:
                self.assertEqual(0, _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))
                self.assertEqual(0, _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))

        # Two permitted infra attempts (the initial + one retry at limit 2) run;
        # the third must be terminal, never a hidden third install.
        fu = [e for e in events if e.get("event") == "materialize.failed"]
        self.assertEqual(2, len(fu))
        self.assertEqual("infrastructure", fu[0]["kind"])
        self.assertTrue(fu[0]["retryable"])
        self.assertEqual(1, fu[0]["retryCount"])
        self.assertEqual(2, fu[0]["maxRetries"])
        self.assertEqual(2, fu[1]["retryCount"])
        self.assertTrue(fu[1]["retryable"])

        candidate = load_candidate(run_dir)
        self.assertEqual("PLANNED", candidate["stage"])
        self.assertEqual(2, candidate["attemptResult"]["retryCount"])
        self.assertTrue(candidate["attemptResult"]["retryable"])
        # A restart between retries must NOT reset the spent budget.
        self.assertEqual(["infrastructure", "infrastructure"], candidate["attemptResult"]["attemptSequence"])

        # Third failure: terminal conclusion, recoverable infra block, stop.
        with self.materialize_patches(install):
            with mock.patch("iterative_migration._emit_status", side_effect=events.append) as emit:
                self.assertEqual(0, _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))
        fu = [e for e in events if e.get("event") == "materialize.failed"]
        self.assertTrue(fu[-1]["terminalFailure"])
        self.assertFalse(fu[-1]["retryable"])

        candidate = load_candidate(run_dir)
        self.assertEqual("REJECTED", candidate["stage"])
        self.assertTrue(candidate["attemptResult"]["terminalFailure"])
        ledger = load_ledger(run_dir)
        self.assertEqual(1, len(ledger.get("infraBlocks", [])))
        run = load_run(run_dir)
        self.assertEqual("READY", run["phase"])
        self.assertIsNotNone(run.get("infraBlocked"))
        # The last verified checkpoint is untouched.
        self.assertEqual("C0", run["activeCheckpointId"])

        # A fourth materialize must be REFUSED, not silently exit 0.
        from iterative_migration import InvalidInputError
        with ExitStack() as _stack:
            with self.assertRaises(InvalidInputError):
                _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))

        # plan-next must STOP with a recoverable infra-blocked event, never
        # re-schedule the identical candidate and never claim scope exhaustion.
        events2: list[dict] = []
        with mock.patch("iterative_migration._emit_status", side_effect=events2.append):
            _plan_next_locked(run_dir, load_run(run_dir), config)
        self.assertIn("plan-next.infra-blocked", [e.get("event") for e in events2])
        self.assertIsNone(load_candidate(run_dir))
        run = load_run(run_dir)
        self.assertIsNone(run.get("terminal"), "infra stop must not be terminal")
        self.assertEqual("READY", run["phase"])

    def test_manual_retry_after_cause_change_opens_fresh_sequence(self) -> None:
        run_dir = self.make_run_dir()
        config = _config_dict(targets={"pkg-a": "2.0.0"})
        save_config(run_dir, config)
        _plan_next_locked(run_dir, load_run(run_dir), config)

        install = self.install_failure("npm ERR! code ECONNRESET\nnetwork request failed\n")
        with mock.patch("iterative_migration.run_budget_ok", return_value=True), \
             mock.patch("iterative_migration._assert_runtime_unchanged"), \
             mock.patch("iterative_migration._materialize_trial_tree", side_effect=self.materialize_trial_tree_ok), \
             mock.patch("iterative_migration._apply_assignment", return_value={}), \
             mock.patch("iterative_migration._run_install", return_value=install), \
             mock.patch("iterative_migration._runtime_env", return_value={}), \
             mock.patch("iterative_migration.open_source_snapshot", return_value=SimpleNamespace(root=str(self.tmp))), \
             mock.patch("iterative_migration._emit_status"):
            _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))
            _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))
            _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))

        run = load_run(run_dir)
        self.assertIsNotNone(run.get("infraBlocked"))
        ledger = load_ledger(run_dir)
        self.assertEqual(1, len(ledger.get("infraBlocks", [])))

        # User continuation after the cause changed: plan-next --retry-infra
        # clears the current-scope blocks, so the SAME target is re-proposed and
        # a FRESH infra budget applies.
        args = SimpleNamespace(retry_infra=True)
        events: list[dict] = []
        with mock.patch("iterative_migration._emit_status", side_effect=events.append):
            _plan_next_locked(run_dir, load_run(run_dir), config, args)
        candidate = load_candidate(run_dir)
        self.assertIsNotNone(candidate, "retry-infra must re-propose the target after the cause changed")
        self.assertEqual("2.0.0", candidate["fullAssignment"]["pkg-a"])
        run = load_run(run_dir)
        self.assertIsNone(run.get("infraBlocked"))
        ledger = load_ledger(run_dir)
        self.assertEqual([], ledger.get("infraBlocks", []),
                         "current-scope infra blocks must be cleared by the explicit continuation")


class ExactTargetDeferralTests(MaterializeRetryBudgetBase):
    def test_etarget_defers_exact_target_and_remaining_reaches_checkpoint_without_driver_feedback(self) -> None:
        run_dir = self.make_run_dir()
        incumbent = {"pkg-a": "1.0.0", "pkg-b": "1.0.0"}
        save_checkpoint(run_dir, _checkpoint_dict(assignment=incumbent))
        config = _config_dict(targets={"pkg-a": "99.99.99", "pkg-b": "1.1.0"})
        save_config(run_dir, config)
        _plan_next_locked(run_dir, load_run(run_dir), config)
        candidate = load_candidate(run_dir)
        changed = candidate["delta"]["changed"]
        self.assertTrue({"pkg-a": "99.99.99"}.items() <= changed.items())

        # ETARGET failure of the exact target -> conclude the candidate by
        # itself, defer ONLY a@99.99.99, never materialize a third time.
        install = self.install_failure(
            "npm ERR! code ETARGET\nnpm ERR! notarget No matching version found for pkg-a@99.99.99.\n"
        )
        events: list[dict] = []
        with self.materialize_patches(install):
            with mock.patch("iterative_migration._emit_status", side_effect=events.append):
                self.assertEqual(0, _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))
        fu = [e for e in events if e.get("event") == "materialize.failed"]
        self.assertEqual("resolver", fu[-1]["kind"])
        self.assertTrue(fu[-1]["concluded"])
        self.assertTrue(fu[-1]["exactTargetDeferred"])
        candidate = load_candidate(run_dir)
        self.assertEqual("REJECTED", candidate["stage"])
        run = load_run(run_dir)
        self.assertEqual("READY", run["phase"])
        self.assertFalse(run.get("infraBlocked"), "ETARGET is a resolver deferral, not an infra stop")

        # The next plan must reach b:1.1.0 WITHOUT any apply-feedback.
        events2: list[dict] = []
        with mock.patch("iterative_migration._emit_status", side_effect=events2.append):
            _plan_next_locked(run_dir, load_run(run_dir), config)
        candidate2 = load_candidate(run_dir)
        self.assertIsNotNone(candidate2)
        self.assertEqual({"pkg-b": "1.1.0"}, candidate2["delta"]["changed"])
        self.assertNotIn("pkg-a", candidate2["delta"]["changed"])

        # Materialize b successfully, precheck passes, authoritative verify passes.
        events3: list[dict] = []
        with mock.patch("iterative_migration.run_budget_ok", return_value=True), \
             mock.patch("iterative_migration._assert_runtime_unchanged"), \
             mock.patch("iterative_migration.open_source_snapshot", return_value=SimpleNamespace(root=str(self.tmp))), \
             mock.patch("iterative_migration._materialize_trial_tree", side_effect=self.materialize_trial_tree_ok), \
             mock.patch("iterative_migration._apply_assignment", return_value={}), \
             mock.patch("iterative_migration._run_install", return_value=self.install_success()), \
             mock.patch("iterative_migration._runtime_env", return_value={}), \
             mock.patch("iterative_migration.observed_resolved_assignment", return_value={"pkg-a": "1.0.0", "pkg-b": "1.1.0"}), \
             mock.patch("iterative_migration.observed_resolved_hash", return_value="resolved-new"), \
             mock.patch("iterative_migration.capture_source_snapshot", return_value=SimpleNamespace(key="trial-key")), \
             mock.patch("iterative_migration._emit_status", side_effect=events3.append):
            self.assertEqual(0, _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))
        self.assertIn("materialize.done", [e.get("event") for e in events3])

        with mock.patch("iterative_migration.run_budget_ok", return_value=True), \
             mock.patch("iterative_migration._assert_runtime_unchanged"), \
             mock.patch("iterative_migration.discover_baseline_project_checks", return_value=("node check.js",)), \
             mock.patch("iterative_migration._command_argv", return_value=["node", "check.js"]), \
             mock.patch("iterative_migration._run", return_value=SimpleNamespace(returncode=0, stdout="all good", stderr="")), \
             mock.patch("iterative_migration._runtime_env", return_value={}), \
             mock.patch("iterative_migration._emit_status"):
            self.assertEqual(0, _precheck_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))
        self.assertEqual("PRECHECKED", load_candidate(run_dir)["stage"])

        ok_result = BaselineVerifyResult(
            True, "passed", "project verified",
            observed_resolved_versions={"pkg-a": "1.0.0", "pkg-b": "1.1.0"},
            observed_resolved_hash="resolved-new", resolved_state_key="state-new",
        )
        events4: list[dict] = []
        with mock.patch("iterative_migration.run_budget_ok", return_value=True), \
             mock.patch("iterative_migration._assert_runtime_unchanged"), \
             mock.patch("iterative_migration.verify_config_from", return_value=BaselineVerifyConfig(commands=("node check.js",))), \
             mock.patch("iterative_migration.verify_assignment", return_value=ok_result), \
             mock.patch("iterative_migration._runtime_env", return_value={}), \
             mock.patch("iterative_migration.capture_durable_source_snapshot", return_value=SimpleNamespace(key="snap-c1", container=run_dir / "sources", git_head=None)), \
             mock.patch("iterative_migration.resolve_project_relative", return_value=Path(".")), \
             mock.patch("iterative_migration.manifest_and_lock_hashes", return_value=("manifest-c1", "lock-c1", None)), \
             mock.patch("iterative_migration._emit_status", side_effect=events4.append):
            self.assertEqual(0, _verify_exact_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200)))
        self.assertIn("checkpoint.accepted", [e.get("event") for e in events4])
        run = load_run(run_dir)
        self.assertEqual("C1", run["activeCheckpointId"])
        c1 = json.loads((run_dir / "checkpoints" / "C1.json").read_text(encoding="utf-8"))
        self.assertEqual({"pkg-b": "1.1.0"}, c1["acceptedDelta"]["changed"])
        self.assertEqual("1.0.0", c1["fullAssignment"]["pkg-a"])


class PerRevisionRepairBudgetTests(MaterializeRetryBudgetBase):
    @contextmanager
    def _verify_patches(self, result):
        patches = [
            mock.patch("iterative_migration.run_budget_ok", return_value=True),
            mock.patch("iterative_migration._assert_runtime_unchanged"),
            mock.patch("iterative_migration.verify_config_from", return_value=BaselineVerifyConfig(commands=("node check.js",))),
            mock.patch("iterative_migration.verify_assignment", return_value=result),
            mock.patch("iterative_migration._runtime_env", return_value={}),
            mock.patch("iterative_migration.capture_durable_source_snapshot", return_value=SimpleNamespace(key="snap", container=Path("sources"), git_head=None)),
            mock.patch("iterative_migration.resolve_project_relative", return_value=Path(".")),
            mock.patch("iterative_migration.manifest_and_lock_hashes", return_value=("m", "l", None)),
            mock.patch("iterative_migration._emit_status"),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            yield

    def _materialize_and_precheck_ok(self, run_dir, config):
        with mock.patch("iterative_migration.run_budget_ok", return_value=True), \
             mock.patch("iterative_migration._assert_runtime_unchanged"), \
             mock.patch("iterative_migration.open_source_snapshot", return_value=SimpleNamespace(root=str(self.tmp))), \
             mock.patch("iterative_migration._materialize_trial_tree", side_effect=self.materialize_trial_tree_ok), \
             mock.patch("iterative_migration._apply_assignment", return_value={}), \
             mock.patch("iterative_migration._run_install", return_value=self.install_success()), \
             mock.patch("iterative_migration._runtime_env", return_value={}), \
             mock.patch("iterative_migration.observed_resolved_assignment", side_effect=lambda p, assignment: assignment), \
             mock.patch("iterative_migration.observed_resolved_hash", return_value="h"), \
             mock.patch("iterative_migration.capture_source_snapshot", return_value=SimpleNamespace(key="trial-key")), \
             mock.patch("iterative_migration._emit_status"):
            _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))
        with mock.patch("iterative_migration.run_budget_ok", return_value=True), \
             mock.patch("iterative_migration._assert_runtime_unchanged"), \
             mock.patch("iterative_migration.discover_baseline_project_checks", return_value=("node check.js",)), \
             mock.patch("iterative_migration._command_argv", return_value=["node", "check.js"]), \
             mock.patch("iterative_migration._run", return_value=SimpleNamespace(returncode=0, stdout="ok", stderr="")), \
             mock.patch("iterative_migration._runtime_env", return_value={}), \
             mock.patch("iterative_migration._emit_status"):
            _precheck_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))

    def _feedback(self, candidate, kind, attempt_id, reason=""):
        return {
            "schemaVersion": SCHEMA_VERSION,
            "runId": candidate["runId"],
            "candidateId": candidate["candidateId"],
            "baseCheckpointId": candidate["baseCheckpointId"],
            "attemptId": attempt_id,
            "kind": kind,
            "reason": reason,
            "changedFiles": [],
            "diagnosticsRefs": [],
            "proposedScope": {},
            "proposedConstraints": {},
        }

    def test_new_candidate_gets_fresh_repair_budget_and_kill_on_last_attempt_never_adds_or_resets(self) -> None:
        run_dir = self.make_run_dir()
        incumbent = {"pkg-a": "1.0.0", "pkg-b": "1.0.0"}
        save_checkpoint(run_dir, _checkpoint_dict(assignment=incumbent))
        config = _config_dict(targets={"pkg-a": "2.0.0", "pkg-b": "1.1.0"}, cohortMaxPackages=1)
        save_config(run_dir, config)

        # --- Candidate 1 needs one repair, then is concluded. ---
        _plan_next_locked(run_dir, load_run(run_dir), config)
        c1 = load_candidate(run_dir)
        self.assertEqual(1, len(c1["delta"]["changed"]))
        self._materialize_and_precheck_ok(run_dir, config)
        fail1 = BaselineVerifyResult(False, "project", "check failed",
                                     project_failures=())
        with self._verify_patches(fail1):
            _verify_exact_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))
        c1 = load_candidate(run_dir)
        self.assertEqual("REPAIRING", c1["stage"])
        self.assertEqual(1, c1["attemptId"])
        ledger = load_ledger(run_dir)
        rev1 = _attempt_revision_key(str(c1["baseCheckpointId"]), assignment_fingerprint(c1["fullAssignment"]))
        self.assertEqual(1, ledger["counters"]["repairAttemptsByRevision"][rev1])

        # Conclude candidate 1 (simulated successful repair at attempt 1).
        with mock.patch("iterative_migration._emit_status"):
            _apply_feedback_locked(run_dir, load_run(run_dir), config, self._feedback(c1, "READY_FOR_VERIFY", attempt_id=1))
        with self._verify_patches(BaselineVerifyResult(True, "passed", "ok")):
            _verify_exact_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))

        # --- Candidate 2: a DIFFERENT revision must NOT inherit the spent
        # per-revision budget of candidate 1. ---
        _plan_next_locked(run_dir, load_run(run_dir), config)
        c2 = load_candidate(run_dir)
        self.assertEqual(1, len(c2["delta"]["changed"]))
        self.assertNotEqual(c2["delta"]["changed"], c1["delta"]["changed"])
        self._materialize_and_precheck_ok(run_dir, config)
        with self._verify_patches(fail1):
            _verify_exact_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))
        c2 = load_candidate(run_dir)
        self.assertEqual("REPAIRING", c2["stage"], "new revision must not be rejected after its first failure")
        self.assertEqual(1, c2["attemptId"])
        ledger = load_ledger(run_dir)
        rev2 = _attempt_revision_key(str(c2["baseCheckpointId"]), assignment_fingerprint(c2["fullAssignment"]))
        self.assertEqual(1, ledger["counters"]["repairAttemptsByRevision"][rev2])
        self.assertNotEqual(rev2, rev1)
        # Candidate 1's spent count is preserved, not clobbered by candidate 2.
        self.assertEqual(1, ledger["counters"]["repairAttemptsByRevision"][rev1])

        # --- Kill-on-last-attempt: the final (attempt 1) verify is interrupted
        # mid-run (candidate stays VERIFYING, ledger already 1) and RESUMED.
        # Resume must re-run the SAME attempt, not add one and not reset. ---
        c2 = load_candidate(run_dir)
        c2["stage"] = "VERIFYING"
        c2["updatedAt"] = "2026-10-04T00:00:00Z"
        save_candidate(run_dir, c2)
        with self._verify_patches(fail1):
            _verify_exact_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))
        c2 = load_candidate(run_dir)
        self.assertEqual("REJECTED", c2["stage"], "resumed last attempt exhausts the 2-attempt budget")
        self.assertEqual(1, c2["attemptId"], "the last attempt must not be re-added")
        ledger = load_ledger(run_dir)
        self.assertEqual(2, ledger["counters"]["repairAttemptsByRevision"][rev2],
                         "spent count must not reset on the resume")
        # No extra repair request may be emitted for the exhausted attempt: only
        # the original request from the first failure of attempt 1 remains.
        repair_requests = json.loads((run_dir / "repair-requests.json").read_text(encoding="utf-8"))["requests"]
        c2_requests = [r for r in repair_requests if r["candidateId"] == c2["candidateId"]]
        self.assertEqual(1, len(c2_requests),
                         "the exhausted resume must not add a second repair request")
        self.assertEqual(1, c2_requests[0]["attemptId"])

        # The exhausted revision is now a learned NOT_ACTIONABLE block: plan-next
        # must NOT re-schedule it and must NOT create a new candidate. The run
        # reaches a real end while the last verified checkpoint stays intact.
        events5: list[dict] = []
        with mock.patch("iterative_migration._emit_status", side_effect=events5.append):
            _plan_next_locked(run_dir, load_run(run_dir), config)
        self.assertIn("plan-next.no-candidate", [e.get("event") for e in events5])
        run = load_run(run_dir)
        self.assertIn(run.get("terminal"), {"SCOPE_EXHAUSTED", "NO_ACTIONABLE"})
        c1_checkpoint = json.loads((run_dir / "checkpoints" / "C1.json").read_text(encoding="utf-8"))
        self.assertEqual("VERIFIED", c1_checkpoint["status"])

    def test_repeated_identical_gate_stops_with_reason_and_evidence(self) -> None:
        # An unknown/canonical failure must conclude on the first occurrence and
        # never loop, with durable evidence on the candidate.
        run_dir = self.make_run_dir()
        config = _config_dict(targets={"pkg-a": "2.0.0"})
        save_config(run_dir, config)
        _plan_next_locked(run_dir, load_run(run_dir), config)
        install = self.install_failure("Some totally unexpected resolver prose here\n")
        events: list[dict] = []
        with self.materialize_patches(install):
            with mock.patch("iterative_migration._emit_status", side_effect=events.append):
                _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))
        fu = [e for e in events if e.get("event") == "materialize.failed"]
        self.assertEqual("unknown", fu[-1]["kind"])
        candidate = load_candidate(run_dir)
        self.assertEqual("REJECTED", candidate["stage"])
        self.assertIn("outputTail", candidate["attemptResult"]["lastFailureEvidence"])
        # No endless hidden replay.
        with self.materialize_patches(install):
            with mock.patch("iterative_migration._emit_status", side_effect=events.append):
                from iterative_migration import InvalidInputError
                with self.assertRaises(InvalidInputError):
                    _materialize_locked(run_dir, load_run(run_dir), config, SimpleNamespace(timeout_seconds=1200))


if __name__ == "__main__":
    unittest.main()
