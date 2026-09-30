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


def make_run_dir(root: Path, *, extra_targets=True, runtime: Optional[dict] = None) -> Path:
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
    if runtime is not None:
        config["runtime"] = runtime
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


def make_discovery_deferral_run_dir(root: Path) -> Path:
    """A run whose bounded discovery found NO newer compatible version for the
    only direct dependency (status ``no-newer-compatible``) — the review P2
    repro. The package is deliberately ABSENT from config.targets (there is no
    executable target version), so without surfacing the intent the task would
    read "0 of 0" and hide the unfulfilled upgrade and its reason."""
    run_dir = root / "run"
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    config = {
        "schemaVersion": 1,
        "projectDir": str(root / "sample-app"),
        "projectName": "sample-app",
        "workspaceId": "ws-1",
        "projectId": "sample-app",
        "targetLevel": "yellow",
        "targets": {},
        "targetDiscovery": [
            {
                "package": "sample",
                "declared": "^8.0.0",
                "installed": "8.5.0",
                "latest": "9.0.0",
                "nodeVersion": "20.11.0",
                "latestNodeSpec": ">=22",
                "rejected": [
                    {"version": "8.1.0", "nodeSpec": "", "status": "not-newer-than-current"},
                    {"version": "8.5.0", "nodeSpec": "", "status": "not-newer-than-current"},
                ],
                "status": "no-newer-compatible",
                "reason": (
                    "latest 9.0.0 requires node >=22, but the run's chosen Node is "
                    "20.11.0; no verified-compatible version strictly newer than the "
                    "INSTALLED 8.5.0 exists within the bounded window — keeping the "
                    "current version"
                ),
            }
        ],
        "verifyConfig": {"commands": ["node check.js"]},
        "policyHash": "policy-hash-abc",
        "budget": {},
        "createdAt": "2026-09-30T00:00:00Z",
        "toolBuildId": "",
    }
    c0 = {
        "schemaVersion": 1, "checkpointId": "C0", "parentCheckpointId": None, "seq": 0,
        "status": "VERIFIED", "sourceSnapshotKey": "snap-c0", "sourceSnapshotContainer": str(root / "snap-c0"),
        "sourceHead": "", "projectRelative": ".", "manifestHash": "mh-1", "lockfileHash": "lh-1",
        "resolvedStateKey": "rsk-c0", "observedResolvedHash": "orh-c0",
        "fullAssignment": {"sample": "8.5.0"},
        "acceptedDelta": {"added": {}, "changed": {}, "removed": {}},
        "cohortId": None,
        "verification": {"status": "passed", "kind": "passed", "commands": ["node check.js"], "failingCommands": []},
        "proofRefs": {"resolvedStateKey": "rsk-c0", "preparationProofKey": ""},
        "audit": {"status": "UNKNOWN", "evidenceRef": ""},
        "createdAt": "2026-09-30T00:01:00Z",
    }
    run = {
        "schemaVersion": 1, "runId": "iter-defer-1", "workspaceId": "ws-1", "projectId": "sample-app",
        "generation": 1, "targetPolicyHash": "policy-hash-abc", "initialSnapshotKey": "snap-c0",
        "activeCheckpointId": "C0", "activeCandidateId": None, "phase": "READY", "terminal": None,
        "budgetLedger": {}, "leaseOwner": "", "deadlineAt": "2026-09-30T02:00:00Z",
        "heartbeatAt": "2026-09-30T00:06:00Z", "createdAt": "2026-09-30T00:00:00Z",
        "updatedAt": "2026-09-30T00:06:00Z", "toolBuildId": "",
    }
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_dir / "run-config.json").write_text(json.dumps(config), encoding="utf-8")
    (run_dir / "checkpoints" / "C0.json").write_text(json.dumps(c0), encoding="utf-8")
    (run_dir / "ledger.json").write_text(json.dumps({"schemaVersion": 1, "blocks": [], "deferrals": []}), encoding="utf-8")
    return run_dir


