"""Iterative migration: versioned run/checkpoint/candidate/feedback identities,
stale-rejection, plan-next cohort construction and ledger behaviour.

Common fixture: tests/fixtures/iterative_migration_contract.json — the SAME file
is validated from Node by desktop/scripts/check-iterative-migration.mjs, so a
schema drift between the Python owner and the Desktop coordinator is caught on
both sides.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from iterative_migration import (
    SCHEMA_VERSION,
    StaleFeedbackError,
    InvalidInputError,
    _accepted_delta,
    _apply_feedback_locked,
    _plan_next_locked,
    _record_block,
    assignment_fingerprint,
    load_candidate,
    load_checkpoint,
    load_config,
    load_run,
    save_candidate,
    save_checkpoint,
    save_config,
    save_run,
    trial_workspace_root,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "iterative_migration_contract.json"
)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _config_dict(**overrides) -> dict:
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "projectDir": "C:/projects/sample-app",
        "projectName": "sample-app",
        "workspaceId": "ws-1",
        "projectId": "sample-app",
        "targetLevel": "yellow",
        "targets": {"pkg-a": "1.1.0"},
        "verifyConfig": {"commands": ["npm run test"]},
        "policyHash": "policyhash-0001",
        "budget": {
            "runBudgetMinutes": 120,
            "maxRepairAttemptsPerRevision": 2,
            "maxInfraRetries": 2,
            "phaseTimeoutSeconds": 1200,
        },
        "createdAt": "2026-09-29T00:00:00Z",
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
        "heartbeatAt": "2026-09-29T00:00:00Z",
        "createdAt": "2026-09-29T00:00:00Z",
        "updatedAt": "2026-09-29T00:00:00Z",
        "toolBuildId": "tool-0001",
    }
    value.update(overrides)
    return value


def _checkpoint_dict(status="VERIFIED", full_assignment=None, checkpoint_id="C0", seq=0) -> dict:
    fixture = _fixture()["checkpoint"]
    value = dict(fixture)
    value.update(
        {
            "checkpointId": checkpoint_id,
            "seq": seq,
            "parentCheckpointId": None if checkpoint_id == "C0" else "C0",
            "status": status,
            "fullAssignment": full_assignment or fixture["fullAssignment"],
        }
    )
    return value


def _candidate_dict(**overrides) -> dict:
    value = dict(_fixture()["candidate"])
    value.update(overrides)
    return value


def _feedback_dict(**overrides) -> dict:
    value = dict(_fixture()["feedback"])
    value.update(overrides)
    return value


class _RunDir:
    """Minimal fabricated run directory (no real verification)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "trial").mkdir(parents=True, exist_ok=True)

    def write_run(self, run=None) -> None:
        save_run(self.root, run or _run_dict())

    def write_config(self, config=None) -> None:
        save_config(self.root, config or _config_dict())

    def write_checkpoint(self, checkpoint=None) -> None:
        save_checkpoint(self.root, checkpoint or _checkpoint_dict())

    def write_candidate(self, candidate=None) -> None:
        save_candidate(self.root, candidate or _candidate_dict())


class IterativeMigrationSchemaTests(unittest.TestCase):
    def test_fixture_contract_schema(self) -> None:
        fixture = _fixture()
        self.assertEqual(SCHEMA_VERSION, fixture["schemaVersion"])
        for kind in ("run", "checkpoint", "candidate", "feedback"):
            self.assertEqual(
                SCHEMA_VERSION, fixture[kind]["schemaVersion"], kind
            )

    def test_fixture_kind_enums_match_module(self) -> None:
        from iterative_migration import FEEDBACK_KINDS, PHASES, TERMINALS

        fixture = _fixture()
        self.assertEqual(set(fixture["feedbackKinds"]), FEEDBACK_KINDS)
        self.assertEqual(set(fixture["phases"]), PHASES)
        self.assertEqual(set(fixture["terminals"]), TERMINALS)

    def test_fixture_feedback_stale_cases_present(self) -> None:
        fixture = _fixture()
        self.assertEqual(
            {case["name"] for case in fixture["staleFeedbackCases"]},
            {"wrong-run", "wrong-candidate", "wrong-base", "wrong-attempt"},
        )


