"""Iterative migration: versioned run/checkpoint/candidate/feedback identities,
stale-rejection, plan-next cohort construction and ledger behaviour.

Common fixture: tests/fixtures/iterative_migration_contract.json — the SAME file
is validated from Node by desktop/scripts/check-iterative-migration.mjs, so a
schema drift between the Python owner and the Desktop coordinator is caught on
both sides.
"""
from __future__ import annotations

import argparse
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
    cmd_status,
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


def _checkpoint_dict(status="VERIFIED", full_assignment=None, checkpoint_id="C0", seq=0, **overrides) -> dict:
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
    value.update(overrides)
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


class IterativeMigrationFinishAuditTests(unittest.TestCase):
    """R8: the independent audit of the exact accepted checkpoint precedes the
    terminal finish; finish is an idempotent terminal transition and UNKNOWN
    audit evidence never yields COMPLETE."""

    def test_terminal_outcome_table(self) -> None:
        from iterative_migration import _terminal_outcome

        self.assertEqual(_terminal_outcome(True, "PASS", 2), "COMPLETE")
        # Satisfied policy with a FAIL audit still made progress: never COMPLETE.
        self.assertEqual(_terminal_outcome(True, "FAIL", 2), "PARTIAL_VERIFIED")
        # UNKNOWN evidence blocks the completion claim.
        self.assertEqual(_terminal_outcome(True, "UNKNOWN", 2), "BLOCKED_BASELINE")
        self.assertEqual(_terminal_outcome(True, "", 2), "BLOCKED_BASELINE")
        self.assertEqual(_terminal_outcome(False, "PASS", 2), "PARTIAL_VERIFIED")
        self.assertEqual(_terminal_outcome(False, "FAIL", 0), "NO_VERIFIED_UPGRADE")
        self.assertEqual(_terminal_outcome(False, "PASS", 0), "NO_VERIFIED_UPGRADE")

    def test_policy_satisfied_exact(self) -> None:
        from iterative_migration import _policy_satisfied

        config = _config_dict(targets={"pkg-a": "1.1.0", "pkg-b": "2.0.0"})
        self.assertTrue(
            _policy_satisfied(config, _checkpoint_dict(full_assignment={"pkg-a": "1.1.0", "pkg-b": "2.0.0"}))
        )
        self.assertFalse(
            _policy_satisfied(config, _checkpoint_dict(full_assignment={"pkg-a": "1.1.0"}))
        )
        self.assertFalse(_policy_satisfied(_config_dict(targets={}), _checkpoint_dict()))

    def test_finish_with_recorded_audit_computes_durable_outcome(self) -> None:
        from iterative_migration import _finish_locked, load_config, load_run

        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            audit = {"status": "PASS", "evidenceRef": str(root.root / "audit" / "C0")}
            root.write_run(_run_dict(phase="READY", terminal=None))
            root.write_config(_config_dict(targets={"pkg-a": "1.1.0"}))
            root.write_checkpoint(
                _checkpoint_dict(full_assignment={"pkg-a": "1.1.0"}, audit=audit)
            )
            rc = _finish_locked(root.root, load_run(root.root), load_config(root.root))
            self.assertEqual(rc, 0)
            run = load_run(root.root)
            self.assertEqual(run["phase"], "TERMINAL")
            self.assertEqual(run["terminal"], "COMPLETE")
            self.assertEqual(run["terminalOutcome"]["outcome"], "COMPLETE")
            self.assertEqual(run["terminalOutcome"]["auditStatus"], "PASS")
            report = (root.root / "reports" / "MIGRATION_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("COMPLETE", report)
            self.assertIn("lagOkPct", report)

    def test_first_finish_report_contains_audit_just_persisted(self) -> None:
        from unittest.mock import patch
        from iterative_migration import _finish_locked, load_config, load_run, load_checkpoint, save_checkpoint
        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            root.write_run(_run_dict(phase="READY", terminal=None))
            root.write_config(_config_dict(targets={"pkg-a": "1.1.0"}))
            root.write_checkpoint(_checkpoint_dict(full_assignment={"pkg-a": "1.1.0"}, audit={"status": "UNKNOWN"}))
            def audit_now(run_dir, run, config, args):
                checkpoint = load_checkpoint(run_dir, "C0")
                checkpoint["audit"] = {"status": "PASS", "lagOkPct": 100, "lagOk": 1,
                                       "lagTotal": 1, "lagLagging": 0, "lagUnknown": 0,
                                       "vulnerabilityPackages": 0, "evidenceRef": "fresh-audit-evidence"}
                save_checkpoint(run_dir, checkpoint)
                return 0
            with patch("iterative_migration._audit_locked", side_effect=audit_now):
                _finish_locked(root.root, load_run(root.root), load_config(root.root))
            report = (root.root / "reports/MIGRATION_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("статус `PASS`", report)
            self.assertIn("lag-ok `100%`", report)
            self.assertIn("fresh-audit-evidence", report)
            self.assertNotIn("статус `UNKNOWN`", report)

    def test_finish_idempotent_preserves_terminal_outcome(self) -> None:
        from iterative_migration import _finish_locked, load_config, load_run

        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            outcome = {
                "outcome": "PARTIAL_VERIFIED",
                "satisfied": False,
                "auditStatus": "PASS",
                "acceptedCheckpoints": 1,
                "finishedAt": "2026-09-28T00:00:00Z",
                "activeCheckpointId": "C0",
            }
            root.write_run(_run_dict(phase="TERMINAL", terminal="PARTIAL_VERIFIED", terminalOutcome=outcome))
            root.write_config(_config_dict())
            root.write_checkpoint(_checkpoint_dict())
            rc = _finish_locked(root.root, load_run(root.root), load_config(root.root))
            self.assertEqual(rc, 0)
            run = load_run(root.root)
            # Idempotent: the durable outcome and its timestamp are untouched.
            self.assertEqual(run["terminal"], "PARTIAL_VERIFIED")
            self.assertEqual(run["terminalOutcome"]["finishedAt"], "2026-09-28T00:00:00Z")
            report = (root.root / "reports" / "MIGRATION_REPORT.md").read_text(encoding="utf-8")
            self.assertIn("PARTIAL_VERIFIED", report)


class IterativeMigrationReportTests(unittest.TestCase):
    """R10: the exported docs aggregate the ACTUAL accepted chain — the active
    checkpoint is resolved by identity/parent chain, never by string-max id
    (C10 > C9 would silently describe the wrong state), and итог/остаток +
    coverage/vuln + agent explanations are real content."""

    def test_active_checkpoint_by_identity_not_string_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            # C10 sorts BEFORE C9 as a string; the run's active state is C9.
            root.write_run(_run_dict(activeCheckpointId="C9"))
            root.write_config(_config_dict(targets={"pkg-a": "1.1.0"}))
            root.write_checkpoint(
                _checkpoint_dict(
                    checkpoint_id="C9",
                    full_assignment={"pkg-a": "9.9.9"},
                    verification={"status": "passed", "kind": "passed", "commands": [], "failingCommands": []},
                )
            )
            root.write_checkpoint(
                _checkpoint_dict(
                    checkpoint_id="C10",
                    full_assignment={"pkg-a": "10.0.0"},
                    verification={"status": "passed", "kind": "passed", "commands": [], "failingCommands": []},
                )
            )
            from iterative_migration import _build_migration_report, _build_developer_upgrade_guide

            report = _build_migration_report(
                root.root, load_run(root.root), load_config(root.root),
                [_checkpoint_dict(checkpoint_id="C10", full_assignment={"pkg-a": "10.0.0"}),
                 _checkpoint_dict(checkpoint_id="C9", full_assignment={"pkg-a": "9.9.9"})],
                None,
            )
            self.assertIn("9.9.9", report)
            self.assertNotIn("10.0.0", report)
            guide = _build_developer_upgrade_guide(
                root.root, load_config(root.root),
                [_checkpoint_dict(checkpoint_id="C10", full_assignment={"pkg-a": "10.0.0"}),
                 _checkpoint_dict(checkpoint_id="C9", full_assignment={"pkg-a": "9.9.9"})],
            )
            # Neither checkpoint accepted a change, so the guide reports NO
            # accepted upgrades with REAL content, not a placeholder promise.
            self.assertIn("Проверенных обновлений зависимостей", guide)
            self.assertNotIn("описываются по мере", guide)

    def test_report_orders_by_parent_chain_and_aggregates_coverage(self) -> None:
        from iterative_migration import _build_migration_report

        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            root.write_run(_run_dict(activeCheckpointId="C2"))
            root.write_config(_config_dict(targets={"pkg-a": "1.1.0", "pkg-b": "2.0.0"}))
            audit = {
                "status": "PASS", "evidenceRef": str(root.root / "audit" / "C2"),
                "lagOkPct": 90, "lagOk": 9, "lagLagging": 1, "lagUnknown": 0,
                "lagTotal": 10, "vulnerabilityPackages": 0,
            }
            nodes = [
                ("C0", None, {"pkg-a": "0.5.0", "pkg-b": "1.0.0"}),
                ("C1", "C0", {"pkg-a": "1.1.0", "pkg-b": "1.0.0"}),
                ("C2", "C1", {"pkg-a": "1.1.0", "pkg-b": "2.0.0"}),
            ]
            for cid, parent, assignment in nodes:
                root.write_checkpoint(
                    _checkpoint_dict(
                        checkpoint_id=cid,
                        parentCheckpointId=parent,
                        seq=int(cid[1:]),
                        full_assignment=assignment,
                        audit=audit if cid == "C2" else {},
                        acceptedDelta=(
                            {"changed": {"pkg-a": "1.1.0"}, "added": {}, "removed": {}}
                            if cid == "C1"
                            else ({"changed": {"pkg-b": "2.0.0"}, "added": {}, "removed": {}}
                                  if cid == "C2" else {"changed": {}, "added": {}, "removed": {}})
                        ),
                    )
                )
            (root.root / "ledger.json").write_text(
                json.dumps({
                    "blocks": [], "deferrals": [],
                    "feedback": [{
                        "schemaVersion": 1, "runId": "iter-000000000001",
                        "candidateId": "cand-1", "baseCheckpointId": "C1",
                        "attemptId": 0, "kind": "ADAPTATION",
                        "reason": "pkg-b needs runtime adapter for v2 API", "changedFiles": ["src/adapter.ts"],
                        "diagnosticsRefs": [], "proposedScope": {}, "proposedConstraints": {},
                        "createdAt": "2026-09-29T00:01:00Z",
                    }],
                    "counters": {},
                }),
                encoding="utf-8",
            )
            checkpoints = [root.root / "checkpoints" / f"{cid}.json" for cid, _, _ in nodes]
            checkpoints = [
                json.loads(path.read_text(encoding="utf-8")) for path in checkpoints
            ]
            report = _build_migration_report(
                root.root, load_run(root.root), load_config(root.root), checkpoints, None,
            )
            # Parent-chain order: C0 before C1 before C2 (not filename order).
            self.assertLess(report.index("| C0 "), report.index("| C1 "))
            self.assertLess(report.index("| C1 "), report.index("| C2 "))
            self.assertIn("Итог миграции", report)
            self.assertIn("остаток: `0`", report)
            self.assertIn("lagOkPct", report)
            self.assertIn("vulnerabilityPackages", report)
            self.assertIn("runtime adapter", report)
            chain = [root.root / "checkpoints" / f"{cid}.json" for cid, _, _ in nodes]
            from iterative_migration import _ordered_checkpoint_chain

            chain, tail = _ordered_checkpoint_chain(
                [json.loads(p.read_text(encoding="utf-8")) for p in chain]
            )
            self.assertEqual([c["checkpointId"] for c in chain], ["C0", "C1", "C2"])
            self.assertEqual(tail, [])

    def test_guide_aggregates_actual_adaptations_and_deferrals(self) -> None:
        from iterative_migration import _build_developer_upgrade_guide

        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            root.write_config(_config_dict(targets={"pkg-a": "1.1.0"}))
            root.write_checkpoint(
                _checkpoint_dict(
                    checkpoint_id="C1",
                    parentCheckpointId="C0",
                    full_assignment={"pkg-a": "1.1.0"},
                    acceptedDelta={"changed": {"pkg-a": "1.1.0"}, "added": {}, "removed": {}},
                )
            )
            root.write_checkpoint(
                _checkpoint_dict(
                    checkpoint_id="C0",
                    parentCheckpointId=None,
                    full_assignment={"pkg-a": "0.5.0"},
                )
            )
            (root.root / "ledger.json").write_text(
                json.dumps({
                    "blocks": [], "deferrals": [
                        {"package": "pkg-b", "reason": "PEER_RESOLUTION_DEFERRED: not actionable"}
                    ],
                    "feedback": [{
                        "schemaVersion": 1, "runId": "iter-000000000001",
                        "candidateId": "cand-1", "baseCheckpointId": "C1",
                        "attemptId": 0, "kind": "ADAPTATION",
                        "reason": "pkg-a dropped legacy API; adapter added",
                        "changedFiles": ["src/adapter.ts"],
                        "diagnosticsRefs": [], "proposedScope": {}, "proposedConstraints": {},
                        "createdAt": "2026-09-29T00:01:00Z",
                    }],
                    "counters": {},
                }),
                encoding="utf-8",
            )
            checkpoints = [
                json.loads((root.root / "checkpoints" / f"{cid}.json").read_text(encoding="utf-8"))
                for cid in ("C0", "C1")
            ]
            guide = _build_developer_upgrade_guide(root.root, load_config(root.root), checkpoints)
            self.assertIn("| `pkg-a` | `0.5.0` | `1.1.0` |", guide)
            self.assertIn("dropped legacy API", guide)
            self.assertIn("PEER_RESOLUTION_DEFERRED", guide)


class IterativeRuntimeStatusViewTests(unittest.TestCase):
    """D2.4: `cmd_status` exposes the durable runtime contract (requested vs
    effective + manager/platform) as a display-only `config` view for the UI,
    and omits it when the run makes no runtime commitment."""

    def _status_payload(self, root: _RunDir) -> dict:
        with tempfile.TemporaryDirectory() as _stdout_dir:
            args = argparse.Namespace(run_dir=str(root.root))
            code = cmd_status(args)
            self.assertEqual(0, code)
        import json as _json

        payload = _json.loads((root.root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual("C0", payload["run"]["activeCheckpointId"])
        return payload

    def test_status_payload_carries_requested_and_runtime_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            root.write_run()
            runtime = {
                "requested": "22",
                "effectiveVersion": "22.18.0",
                "nodePath": "C:/node22/node.exe",
                "npmPath": "C:/node22/npm.cmd",
                "source": "major-alias",
                "packageManager": "npm",
                "packageManagerVersion": "10.9.2",
                "platform": "win32",
                "arch": "AMD64",
                "contractHash": "rt-hash-1",
                "hash": "rt-hash-1",
            }
            root.write_config(_config_dict(requestedNode="22", runtime=runtime))
            root.write_checkpoint()
            payload = self._status_payload(root)
            self.assertEqual("22", payload["config"]["requestedNode"])
            view = payload["config"]["runtime"]
            self.assertEqual("22", view["requested"])
            self.assertEqual("22.18.0", view["effectiveVersion"])
            self.assertEqual("npm", view["packageManager"])
            self.assertEqual("10.9.2", view["packageManagerVersion"])
            self.assertEqual("win32", view["platform"])
            # the durable contract keeps raw paths; the display view does not
            # need them, but they must stay intact on the durable side.
            stored = load_config(root.root)
            self.assertEqual("C:/node22/node.exe", stored["runtime"]["nodePath"])

    def test_status_payload_omits_runtime_when_uncommitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _RunDir(Path(tmp))
            root.write_run()
            root.write_config(_config_dict())
            root.write_checkpoint()
            payload = self._status_payload(root)
            self.assertEqual(payload["config"]["requestedNode"], "")
            self.assertEqual(payload["config"]["runtime"], {})


if __name__ == "__main__":
    unittest.main()