def make_installed_unknown_run_dir(root: Path) -> Path:
    """A run whose bounded discovery ABSTAINED for the only direct dependency
    (status ``installed-version-unknown`` — the exact installed version cannot
    be read from the lockfile). The review re-check P2 repro: an unresolved
    uncertainty must stay visible in the remainder ("1 of 1", not "0 of 0") as
    an explicit unknown-goals section, WITHOUT claiming a proven lag."""
    run_dir = root / "run"
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
    config = {
        "schemaVersion": 1,
        "projectDir": str(root / "sample-app"),
        "projectName": "sample-app",
        "workspaceId": "ws-1",
        "projectId": "sample-app",
        "targetLevel": "yellow",
        "targets": {},
        "targetDiscovery": [
            {
                "package": "sample",
                "declared": "^8.0.0",
                "status": "installed-version-unknown",
                "reason": (
                    "the exact installed version of sample cannot be read from the "
                    "canonical lockfile; the never-downgrade guard abstains instead of "
                    "choosing a target against an unverifiable baseline"
                ),
            }
        ],
        "verifyConfig": {"commands": ["node check.js"]},
        "policyHash": "policy-hash-abc",
        "budget": {},
        "createdAt": "2026-09-30T00:00:00Z",
        "toolBuildId": "",
    }
    c0 = {
        "schemaVersion": 1, "checkpointId": "C0", "parentCheckpointId": None, "seq": 0,
        "status": "VERIFIED", "sourceSnapshotKey": "snap-c0", "sourceSnapshotContainer": str(root / "snap-c0"),
        "sourceHead": "", "projectRelative": ".", "manifestHash": "mh-1", "lockfileHash": "lh-1",
        "resolvedStateKey": "rsk-c0", "observedResolvedHash": "orh-c0",
        "fullAssignment": {},
        "acceptedDelta": {"added": {}, "changed": {}, "removed": {}},
        "cohortId": None,
        "verification": {"status": "passed", "kind": "passed", "commands": ["node check.js"], "failingCommands": []},
        "proofRefs": {"resolvedStateKey": "rsk-c0", "preparationProofKey": ""},
        "audit": {"status": "UNKNOWN", "evidenceRef": ""},
        "createdAt": "2026-09-30T00:01:00Z",
    }
    run = {
        "schemaVersion": 1, "runId": "iter-unknown-1", "workspaceId": "ws-1", "projectId": "sample-app",
        "generation": 1, "targetPolicyHash": "policy-hash-abc", "initialSnapshotKey": "snap-c0",
        "activeCheckpointId": "C0", "activeCandidateId": None, "phase": "READY", "terminal": None,
        "budgetLedger": {}, "leaseOwner": "", "deadlineAt": "2026-09-30T02:00:00Z",
        "heartbeatAt": "2026-09-30T00:06:00Z", "createdAt": "2026-09-30T00:00:00Z",
        "updatedAt": "2026-09-30T00:06:00Z", "toolBuildId": "",
    }
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_dir / "run-config.json").write_text(json.dumps(config), encoding="utf-8")
    (run_dir / "checkpoints" / "C0.json").write_text(json.dumps(c0), encoding="utf-8")
    (run_dir / "ledger.json").write_text(json.dumps({"schemaVersion": 1, "blocks": [], "deferrals": []}), encoding="utf-8")
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

    def test_no_newer_compatible_deferral_visible_in_final_remainder(self) -> None:
        # Review P2: discovery found no strictly-newer compatible version, the
        # package has NO executable target and is absent from config.targets.
        # The unfulfilled upgrade must stay visible: remainder "1 of 1" (not
        # "0 of 0"), with the reason in the manifest AND in the final ТЗ.
        run_dir = make_discovery_deferral_run_dir(self.root)
        task.export_task_artifact(run_dir)
        manifest = task.current_task(run_dir)
        self.assertIsNotNone(manifest)
        self.assertFalse(manifest["completeness"]["policySatisfied"])
        self.assertEqual(1, manifest["completeness"]["denominator"])
        self.assertEqual(1, manifest["completeness"]["remaining"])
        deferred = [row for row in manifest["deferred"] if row["package"] == "sample"]
        self.assertEqual(1, len(deferred), manifest["deferred"])
        self.assertEqual("8.5.0", deferred[0]["current"])
        self.assertEqual("9.0.0", deferred[0]["lagPolicyTarget"])
        self.assertIn("keeping the current version", deferred[0]["reason"])
        root = run_dir / "task" / manifest["artifactId"]
        ru = (root / "task.ru.md").read_text(encoding="utf-8")
        en = (root / "task.en.md").read_text(encoding="utf-8")
        self.assertIn("**1** из **1**", ru)
        self.assertIn("**1** of **1**", en)
        self.assertIn("sample", ru)
        self.assertIn("keeping the current version", ru)
        self.assertIn("keeping the current version", en)
        self.assertIn("no verified-compatible version strictly newer", en)

    def test_installed_version_unknown_surfaces_in_remainder_not_zero_of_zero(self) -> None:
        # Review re-check P2: discovery ABSTAINED (installed-version-unknown) —
        # the exact installed version cannot be read from the lockfile, so no
        # target is provable. That unresolved uncertainty must keep the task
        # from reading "0 of 0": it counts into the remainder ("1 of 1") and
        # appears in an explicit unknown-goals section in the manifest and the
        # final ТЗ, WITHOUT claiming a proven lag.
        run_dir = make_installed_unknown_run_dir(self.root)
        task.export_task_artifact(run_dir)
        manifest = task.current_task(run_dir)
        self.assertIsNotNone(manifest)
        self.assertFalse(manifest["completeness"]["policySatisfied"])
        self.assertEqual(1, manifest["completeness"]["denominator"])
        self.assertEqual(1, manifest["completeness"]["remaining"])
        unknown = [row for row in manifest["unknownGoals"] if row["package"] == "sample"]
        self.assertEqual(1, len(unknown), manifest["unknownGoals"])
        self.assertEqual("^8.0.0", unknown[0]["declared"])
        self.assertIn("cannot be read from the canonical lockfile", unknown[0]["reason"])
        self.assertEqual([], manifest["deferred"])
        root = run_dir / "task" / manifest["artifactId"]
        ru = (root / "task.ru.md").read_text(encoding="utf-8")
        en = (root / "task.en.md").read_text(encoding="utf-8")
        self.assertIn("**1** из **1**", ru)
        self.assertIn("**1** of **1**", en)
        self.assertIn("### Неопределённые цели (установленная версия неизвестна)", ru)
        self.assertIn("### Unknown goals (installed version not readable)", en)
        # The uncertainty must NOT be dressed up as a confirmed lag or deferral.
        self.assertIn("отставание НЕ подтверждено", ru)
        self.assertIn("NO lag is proven", en)
        self.assertNotIn("Явно отложенные пакеты", ru)
        self.assertNotIn("Explicitly deferred packages", en)

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


