"""Regression: ТЗ artifact contract for the iterative migration.

The ТЗ is built ONLY from durable JSON state (run.json / run-config.json /
checkpoints / ledger): no Baseline, no project checks, no HTML. The artifact
is an atomically published linked pair (task text + manifest with identities
and hashes); the previous valid set survives and the pointer flips last.
Explicit planner deferrals stay immutable and in the remainder; a deferred
target is never turned into an executable target.  Identity/hash drift makes
the task stale for dispatch.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import iterative_task as task


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_run_dir(root: Path, *, extra_targets=True) -> Path:
    run_dir = root / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    targets = {"is-number": "7.0.0", "is-finite": "1.1.0"}
    if extra_targets:
        targets["@example/widgets"] = "2.1.0"
    config = {
        "schemaVersion": 1,
        "projectDir": str(root / "sample-app"),
        "projectName": "sample-app",
        "workspaceId": "ws-1",
        "projectId": "sample-app",
        "targetLevel": "yellow",
        "targets": targets,
        "verifyConfig": {"commands": ["node check.js"], "projectChecks": "adaptive"},
        "policyHash": "policy-hash-abc",
        "budget": {},
        "createdAt": "2026-09-29T00:00:00Z",
        "toolBuildId": "",
    }
    c0 = {
        "schemaVersion": 1, "checkpointId": "C0", "parentCheckpointId": None, "seq": 0,
        "status": "VERIFIED", "sourceSnapshotKey": "snap-c0", "sourceSnapshotContainer": str(root / "snap-c0"),
        "sourceHead": "", "projectRelative": ".", "manifestHash": "mh-1", "lockfileHash": "lh-1",
        "resolvedStateKey": "rsk-c0", "observedResolvedHash": "orh-c0",
        "fullAssignment": {"is-number": "5.0.0", "is-finite": "1.0.0", "@example/widgets": "1.2.0"},
        "acceptedDelta": {"added": {}, "changed": {}, "removed": {}},
        "cohortId": None,
        "verification": {"status": "passed", "kind": "passed", "commands": ["node check.js"], "failingCommands": []},
        "proofRefs": {"resolvedStateKey": "rsk-c0", "preparationProofKey": ""},
        "audit": {"status": "UNKNOWN", "evidenceRef": ""},
        "createdAt": "2026-09-29T00:01:00Z",
    }
    c1 = {
        "schemaVersion": 1, "checkpointId": "C1", "parentCheckpointId": "C0", "seq": 1,
        "status": "VERIFIED", "sourceSnapshotKey": "snap-c1", "sourceSnapshotContainer": str(root / "snap-c1"),
        "sourceHead": "", "projectRelative": ".", "manifestHash": "mh-2", "lockfileHash": "lh-2",
        "resolvedStateKey": "rsk-c1", "observedResolvedHash": "orh-c1",
        "fullAssignment": {"is-number": "7.0.0", "is-finite": "1.0.0", "@example/widgets": "1.2.0"},
        "acceptedDelta": {"added": {}, "changed": {"is-number": "7.0.0"}, "removed": {}},
        "cohortId": "cohort-a",
        "verification": {"status": "passed", "kind": "passed", "commands": ["node check.js"], "failingCommands": []},
        "proofRefs": {"resolvedStateKey": "rsk-c1", "preparationProofKey": ""},
        "audit": {"status": "UNKNOWN", "evidenceRef": ""},
        "createdAt": "2026-09-29T00:05:00Z",
    }
    ledger = {
        "schemaVersion": 1,
        "blocks": [
            {
                "kind": "NOT_ACTIONABLE", "package": "@example/widgets",
                "reason": "PEER_RESOLUTION_DEFERRED: desired=2.1.0; resolved=current 1.2.0; component unresolved",
                "baseCheckpointId": "C1",
            }
        ],
        "deferrals": [
            {"package": "@example/widgets", "kind": "INCONCLUSIVE", "reason": "resolver could not produce a feasible revision"}
        ],
        "counters": {},
    }
    run = {
        "schemaVersion": 1, "runId": "iter-test-123", "workspaceId": "ws-1", "projectId": "sample-app",
        "generation": 2, "targetPolicyHash": "policy-hash-abc", "initialSnapshotKey": "snap-c0",
        "activeCheckpointId": "C1", "activeCandidateId": None, "phase": "READY", "terminal": None,
        "budgetLedger": {}, "leaseOwner": "", "deadlineAt": "2026-09-29T02:00:00Z",
        "heartbeatAt": "2026-09-29T00:06:00Z", "createdAt": "2026-09-29T00:00:00Z",
        "updatedAt": "2026-09-29T00:06:00Z", "toolBuildId": "",
    }
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_dir / "run-config.json").write_text(json.dumps(config), encoding="utf-8")
    (run_dir / "checkpoints" / "C0.json").write_text(json.dumps(c0), encoding="utf-8")
    (run_dir / "checkpoints" / "C1.json").write_text(json.dumps(c1), encoding="utf-8")
    (run_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    return run_dir


class IterativeTaskExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_partial_plan_exports_with_deferred_kept_in_remainder(self):
        run_dir = make_run_dir(self.root)
        summary = task.export_task_artifact(run_dir)
        self.assertEqual("export-task.done", summary["event"])
        manifest = task.current_task(run_dir)
        self.assertIsNotNone(manifest)
        self.assertEqual("iter-test-123", manifest["runId"])
        self.assertEqual("C1", manifest["targetCheckpointId"])
        # Exact accepted versions are the source of truth.
        self.assertEqual("7.0.0", manifest["exactVersions"]["is-number"])
        # One accepted action, deferred kept with a reason.
        self.assertEqual(1, len(manifest["actions"]))
        self.assertEqual("is-number", manifest["actions"][0]["package"])
        deferred = manifest["deferred"]
        self.assertEqual(1, len(deferred))
        self.assertEqual("@example/widgets", deferred[0]["package"])
        self.assertIn("PEER_RESOLUTION_DEFERRED", deferred[0]["reason"])
        # Deferred stays immutable at its current version: the assignment keeps
        # 1.2.0 and policy is NOT satisfied.
        self.assertEqual("1.2.0", manifest["exactVersions"]["@example/widgets"])
        self.assertFalse(manifest["completeness"]["policySatisfied"])
        self.assertEqual(3, manifest["completeness"]["denominator"])
        self.assertEqual(2, manifest["completeness"]["remaining"])
        # Content hash is bound per language.
        md = (run_dir / "task" / manifest["artifactId"] / "task.ru.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["contents"]["ru"]["contentHash"], sha256(md))
        self.assertIn("@example/widgets", md)
        self.assertIn("1.2.0", md)

    def test_ru_and_en_artifacts_are_published_with_shared_manifest(self):
        run_dir = make_run_dir(self.root)
        task.export_task_artifact(run_dir)
        manifest = task.current_task(run_dir)
        self.assertEqual(["en", "ru"], manifest["languages"])
        root = run_dir / "task" / manifest["artifactId"]
        ru = (root / "task.ru.md").read_text(encoding="utf-8")
        en = (root / "task.en.md").read_text(encoding="utf-8")
        self.assertIn("Задание на адаптацию", ru)
        self.assertIn("Dependency adaptation assignment", en)
        self.assertIn("PEER_RESOLUTION_DEFERRED", en)
        self.assertIn("@example/widgets", en)
        self.assertEqual(manifest["contents"]["ru"]["contentHash"], sha256(ru))
        self.assertEqual(manifest["contents"]["en"]["contentHash"], sha256(en))

    def test_insufficient_json_is_a_specific_error_without_artifacts(self):
        run_dir = self.root / "empty-run"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"runId": "iter-x"}), encoding="utf-8"
        )
        (run_dir / "run-config.json").write_text(json.dumps({"projectName": "x"}), encoding="utf-8")
        with self.assertRaises(task.TaskExportError) as ctx:
            task.export_task_artifact(run_dir)
        self.assertEqual("TASK_INPUT_INSUFFICIENT", ctx.exception.code)
        self.assertTrue(any("run-config.targets" in field for field in ctx.exception.missing))
        self.assertFalse((run_dir / "task").exists())
        # Diagnose-only entry reports the same missing fields without side effects.
        missing = task.verify_task_input_errors(run_dir)
        self.assertTrue(any("targets" in field for field in missing))

    def test_identity_drift_marks_the_task_stale_for_dispatch(self):
        run_dir = make_run_dir(self.root)
        task.export_task_artifact(run_dir)
        staleness = task.task_staleness(run_dir)
        self.assertFalse(staleness["stale"], staleness["reason"])
        # Policy changed -> stale.
        config = json.loads((run_dir / "run-config.json").read_text(encoding="utf-8"))
        config["policyHash"] = "policy-hash-new"
        (run_dir / "run-config.json").write_text(json.dumps(config), encoding="utf-8")
        self.assertTrue(task.task_staleness(run_dir)["stale"])
        # New checkpoint accepted (candidate changed) -> stale.
        config = json.loads((run_dir / "run-config.json").read_text(encoding="utf-8"))
        config["policyHash"] = "policy-hash-abc"
        (run_dir / "run-config.json").write_text(json.dumps(config), encoding="utf-8")
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        run["activeCheckpointId"] = "C99"
        (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
        self.assertTrue(task.task_staleness(run_dir)["stale"])
        self.assertIn("checkpoint", task.task_staleness(run_dir)["reason"])

    def test_atomic_publish_keeps_the_previous_valid_set_and_pointer(self):
        run_dir = make_run_dir(self.root)
        task.export_task_artifact(run_dir)
        first = task.current_task(run_dir)
        first_root = run_dir / "task" / first["artifactId"]
        self.assertTrue((first_root / "task-manifest.json").exists())
        # A NEW accepted checkpoint (different assignment) supersedes the task:
        # the old artifact directory survives and is marked stale in history.
        active = {
            "schemaVersion": 1, "checkpointId": "C2", "parentCheckpointId": "C1", "seq": 2,
            "status": "VERIFIED", "sourceSnapshotKey": "snap-c2", "sourceSnapshotContainer": str(self.root / "snap-c2"),
            "sourceHead": "", "projectRelative": ".", "manifestHash": "mh-3", "lockfileHash": "lh-3",
            "resolvedStateKey": "rsk-c2", "observedResolvedHash": "orh-c2",
            "fullAssignment": {"is-number": "7.0.0", "is-finite": "1.1.0", "@example/widgets": "1.2.0"},
            "acceptedDelta": {"added": {}, "changed": {"is-finite": "1.1.0"}, "removed": {}},
            "cohortId": "cohort-b",
            "verification": {"status": "passed", "kind": "passed", "commands": ["node check.js"], "failingCommands": []},
            "proofRefs": {"resolvedStateKey": "rsk-c2", "preparationProofKey": ""},
            "audit": {"status": "UNKNOWN", "evidenceRef": ""},
            "createdAt": "2026-09-29T00:10:00Z",
        }
        (run_dir / "checkpoints" / "C2.json").write_text(json.dumps(active), encoding="utf-8")
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        run["activeCheckpointId"] = "C2"
        (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
        task.export_task_artifact(run_dir)
        second = task.current_task(run_dir)
        self.assertNotEqual(first["artifactId"], second["artifactId"])
        self.assertTrue((first_root / "task-manifest.json").exists())
        pointer = json.loads((run_dir / "task" / task.CURRENT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(second["artifactId"], pointer["artifactId"])
        history = json.loads((run_dir / "task" / task.HISTORY_FILENAME).read_text(encoding="utf-8"))
        stale_entries = [entry for entry in history if entry.get("stale")]
        self.assertEqual(1, len(stale_entries))
        self.assertEqual(first["artifactId"], stale_entries[0]["artifactId"])

    def test_manifest_is_identity_bound_and_useable_by_consumers(self):
        run_dir = make_run_dir(self.root)
        task.export_task_artifact(run_dir)
        manifest = task.current_task(run_dir)
        for key in ("runId", "targetCheckpointId", "scopeHash", "policyHash", "sourceSnapshotKey",
                    "manifestHash", "lockfileHash", "resolvedStateKey", "contentHash"):
            self.assertTrue(manifest.get(key), f"{key} empty")
        manifest_path = run_dir / "task" / manifest["artifactId"] / task.TASK_MANIFEST_FILENAME
        on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, manifest)

    def test_reexport_after_lost_pointer_is_idempotent(self):
        # Kill between artifact write and pointer flip: the pointer is gone,
        # the full artifact set is on disk. A re-export (fresh process) must
        # reuse the SAME artifact dir, not fork a second one, and restore the
        # pointer.
        run_dir = make_run_dir(self.root)
        task.export_task_artifact(run_dir)
        first = task.current_task(run_dir)
        (run_dir / "task" / task.CURRENT_FILENAME).unlink()
        task.export_task_artifact(run_dir)
        second = task.current_task(run_dir)
        self.assertEqual(first["artifactId"], second["artifactId"])
        self.assertEqual(first["contentHash"], second["contentHash"])
        dirs = sorted(p.name for p in (run_dir / "task").iterdir() if p.is_dir())
        self.assertEqual(1, len(dirs), f"artifact dirs forked: {dirs}")

    def test_partial_artifact_gets_a_fresh_full_set(self):
        # Kill mid-write leaves a truncated set (missing one language body).
        # The stale partial dir must never be trusted for dispatch: re-export
        # publishes a fresh complete set and moves the pointer there.
        run_dir = make_run_dir(self.root)
        task.export_task_artifact(run_dir)
        first = task.current_task(run_dir)
        (run_dir / "task" / first["artifactId"] / "task.ru.md").unlink()
        task.export_task_artifact(run_dir)
        second = task.current_task(run_dir)
        self.assertNotEqual(first["artifactId"], second["artifactId"])
        second_root = run_dir / "task" / second["artifactId"]
        self.assertTrue((second_root / "task.ru.md").exists())
        self.assertTrue((second_root / "task.en.md").exists())
        self.assertTrue((second_root / task.TASK_MANIFEST_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
