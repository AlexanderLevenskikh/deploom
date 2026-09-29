"""#6 runtime-aware candidate selection in the iterative planner.

Before the expensive materialize, ``_plan_next_locked`` probes the registry
``engines.node`` of each direct target version and defers a target that
excludes the run's chosen Node (runtime contract effectiveVersion) — an
incompatible install is never materialized and never becomes a false
verification outcome. Abstention (no metadata, probe failure, no runtime)
is compatibility, never a constraint.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iterative_migration import (
    SCHEMA_VERSION,
    _normalize_effective_node_version,
    _plan_next_locked,
    load_candidate,
    load_ledger,
    load_run,
    save_checkpoint,
    save_config,
    save_run,
)

FAKE_RUNTIME = {
    "runtime": {
        "requested": "20",
        "effectiveVersion": "20.11.0",
        "nodePath": "C:/node20/node.exe",
        "npmPath": "C:/node20/npm.cmd",
        "source": "major-alias",
        "packageManager": "npm",
        "packageManagerVersion": "10.0.0",
        "platform": "win32",
        "arch": "AMD64",
        "contractHash": "rt-20",
    }
}


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


def _checkpoint_dict(**overrides) -> dict:
    value = {
        "schemaVersion": SCHEMA_VERSION,
        "checkpointId": "C0",
        "seq": 0,
        "parentCheckpointId": None,
        "status": "VERIFIED",
        "fullAssignment": {"pkg-a": "1.0.0", "pkg-b": "1.0.0"},
        "lockfileHash": "lock-1",
        "manifestHash": "manifest-1",
        "observedResolvedHash": "resolved-1",
        "proofRefs": {},
        "audit": {"status": "UNKNOWN", "evidenceRef": ""},
        "verification": {"kind": "passed", "status": "passed", "commands": ["npm run test"], "failingCommands": []},
        "createdAt": "2026-09-29T00:00:00Z",
        "updatedAt": "2026-09-29T00:00:00Z",
    }
    value.update(overrides)
    return value


class IterativeEnginePrecheckTests(unittest.TestCase):
    """Engine gate must run BEFORE materialization and defer incompatible
    direct targets deterministically, never inventing versions."""

    # D3.4's RUNTIME_CHANGED guard re-resolves the requested Node against the
    # HOST installation; it is exercised in test_iterative_runtime_request.py
    # with real fake runtimes. Here it is neutralized so the engine-gate unit
    # owns the effective version under test.
    def _runtime_guard(self) -> mock.patch:
        return mock.patch("iterative_migration._assert_runtime_unchanged")

    def _run_dir(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="iter-engine-"))
        (root / "trial").mkdir(parents=True, exist_ok=True)
        save_run(root, _run_dict())
        save_config(root, _config_dict())
        save_checkpoint(root, _checkpoint_dict())
        return root

    def test_incompatible_target_is_deferred_not_materialized(self) -> None:
        run_dir = self._run_dir()
        save_config(
            run_dir,
            _config_dict(**FAKE_RUNTIME, targets={"pkg-a": "1.1.0"}),
        )

        def fake_probe(project_dir, name, version, runtime_env):
            if name == "pkg-a" and version == "1.1.0":
                return "^22.18.0 || >=24.11.0"
            return ""

        with mock.patch("iterative_migration._npm_engines_node", side_effect=fake_probe), self._runtime_guard():
            _plan_next_locked(run_dir, _run_dict(), _config_dict(**FAKE_RUNTIME, targets={"pkg-a": "1.1.0"}))

        candidate = load_candidate(run_dir)
        self.assertIsNone(candidate, "An engines-incompatible target must never produce a candidate")
        ledger = load_ledger(run_dir)
        engine_deferrals = [e for e in ledger.get("deferrals", []) if e.get("kind") == "ENGINES_INCOMPATIBLE"]
        self.assertEqual(len(engine_deferrals), 1)
        self.assertEqual(engine_deferrals[0]["package"], "pkg-a")
        self.assertEqual(engine_deferrals[0]["requestedVersion"], "1.1.0")
        self.assertIn("20.11.0", engine_deferrals[0]["reason"])
        run = load_run(run_dir)
        self.assertEqual(run["terminal"], "NO_ACTIONABLE", "Deferred target keeps policy unmet; run must not be satisfied")

    def test_compatible_target_is_assigned_without_deferral(self) -> None:
        run_dir = self._run_dir()
        save_config(
            run_dir,
            _config_dict(**FAKE_RUNTIME, targets={"pkg-a": "1.1.0"}),
        )

        def fake_probe(project_dir, name, version, runtime_env):
            if name == "pkg-a" and version == "1.1.0":
                return ">=18"
            return ""

        with mock.patch("iterative_migration._npm_engines_node", side_effect=fake_probe), self._runtime_guard():
            _plan_next_locked(run_dir, _run_dict(), _config_dict(**FAKE_RUNTIME, targets={"pkg-a": "1.1.0"}))

        candidate = load_candidate(run_dir)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["fullAssignment"]["pkg-a"], "1.1.0")
        ledger = load_ledger(run_dir)
        engine_deferrals = [e for e in ledger.get("deferrals", []) if e.get("kind") == "ENGINES_INCOMPATIBLE"]
        self.assertEqual(len(engine_deferrals), 0)

    def test_metadata_abstention_is_compatible_not_a_constraint(self) -> None:
        run_dir = self._run_dir()
        save_config(
            run_dir,
            _config_dict(**FAKE_RUNTIME, targets={"pkg-a": "1.1.0"}),
        )

        # A probe that fails ('' strings) must be treated as NO evidence —
        # compatibility, not incompatibility: no deferral, candidate is made.
        with mock.patch("iterative_migration._npm_engines_node", return_value=""), self._runtime_guard():
            _plan_next_locked(run_dir, _run_dict(), _config_dict(**FAKE_RUNTIME, targets={"pkg-a": "1.1.0"}))

        candidate = load_candidate(run_dir)
        self.assertIsNotNone(candidate)
        ledger = load_ledger(run_dir)
        self.assertEqual(len(ledger.get("deferrals", [])), 0)

    def test_mixed_group_assigns_compatible_and_defers_incompatible(self) -> None:
        run_dir = self._run_dir()
        save_config(
            run_dir,
            _config_dict(**FAKE_RUNTIME, targets={"pkg-a": "1.1.0", "pkg-b": "2.0.0"}),
        )

        def fake_probe(project_dir, name, version, runtime_env):
            if name == "pkg-a" and version == "1.1.0":
                return ">=18"
            if name == "pkg-b" and version == "2.0.0":
                return "^22.18.0"
            return ""

        with mock.patch("iterative_migration._npm_engines_node", side_effect=fake_probe), self._runtime_guard():
            _plan_next_locked(
                run_dir,
                _run_dict(),
                _config_dict(**FAKE_RUNTIME, targets={"pkg-a": "1.1.0", "pkg-b": "2.0.0"}),
            )

        candidate = load_candidate(run_dir)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["fullAssignment"].get("pkg-a"), "1.1.0")
        self.assertEqual(candidate["fullAssignment"].get("pkg-b"), "1.0.0", "Incompatible target must stay at incumbent")
        ledger = load_ledger(run_dir)
        engine_deferrals = [e for e in ledger.get("deferrals", []) if e.get("kind") == "ENGINES_INCOMPATIBLE"]
        self.assertEqual([e["package"] for e in engine_deferrals], ["pkg-b"])

    def test_no_runtime_means_no_engine_gate(self) -> None:
        run_dir = self._run_dir()
        save_config(
            run_dir,
            _config_dict(targets={"pkg-a": "1.1.0"}),
        )

        def fake_probe(project_dir, name, version, runtime_env):
            self.fail("Without a chosen Node the planner must not probe the registry")

        with mock.patch("iterative_migration._npm_engines_node", side_effect=fake_probe):
            _plan_next_locked(run_dir, _run_dict(), _config_dict(targets={"pkg-a": "1.1.0"}))

        candidate = load_candidate(run_dir)
        self.assertIsNotNone(candidate)

    def test_normalize_effective_node_version(self) -> None:
        self.assertEqual(_normalize_effective_node_version("20.11.0"), "20.11.0")
        self.assertEqual(_normalize_effective_node_version("v22.18.0"), "22.18.0")
        self.assertEqual(_normalize_effective_node_version("24.11.0+build5"), "24.11.0")
        self.assertEqual(_normalize_effective_node_version(""), "")
        self.assertEqual(_normalize_effective_node_version(None), "")


if __name__ == "__main__":
    unittest.main()