class IterativeMigrationRoundTripTests(unittest.TestCase):
    def test_run_checkpoint_candidate_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_checkpoint()
            run_dir.write_candidate()
            run = load_run(run_dir.root)
            self.assertEqual(run["runId"], "iter-000000000001")
            self.assertEqual(run["activeCheckpointId"], "C0")
            checkpoint = load_checkpoint(run_dir.root, "C0")
            self.assertEqual(checkpoint["status"], "VERIFIED")
            candidate = load_candidate(run_dir.root)
            self.assertEqual(candidate["stage"], "PLANNED")
            self.assertEqual(candidate["baseCheckpointId"], "C0")

    def test_schema_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            bad = _run_dict()
            bad["schemaVersion"] = 999
            save_run(run_dir.root, bad)
            with self.assertRaises(InvalidInputError):
                load_run(run_dir.root)

    def test_accepted_delta(self) -> None:
        old = {"pkg-a": "1.0.0", "pkg-b": "1.0.0", "pkg-c": "1.0.0"}
        new = {"pkg-a": "1.1.0", "pkg-b": "1.0.0"}
        delta = _accepted_delta(old, new)
        self.assertEqual(delta["changed"], {"pkg-a": "1.1.0"})
        self.assertEqual(delta["added"], {})
        self.assertEqual(delta["removed"], {"pkg-c": "1.0.0"})


class IterativeMigrationPlanNextTests(unittest.TestCase):
    def test_plan_next_no_targets_terminal_no_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config(_config_dict(targets={}))
            run_dir.write_checkpoint()
            _plan_next_locked(run_dir.root, _run_dict(), _config_dict(targets={}))
            run = load_run(run_dir.root)
            self.assertEqual(run["terminal"], "NO_ACTIONABLE")
            self.assertIsNone(load_candidate(run_dir.root))

    def test_plan_next_single_atomic_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config()
            run_dir.write_checkpoint()
            result = _plan_next_locked(run_dir.root, _run_dict(), _config_dict())
            self.assertEqual(result, 0)
            candidate = load_candidate(run_dir.root)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["delta"]["changed"], {"pkg-a": "1.1.0"})
            self.assertEqual(candidate["fullAssignment"]["pkg-a"], "1.1.0")
            self.assertEqual(candidate["fullAssignment"]["pkg-b"], "1.0.0")
            self.assertEqual(candidate["baseCheckpointId"], "C0")
            run = load_run(run_dir.root)
            self.assertEqual(run["activeCandidateId"], candidate["candidateId"])
            self.assertEqual(run["phase"], "PLANNING")

    def test_plan_next_eventual_policy_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config(_config_dict(targets={"pkg-a": "1.1.0"}))
            run_dir.write_checkpoint(
                _checkpoint_dict(full_assignment={"pkg-a": "1.1.0", "pkg-b": "1.0.0"})
            )
            _plan_next_locked(run_dir.root, _run_dict(), _config_dict(targets={"pkg-a": "1.1.0"}))
            run = load_run(run_dir.root)
            self.assertIsNone(run.get("terminal"))
            self.assertEqual(run["phase"], "READY")
            self.assertIsNone(load_candidate(run_dir.root))

    def test_plan_next_skips_blocked_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config(_config_dict(targets={"pkg-a": "1.1.0"}))
            run_dir.write_checkpoint()
            ledger = {
                "schemaVersion": SCHEMA_VERSION,
                "blocks": [],
                "deferrals": [],
                "feedback": [],
                "counters": {},
            }
            candidate = _candidate_dict()
            fingerprint = assignment_fingerprint(
                {"pkg-a": "1.1.0", "pkg-b": "1.0.0"}
            )
            _record_block(
                ledger,
                run_dir.root,
                candidateId=candidate["candidateId"],
                baseCheckpointId="C0",
                kind="NEGATIVE_VERIFY",
                reason="deterministic",
                assignmentFingerprint=fingerprint,
            )
            _plan_next_locked(
                run_dir.root,
                _run_dict(),
                _config_dict(targets={"pkg-a": "1.1.0"}),
            )
            run = load_run(run_dir.root)
            self.assertEqual(run["terminal"], "SCOPE_EXHAUSTED")
            self.assertIsNone(load_candidate(run_dir.root))


