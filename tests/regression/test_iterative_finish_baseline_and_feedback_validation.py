from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iterative_migration import (
    SCHEMA_VERSION,
    InvalidInputError,
    _accepted_upgrade_count,
    _apply_feedback_locked,
    _finish_locked,
    _is_baseline_checkpoint,
    load_candidate,
    load_config,
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
        "targets": {"pkg-a": "2.0.0", "pkg-b": "1.1.0"},
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
        "budgetLedger": {},
        "createdAt": "2026-10-04T00:00:00Z",
        "updatedAt": "2026-10-04T00:00:00Z",
    }
    value.update(overrides)
    return value


def _checkpoint_dict(**overrides) -> dict:
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "checkpointId": "C0",
        "seq": 0,
        "parentCheckpointId": None,
        "status": "VERIFIED",
        "sourceSnapshotKey": "snap-0001",
        "sourceSnapshotContainer": "C:/project/snapshots/snap-0001",
        "projectRelative": ".",
        "fullAssignment": {"pkg-a": "1.0.0", "pkg-b": "1.0.0"},
        "acceptedDelta": {"added": {}, "changed": {}, "removed": {}},
        "cohortId": None,
        "lockfileHash": "lock-1",
        "manifestHash": "manifest-1",
        "observedResolvedHash": "resolved-1",
        "proofRefs": {},
        "audit": {"status": "PASS", "evidenceRef": "audit/c0"},
        "verification": {"kind": "passed", "status": "passed", "commands": ["node check.js"], "failingCommands": []},
        "createdAt": "2026-10-04T00:00:00Z",
    }
    value.update(overrides)
    return value


def _accepted_checkpoint(checkpoint_id: str, *, parent: str, seq: int, assignment: dict, changed: dict) -> dict:
    return _checkpoint_dict(
        checkpointId=checkpoint_id,
        parentCheckpointId=parent,
        seq=seq,
        fullAssignment=assignment,
        acceptedDelta={"added": {}, "changed": changed, "removed": {}},
    )


def _candidate_dict(run: dict, **overrides) -> dict:
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "candidateId": "cand-0001",
        "runId": run["runId"],
        "baseCheckpointId": "C0",
        "stage": "REPAIRING",
        "attemptId": 0,
        "cohortId": "cohort-0001",
        "fullAssignment": {"pkg-a": "1.0.0", "pkg-b": "1.0.0"},
        "delta": {"changed": {}},
        "materializationRefs": {},
        "createdAt": "2026-10-04T00:00:00Z",
        "updatedAt": "2026-10-04T00:00:00Z",
    }
    value.update(overrides)
    return value


def _feedback(run: dict, candidate: dict, *, kind: str, attempt_id: int = 0, **overrides) -> dict:
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run["runId"],
        "candidateId": candidate["candidateId"],
        "baseCheckpointId": candidate["baseCheckpointId"],
        "attemptId": attempt_id,
        "kind": kind,
        "reason": "",
        "changedFiles": [],
        "diagnosticsRefs": [],
        "proposedScope": {},
        "proposedConstraints": {},
    }
    value.update(overrides)
    return value


class _RunDir:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "checkpoints").mkdir(parents=True, exist_ok=True)
        (root / "trial").mkdir(parents=True, exist_ok=True)

    def write_run(self, run) -> None:
        save_run(self.root, run)

    def write_config(self, config) -> None:
        save_config(self.root, config)

    def write_checkpoint(self, checkpoint) -> None:
        save_checkpoint(self.root, checkpoint)

    def write_all_checkpoints(self, *checkpoints) -> None:
        for checkpoint in checkpoints:
            self.write_checkpoint(checkpoint)

    def write_candidate(self, candidate) -> None:
        save_candidate(self.root, candidate)