def make_legacy_dashboard(root: Path, project_name: str = "sample-app") -> Path:
    """A saved LEGACY Baseline result (tracked dashboard-state shape) with one
    lagging target and one explicitly deferred package."""
    dash = {
        "schemaVersion": 3,
        "projectName": project_name,
        "updatedAt": "2026-01-15T12:00:00Z",
        "projects": {
            project_name: [
                {
                    "name": "is-number",
                    "current_version": "1.2.3",
                    "latest": "7.0.0",
                    "lagPolicyTarget": "7.0.0",
                    "lag": 5.8,
                    "color": "yellow",
                },
                {
                    "name": "@example/widgets",
                    "current_version": "1.2.0",
                    "latest": "2.1.0",
                    "lagPolicyTarget": "2.1.0",
                    "planner_deferred": True,
                    "planner_deferred_reason": "PEER_RESOLUTION_DEFERRED: peer cycle unresolved",
                    "color": "red",
                },
            ]
        },
        "issues": [],
    }
    path = root / "legacy-dashboard-state.json"
    path.write_text(json.dumps(dash), encoding="utf-8")
    return path

class IterativeTaskRuntimeContractTests(unittest.TestCase):
    """D3.5: the task text carries the pinned Node/CI runtime contract and the
    explicit no-environment-change/INFRA_BLOCKED instruction (RU and EN)."""

    RUNTIME = {
        "requested": "22",
        "effectiveVersion": "22.18.0",
        "nodePath": "/opt/node22/bin/node",
        "npmPath": "/opt/node22/bin/npm",
        "source": "major-alias",
        "packageManager": "yarn",
        "packageManagerVersion": "1.22.19",
        "platform": "linux",
        "arch": "x86_64",
        "contractHash": "rt-hash-123",
        "hash": "rt-hash-123",
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _markdown(self, language: str, runtime: Optional[dict]) -> str:
        run_dir = make_run_dir(self.root, runtime=runtime)
        task.export_task_artifact(run_dir)
        manifest = task.current_task(run_dir)
        name = f"task.{language}.md"
        return (run_dir / "task" / manifest["artifactId"] / name).read_text(encoding="utf-8")

    def test_runtime_summary_formats_the_contract(self) -> None:
        self.assertEqual(
            task._runtime_summary({"runtime": self.RUNTIME}),
            "22 \u2192 22.18.0 [major-alias] \u00b7 yarn 1.22.19 \u00b7 linux/x86_64",
        )
        self.assertEqual(task._runtime_summary({"runtime": {}}), "")
        self.assertEqual(task._runtime_summary({}), "")

    def test_ru_task_carries_runtime_row_and_environment_section(self) -> None:
        md = self._markdown("ru", self.RUNTIME)
        self.assertIn("Node/CI runtime", md)
        self.assertIn("22 \u2192 22.18.0", md)
        self.assertIn("yarn 1.22.19", md)
        self.assertIn("## Окружение (не менять)", md)
        self.assertIn("INFRA_BLOCKED", md)
        self.assertIn("Не меняй заданный Node-рантайм", md)

    def test_en_task_carries_runtime_row_and_environment_section(self) -> None:
        md = self._markdown("en", self.RUNTIME)
        self.assertIn("Node/CI runtime", md)
        self.assertIn("22 \u2192 22.18.0", md)
        self.assertIn("## Environment (do not change)", md)
        self.assertIn("INFRA_BLOCKED", md)

    def test_unset_runtime_shows_dash_and_no_claim(self) -> None:
        md = self._markdown("ru", None)
        self.assertIn("Node/CI runtime", md)
        self.assertIn("## Окружение (не менять)", md)
        self.assertNotIn("22 \u2192 22.18.0", md)
        self.assertNotIn("yarn 1.22.19", md)


class IterativeTaskLegacyExportTests(unittest.TestCase):
    """R9: the LEGACY saved Baseline result is imported by the SAME task builder
    into the shared task contract WITHOUT any rerun or invented proof."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_legacy_export_publishes_task_without_run_state(self):
        run_dir = self.root / "legacy-run"
        dash = make_legacy_dashboard(self.root)
        summary = task.export_legacy_task_artifact(dash, "sample-app", run_dir)
        self.assertEqual(task.ARTIFACT_SOURCE_LEGACY_BASELINE, summary["artifactSource"])
        self.assertTrue(summary["artifactId"].startswith("task-legacy-"))
        manifest = task.current_task(run_dir)
        self.assertIsNotNone(manifest)
        self.assertEqual("legacy-baseline", manifest.get("artifactSource"))
        self.assertEqual("legacy-exported-not-reverified", manifest["verification"]["status"])
        # exactVersions is the FULL ASSIGNMENT ("as-is"), never a fabricated
        # acceptance: nothing was accepted by this import.
        self.assertEqual("1.2.3", manifest["exactVersions"]["is-number"])
        self.assertEqual("1.2.0", manifest["exactVersions"]["@example/widgets"])
        deferred = [row for row in manifest["deferred"] if row["package"] == "@example/widgets"]
        self.assertEqual(1, len(deferred))
        self.assertIn("peer cycle", deferred[0]["reason"])
        self.assertFalse(manifest["completeness"]["policySatisfied"])
        self.assertEqual(2, manifest["completeness"]["denominator"])
        self.assertEqual(2, manifest["completeness"]["remaining"])
        # The imported TARGET (7.0.0) must remain visible in the published body.
        body_ru = run_dir / "task" / manifest["artifactId"] / "task.ru.md"
        self.assertTrue("7.0.0" in body_ru.read_text(encoding="utf-8"))
        for language in ("ru", "en"):
            body = run_dir / "task" / manifest["artifactId"] / f"task.{language}.md"
            self.assertTrue(body.exists(), body)

    def test_legacy_export_preserves_previous_artifacts_as_history(self):
        run_dir = self.root / "legacy-run"
        dash = make_legacy_dashboard(self.root)
        first = task.export_legacy_task_artifact(dash, "sample-app", run_dir)
        dash.write_text(dash.read_text(encoding="utf-8").replace('"lagPolicyTarget": "7.0.0"', '"lagPolicyTarget": "7.1.0"'), encoding="utf-8")
        second = task.export_legacy_task_artifact(dash, "sample-app", run_dir)
        self.assertNotEqual(first["artifactId"], second["artifactId"])
        self.assertEqual(1, len(second["superseded"]))
        self.assertEqual(first["artifactId"], second["superseded"][0]["artifactId"])

    def test_legacy_export_insufficient_dashboard_is_diagnosed(self):
        run_dir = self.root / "legacy-run"
        dash = make_legacy_dashboard(self.root)
        parsed = json.loads(dash.read_text(encoding="utf-8"))
        del parsed["projects"]["sample-app"][0]["lagPolicyTarget"]
        parsed["projects"]["sample-app"][0]["planned_action_default"] = "latest"
        dash.write_text(json.dumps(parsed), encoding="utf-8")
        with self.assertRaises(task.TaskExportError) as ctx:
            task.export_legacy_task_artifact(dash, "sample-app", run_dir)
        self.assertEqual("LEGACY_BASELINE_INSUFFICIENT", ctx.exception.code)
        self.assertTrue(any("no exact target version" in message for message in ctx.exception.missing))
        self.assertFalse((run_dir / "task").exists(), "No artifact must be published for insufficient input")

    def test_legacy_export_missing_file_never_invents_evidence(self):
        run_dir = self.root / "legacy-run"
        with self.assertRaises(task.TaskExportError) as ctx:
            task.export_legacy_task_artifact(self.root / "absent.json", "sample-app", run_dir)
        self.assertEqual("LEGACY_BASELINE_INSUFFICIENT", ctx.exception.code)
        self.assertIn("dashboard-state.json", ctx.exception.missing)

    def test_legacy_marker_target_is_never_exact(self):
        # 'latest' is a marker, not an exact version: a row with only markers
        # and no deferral must not produce an executable target.
        run_dir = self.root / "legacy-run"
        dash = make_legacy_dashboard(self.root)
        parsed = json.loads(dash.read_text(encoding="utf-8"))
        parsed["projects"]["sample-app"][0].update(
            {"lagPolicyTarget": "latest", "lag_target": "-", "planned_action_default": "latest"}
        )
        dash.write_text(json.dumps(parsed), encoding="utf-8")
        with self.assertRaises(task.TaskExportError) as ctx:
            task.export_legacy_task_artifact(dash, "sample-app", run_dir)
        self.assertEqual("LEGACY_BASELINE_INSUFFICIENT", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