class IterativeMigrationFeedbackTests(unittest.TestCase):
    def test_stale_feedback_rejected_for_all_identity_fields(self) -> None:
        fixture = _fixture()
        for case in fixture["staleFeedbackCases"]:
            with self.subTest(case=case["name"]):
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = _RunDir(Path(tmp))
                    run_dir.write_run()
                    run_dir.write_config()
                    run_dir.write_candidate()
                    feedback = _feedback_dict()
                    feedback[case["field"]] = case["value"]
                    with self.assertRaises(StaleFeedbackError):
                        _apply_feedback_locked(
                            run_dir.root,
                            _run_dict(),
                            _config_dict(),
                            feedback,
                        )

    def test_valid_ready_for_verify_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config()
            run_dir.write_candidate()
            _apply_feedback_locked(
                run_dir.root,
                _run_dict(),
                _config_dict(),
                _feedback_dict(),
            )
            run = load_run(run_dir.root)
            self.assertEqual(run["phase"], "VERIFYING")

    def test_unknown_feedback_kind_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config()
            run_dir.write_candidate()
            feedback = _feedback_dict(kind="I_PROMISE_IT_WORKS")
            with self.assertRaises(InvalidInputError):
                _apply_feedback_locked(
                    run_dir.root, _run_dict(), _config_dict(), feedback
                )

    def test_forbidden_manifest_path_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config()
            unit = _candidate_dict()
            unit["materializationRefs"] = {
                "workspaceRoot": str(trial_workspace_root(run_dir.root)),
            }
            run_dir.write_candidate(unit)
            feedback = _feedback_dict(changedFiles=["package.json"])
            with self.assertRaises(InvalidInputError):
                _apply_feedback_locked(
                    run_dir.root, _run_dict(), _config_dict(), feedback
                )

    def test_path_outside_trial_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config()
            unit = _candidate_dict()
            unit["materializationRefs"] = {
                "workspaceRoot": str(trial_workspace_root(run_dir.root)),
            }
            run_dir.write_candidate(unit)
            feedback = _feedback_dict(changedFiles=["../../etc/passwd"])
            with self.assertRaises(InvalidInputError):
                _apply_feedback_locked(
                    run_dir.root, _run_dict(), _config_dict(), feedback
                )

    def test_changed_files_require_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config()
            run_dir.write_candidate()
            feedback = _feedback_dict(changedFiles="src/config.js")
            with self.assertRaises(InvalidInputError):
                _apply_feedback_locked(
                    run_dir.root, _run_dict(), _config_dict(), feedback
                )

    def test_inconclusive_records_bounded_deferral(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config()
            run_dir.write_candidate()
            _apply_feedback_locked(
                run_dir.root,
                _run_dict(),
                _config_dict(),
                _feedback_dict(kind="INCONCLUSIVE", reason="flaky evidence"),
            )
            from iterative_migration import load_ledger

            ledger = load_ledger(run_dir.root)
            self.assertEqual(len(ledger["deferrals"]), 1)
            self.assertEqual(ledger["deferrals"][0]["cohortId"], "cohort-0001")
            # Deferral is bounded: no permanent block is recorded.
            self.assertEqual(ledger["blocks"], [])


class IterativeMigrationGuardTests(unittest.TestCase):
    def test_verify_exact_without_candidate_rejected(self) -> None:
        from iterative_migration import cmd_verify_exact

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config()
            run_dir.write_checkpoint()
            import argparse

            args = argparse.Namespace(run_dir=str(run_dir.root), owner="test")
            with self.assertRaises(InvalidInputError):
                cmd_verify_exact(args)

    def test_verify_exact_resumes_verifying_stage_candidate(self) -> None:
        # R4: a kill/restart in the middle of verify-exact leaves stage=VERIFYING.
        # The guard must ACCEPT that stage so the SAME durable candidate can be
        # re-verified (idempotent acceptance), instead of refusing the resume.
        from iterative_migration import cmd_verify_exact

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run(_run_dict(phase="VERIFYING"))
            run_dir.write_config()
            run_dir.write_checkpoint()
            candidate = _candidate_dict()
            candidate["stage"] = "VERIFYING"
            candidate["materializationRefs"] = {
                "workspaceRoot": str(trial_workspace_root(run_dir.root)),
                "projectRelative": ".",
            }
            run_dir.write_candidate(candidate)
            import argparse

            args = argparse.Namespace(run_dir=str(run_dir.root), owner="test")
            with self.assertRaises(InvalidInputError) as ctx:
                cmd_verify_exact(args)
            # The VERIFYING stage passed the guard; the next concrete error is
            # the absent trial materialization, NOT a stage refusal.
            self.assertNotIn(
                "CANDIDATE_STAGE_NOT_READY_FOR_VERIFY",
                str(ctx.exception),
            )
            self.assertIn("TRIAL_PROJECT_MISSING", str(ctx.exception))

    def test_verify_bootstrap_requires_trial_and_bootstrap_phase(self) -> None:
        # R7: the control re-verify runs on the isolated version-neutral trial
        # (created by bootstrap-materialize), NEVER on the developer checkout, so
        # without durable bootstrapRefs it must refuse instead of re-checking
        # config.projectDir.
        from iterative_migration import cmd_verify_bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run(_run_dict(phase="READY"))
            run_dir.write_config()
            run_dir.write_checkpoint()
            import argparse

            args = argparse.Namespace(run_dir=str(run_dir.root), owner="test")
            with self.assertRaises(InvalidInputError) as ctx:
                cmd_verify_bootstrap(args)
            self.assertIn("PHASE_NOT_BOOTSTRAP", str(ctx.exception))

            run_dir.write_run(_run_dict(phase="BOOTSTRAP_REPAIR"))
            with self.assertRaises(InvalidInputError) as ctx:
                cmd_verify_bootstrap(args)
            self.assertIn("BOOTSTRAP_TRIAL_MISSING", str(ctx.exception))

    def test_bootstrap_materialize_guards_phase_and_request(self) -> None:
        from iterative_migration import (
            InvalidInputError,
            cmd_bootstrap_materialize,
            write_repair_requests,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run(_run_dict(phase="READY"))
            run_dir.write_config()
            run_dir.write_checkpoint()
            import argparse

            args = argparse.Namespace(run_dir=str(run_dir.root), owner="test", timeout_seconds=1800)
            with self.assertRaises(InvalidInputError) as ctx:
                cmd_bootstrap_materialize(args)
            self.assertIn("PHASE_NOT_BOOTSTRAP", str(ctx.exception))

            run_dir.write_run(_run_dict(phase="BOOTSTRAP_REPAIR"))
            with self.assertRaises(InvalidInputError) as ctx:
                cmd_bootstrap_materialize(args)
            self.assertIn("BOOTSTRAP_REQUEST_MISSING", str(ctx.exception))

            write_repair_requests(run_dir.root, [{"bootstrap": True, "reason": "C0 red"}])
            # With a live bootstrap request the materializer proceeds to the
            # sealed snapshot: the fixture has no real source container, so it
            # must fail there (controlled), i.e. past the guards.
            with self.assertRaises(Exception) as ctx:
                cmd_bootstrap_materialize(args)
            self.assertNotIn("BOOTSTRAP_REQUEST_MISSING", str(ctx.exception))
            self.assertNotIn("PHASE_NOT_BOOTSTRAP", str(ctx.exception))

    def test_plan_next_with_red_checkpoint_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _RunDir(Path(tmp))
            run_dir.write_run()
            run_dir.write_config(_config_dict(targets={"pkg-a": "1.1.0"}))
            run_dir.write_checkpoint(_checkpoint_dict(status="RED_SOURCE"))
            with self.assertRaises(InvalidInputError):
                _plan_next_locked(run_dir.root, _run_dict(), _config_dict())


if __name__ == "__main__":
    unittest.main()