class BaselineNotAcceptedUpgradeTests(unittest.TestCase):
    def test_c0_only_is_not_an_accepted_upgrade(self) -> None:
        # B3 repro: a run that only ever verified the BASELINE must end as
        # NO_VERIFIED_UPGRADE with acceptedCheckpoints=0 and actual updates=0 —
        # never PARTIAL_VERIFIED with acceptedCheckpoints=1.
        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            root.write_run(_run_dict(phase="READY", terminal=None))
            # PASS audit, but the policy is NOT satisfied (no upgrade happened).
            root.write_config(_config_dict(targets={"pkg-a": "2.0.0", "pkg-b": "1.1.0"}))
            root.write_checkpoint(_checkpoint_dict(audit={"status": "PASS", "evidenceRef": "audit/c0"}))
            rc = _finish_locked(root.root, load_run(root.root), load_config(root.root))
            self.assertEqual(rc, 0)
            run = load_run(root.root)
            self.assertEqual(run["terminal"], "NO_VERIFIED_UPGRADE")
            outcome = run["terminalOutcome"]
            self.assertEqual(outcome["outcome"], "NO_VERIFIED_UPGRADE")
            self.assertEqual(outcome["acceptedCheckpoints"], 0)
            self.assertFalse(outcome["satisfied"])
            report = (root.root / "reports" / "MIGRATION_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("NO_VERIFIED_UPGRADE", report)
            self.assertIn("Accepted checkpoints: `0`", report)

    def test_real_accepted_checkpoint_still_counts_as_partial(self) -> None:
        # With one REAL accepted upgrade (C1, parent C0) the run is genuinely
        # partial: acceptedCheckpoints counts only non-baseline checkpoints.
        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            root.write_run(_run_dict(phase="READY", terminal=None, activeCheckpointId="C1"))
            root.write_config(_config_dict(targets={"pkg-a": "2.0.0", "pkg-b": "1.1.0"}))
            root.write_checkpoint(_checkpoint_dict())
            root.write_checkpoint(
                _accepted_checkpoint(
                    "C1", parent="C0", seq=1,
                    assignment={"pkg-a": "1.0.0", "pkg-b": "1.1.0"},
                    changed={"pkg-b": "1.1.0"},
                )
            )
            rc = _finish_locked(root.root, load_run(root.root), load_config(root.root))
            self.assertEqual(rc, 0)
            run = load_run(root.root)
            self.assertEqual(run["terminal"], "PARTIAL_VERIFIED")
            self.assertEqual(run["terminalOutcome"]["acceptedCheckpoints"], 1)

    def test_baseline_helpers(self) -> None:
        self.assertTrue(_is_baseline_checkpoint(_checkpoint_dict()))
        self.assertFalse(
            _is_baseline_checkpoint(
                _accepted_checkpoint("C1", parent="C0", seq=1, assignment={"pkg-a": "1.0.0"}, changed={})
            )
        )
        c0 = _checkpoint_dict()
        c1 = _accepted_checkpoint("C1", parent="C0", seq=1, assignment={"pkg-a": "1.0.0"}, changed={})
        c2 = _accepted_checkpoint(
            "C2", parent="C1", seq=2, assignment={"pkg-a": "2.0.0"}, changed={"pkg-a": "2.0.0"}
        )
        self.assertEqual(_accepted_upgrade_count([c0]), 0)
        self.assertEqual(_accepted_upgrade_count([c0, c1, c2]), 2)
        self.assertEqual(_accepted_upgrade_count([c1, c2]), 2)


class FeedbackValidationBeforeDurableWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="iter-feedback-")
        self.root = _RunDir(Path(tmp))
        self.root.write_run(_run_dict(phase="REPAIRING", terminal=None,
                                      activeCandidateId="cand-0001"))
        self.root.write_config(_config_dict())
        self.root.write_checkpoint(_checkpoint_dict())
        self.candidate = _candidate_dict(load_run(self.root.root))
        self.root.write_candidate(self.candidate)

    def _snapshot_candidate(self):
        return dict(load_candidate(self.root.root))

    def test_invalid_companions_raises_before_any_durable_write(self) -> None:
        # B5 repro: companions=[123] must raise FEEDBACK_COMPANIONS_INVALID
        # BEFORE the candidate is concluded/REJECTED on disk.
        run = load_run(self.root.root)
        feedback = _feedback(
            run, self.candidate,
            kind="NEEDS_COHORT_EXPANSION",
            proposedScope={"companions": [123]},
        )
        with self.assertRaises(InvalidInputError) as ctx:
            _apply_feedback_locked(self.root.root, run, load_config(self.root.root), feedback)
        self.assertIn("FEEDBACK_COMPANIONS_INVALID", str(ctx.exception))

        candidate = self._snapshot_candidate()
        self.assertEqual("REPAIRING", candidate["stage"],
                         "invalid feedback must not conclude the candidate")
        run = load_run(self.root.root)
        self.assertEqual("cand-0001", run["activeCandidateId"],
                         "the run pointer must be untouched")
        self.assertEqual("REPAIRING", run["phase"])
        ledger = load_ledger(self.root.root)
        self.assertEqual([], ledger.get("feedback", []))
        self.assertNotIn("scopeExpansions", ledger)

    def test_invalid_scope_type_raises_before_write(self) -> None:
        run = load_run(self.root.root)
        feedback = _feedback(
            run, self.candidate,
            kind="NEEDS_COHORT_EXPANSION",
            proposedScope=[1, 2],
        )
        with self.assertRaises(InvalidInputError) as ctx:
            _apply_feedback_locked(self.root.root, run, load_config(self.root.root), feedback)
        self.assertIn("FEEDBACK_SCOPE_INVALID", str(ctx.exception))
        self.assertEqual("REPAIRING", self._snapshot_candidate()["stage"])

    def test_invalid_diagnostics_refs_raises_before_write(self) -> None:
        run = load_run(self.root.root)
        feedback = _feedback(
            run, self.candidate, kind="INCONCLUSIVE", diagnosticsRefs=[123],
        )
        with self.assertRaises(InvalidInputError) as ctx:
            _apply_feedback_locked(self.root.root, run, load_config(self.root.root), feedback)
        self.assertIn("FEEDBACK_DIAGNOSTICS_INVALID", str(ctx.exception))
        self.assertEqual("REPAIRING", self._snapshot_candidate()["stage"])

    def test_valid_needs_cohort_expansion_records_companions(self) -> None:
        run = load_run(self.root.root)
        feedback = _feedback(
            run, self.candidate,
            kind="NEEDS_COHORT_EXPANSION",
            proposedScope={"companions": ["pkg-c"]},
            reason="peer of pkg-a",
        )
        rc = _apply_feedback_locked(self.root.root, run, load_config(self.root.root), feedback)
        self.assertEqual(rc, 0)
        candidate = self._snapshot_candidate()
        self.assertEqual("REJECTED", candidate["stage"])
        run = load_run(self.root.root)
        self.assertIsNone(run["activeCandidateId"])
        self.assertEqual("READY", run["phase"])
        ledger = load_ledger(self.root.root)
        self.assertEqual(["pkg-c"], ledger["scopeExpansions"][0]["companions"])
        self.assertEqual("peer of pkg-a", ledger["scopeExpansions"][0]["reason"])
        self.assertEqual(1, len(ledger["feedback"]))
        self.assertEqual("NEEDS_COHORT_EXPANSION", ledger["feedback"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
