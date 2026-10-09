"""Durable source upgrades never transfer old proof authority."""
from __future__ import annotations
import json
import tempfile
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import checkpoint_build_upgrade as upgrade
import iterative_migration as migration
import source_snapshot as source
from baseline_constraint_verifier import BaselineVerifyResult


class CheckpointBuildUpgradeTests(unittest.TestCase):
    def fixture(self, root):
        project = root / "project"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "checkpoint-upgrade-demo", "version": "1.0.0", "private": True}), encoding="utf-8")
        (project / "package-lock.json").write_text(json.dumps({"name": "checkpoint-upgrade-demo", "version": "1.0.0", "lockfileVersion": 3, "requires": True, "packages": {"": {"name": "checkpoint-upgrade-demo", "version": "1.0.0"}}}), encoding="utf-8")
        (project / "check.cjs").write_text("if(require('./package.json').version!=='1.0.0')process.exit(1);\n", encoding="utf-8")
        with mock.patch.object(source, "tool_build_id", return_value="a" * 64):
            old = source.capture_durable_source_snapshot(project, root / "old-snapshot", timeout_seconds=60)
        checkpoint = {"checkpointId": "C4", "status": "VERIFIED", "fullAssignment": {}, "sourceSnapshotKey": old.key, "sourceSnapshotContainer": str(old.container), "projectRelative": "."}
        config = {"verifyConfig": {"commands": ["node check.cjs"], "projectChecks": "strict", "timeoutSeconds": 60}}
        return checkpoint, config

    def test_evicted_upgrade_copy_requires_fresh_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); checkpoint,config=self.fixture(root)
            verify=mock.Mock(return_value=BaselineVerifyResult(True,'passed','fresh'))
            first=upgrade.reopen_checkpoint_source(root/'run',checkpoint,config,verify=verify,progress=lambda *args:None)
            source._force_rmtree(first.container)
            second=upgrade.reopen_checkpoint_source(root/'run',checkpoint,config,verify=verify,progress=lambda *args:None)
            self.assertEqual(verify.call_count,2);self.assertTrue(second.root.is_dir())
            self.assertTrue(Path(checkpoint['sourceSnapshotContainer']).is_dir())
    def test_durable_capture_releases_private_temp_on_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); project=root/'project';project.mkdir();(project/'payload').write_text('keep')
            capture=source.capture_source_snapshot
            seen=[]
            def track(*args,**kwargs):
                snapshot=capture(*args,**kwargs);seen.append(snapshot.container);return snapshot
            with mock.patch.object(source,'capture_source_snapshot',side_effect=track):
                saved=source.capture_durable_source_snapshot(project,root/'saved')
                self.assertFalse(seen[-1].exists());self.assertTrue((saved.root/'payload').exists())
                with mock.patch.object(source,'persist_source_snapshot',side_effect=RuntimeError('publication failed')):
                    with self.assertRaisesRegex(RuntimeError,'publication failed'):
                        source.capture_durable_source_snapshot(project,root/'failed')
                self.assertFalse(seen[-1].exists());self.assertTrue((project/'payload').exists())
    def test_default_opener_stays_strict_and_upgrade_validates_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint, _ = self.fixture(Path(tmp))
            container = Path(checkpoint["sourceSnapshotContainer"])
            with self.assertRaisesRegex(source.SourceCaptureError, "TOOL_BUILD_MISMATCH"):
                source.open_source_snapshot(container)
            source.open_source_snapshot(container, expected_key=checkpoint["sourceSnapshotKey"], allow_build_upgrade=True)
            target = container / "tree" / "check.cjs"
            target.chmod(0o600)
            target.write_text("changed source", encoding="utf-8")
            with self.assertRaisesRegex(source.SourceCaptureError, "CONTENT_MISMATCH"):
                source.open_source_snapshot(container, allow_build_upgrade=True)

    def test_failed_control_does_not_publish_upgrade_or_rewrite_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint, config = self.fixture(root)
            before = json.dumps(checkpoint, sort_keys=True)
            with self.assertRaisesRegex(upgrade.CheckpointBuildUpgradeError, "UNCONFIRMED") as failure:
                upgrade.reopen_checkpoint_source(root / "run", checkpoint, config, verify=lambda *args: BaselineVerifyResult(False, "unknown", "no evidence", command="yarn install --frozen-lockfile", output="opaque lifecycle failure XYZ-42"), progress=lambda *args: None)
            error = failure.exception
            self.assertEqual(error.command, "yarn install --frozen-lockfile")
            self.assertIn("opaque lifecycle failure XYZ-42", str(error))
            diagnostic = json.loads(Path(error.diagnostic_path).read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["kind"], "unknown")
            self.assertEqual(diagnostic["output"], "opaque lifecycle failure XYZ-42")
            self.assertEqual(diagnostic["sourceSnapshotKey"], checkpoint["sourceSnapshotKey"])
            self.assertEqual(before, json.dumps(checkpoint, sort_keys=True))
            self.assertEqual(list((root / "run" / "checkpoint-build-upgrades").glob("*.json")), [])
            self.assertTrue(Path(checkpoint["sourceSnapshotContainer"]).is_dir())

    def test_fresh_control_is_cached_only_for_exact_check_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint, config = self.fixture(root)
            calls = []
            messages = []
            def verify(project, assignment):
                calls.append((project, assignment))
                return BaselineVerifyResult(True, "passed", "checked")
            first = upgrade.reopen_checkpoint_source(root / "run", checkpoint, config, verify=verify, progress=messages.append)
            second = upgrade.reopen_checkpoint_source(root / "run", checkpoint, config, verify=verify, progress=lambda *args: None)
            self.assertNotEqual(first.key, checkpoint["sourceSnapshotKey"])
            self.assertEqual(first.key, second.key)
            self.assertEqual(len(calls), 1)
            self.assertTrue(any("durable source snapshot copy: ready; files=" in message for message in messages))
            self.assertTrue(any("durable source snapshot validation" in message for message in messages))
            changed = {**config, "verifyConfig": {**config["verifyConfig"], "commands": ["node another-check.cjs"]}}
            upgrade.reopen_checkpoint_source(root / "run", checkpoint, changed, verify=verify, progress=lambda *args: None)
            self.assertEqual(len(calls), 2)

    def test_real_red_control_cannot_publish_upgrade_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint, config = self.fixture(root)
            config["verifyConfig"]["commands"] = ['node -e "process.exit(1)"']
            before = json.dumps(checkpoint, sort_keys=True)
            with self.assertRaisesRegex(migration.ProjectUnreadyError, "not confirmed|UNCONFIRMED"):
                migration._open_checkpoint_source(root / "run", checkpoint, config)
            self.assertEqual(before, json.dumps(checkpoint, sort_keys=True))
            self.assertEqual(list((root / "run" / "checkpoint-build-upgrades").glob("*.json")), [])

    def test_real_verifier_checks_preserved_project_after_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint, config = self.fixture(root)
            messages = []
            with mock.patch.object(migration, "_emit_status", side_effect=messages.append):
                snapshot = migration._open_checkpoint_source(root / "run", checkpoint, config, run_id="demo-run", candidate_id="demo-candidate")
            self.assertNotEqual(snapshot.key, checkpoint["sourceSnapshotKey"])
            record = json.loads(next((root / "run" / "checkpoint-build-upgrades").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(record["verification"]["status"], "passed")
            self.assertTrue(any("Re-running" in str(event) for event in messages))
            self.assertEqual(checkpoint["checkpointId"], "C4")
            self.assertTrue(messages)
            self.assertTrue(all(event["runId"] == "demo-run" and event["candidateId"] == "demo-candidate" for event in messages))
            self.assertEqual([event["sequence"] for event in messages], list(range(1, len(messages) + 1)))

    def index_fixture(self, root, producer_build="a" * 64):
        checkpoint, config = self.fixture(root)
        project = root / "project"
        for args in (["init"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)
        with mock.patch.object(source, "tool_build_id", return_value=producer_build):
            snapshot = source.capture_durable_source_snapshot(project, root / "git-snapshot", timeout_seconds=60)
        checkpoint.update(sourceSnapshotKey=snapshot.key, sourceSnapshotContainer=str(snapshot.container))
        index = snapshot.root / ".git/index"
        original = index.read_bytes()
        # A real Git cache refresh changes the sealed index without source edits.
        tracked = snapshot.project_path / "check.cjs"
        os.utime(tracked, (1_700_000_000, 1_700_000_000))
        subprocess.run(["git", "--no-optional-locks", "-C", str(snapshot.root), "update-index", "--refresh"], check=True, capture_output=True)
        self.assertNotEqual(original, index.read_bytes())
        return checkpoint, config, snapshot

    def test_real_git_index_refresh_requires_fresh_real_checks_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint, config, original = self.index_fixture(root)
            before = (original.container / "manifest.json").read_bytes()
            index_before = original.root.joinpath(".git/index").read_bytes()
            recovered = migration._open_checkpoint_source(root / "run", checkpoint, config)
            self.assertNotEqual(recovered.key, checkpoint["sourceSnapshotKey"])
            self.assertEqual(before, (original.container / "manifest.json").read_bytes())
            self.assertEqual(index_before, original.root.joinpath(".git/index").read_bytes())
            record = json.loads(next((root / "run/checkpoint-build-upgrades").glob("*.json")).read_text())
            self.assertEqual(record["identity"]["indexRecovery"]["changedPaths"], [".git/index"])
            self.assertEqual(record["verification"]["status"], "passed")
            again = migration._open_checkpoint_source(root / "run", checkpoint, config)
            self.assertEqual(again.key, recovered.key)

    def test_index_drift_in_same_build_still_requires_new_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint, config, original = self.index_fixture(root, producer_build=source.tool_build_id())
            with self.assertRaisesRegex(source.SourceCaptureError, "CONTENT_MISMATCH"):
                source.open_source_snapshot(original.container)
            verify = mock.Mock(return_value=BaselineVerifyResult(True, "passed", "new checks"))
            recovered = upgrade.reopen_checkpoint_source(root / "run", checkpoint, config, verify=verify, progress=lambda *args: None)
            self.assertNotEqual(recovered.key, checkpoint["sourceSnapshotKey"])
            verify.assert_called_once()
            source.open_source_snapshot(recovered.container, expected_key=recovered.key)

    def test_index_recovery_rejects_source_drift_and_forged_manifest(self):
        for mode in ("source", "manifest", "index-removed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                checkpoint, config, original = self.index_fixture(root)
                if mode == "source":
                    target = original.project_path / "check.cjs"
                    target.chmod(0o600)
                    target.write_text("process.exit(0);", encoding="utf-8")
                elif mode == "manifest":
                    target = original.container / "manifest.json"
                    raw = json.loads(target.read_text())
                    raw["entries"][0]["path"] = "forged"
                    target.write_text(json.dumps(raw), encoding="utf-8")
                else:
                    target = original.root / ".git/index"
                    target.chmod(0o600)
                    target.unlink()
                with self.assertRaises(migration.ProjectUnreadyError):
                    migration._open_checkpoint_source(root / "run", checkpoint, config)
                self.assertFalse(list((root / "run/checkpoint-build-upgrades").glob("*.json")))

    def test_index_recovery_red_checks_and_mutating_verifier_publish_nothing(self):
        for mode in ("red", "mutated"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                checkpoint, config, _ = self.index_fixture(root)
                def verify(project, assignment):
                    if mode == "mutated":
                        target = project / "check.cjs"
                        target.chmod(0o600)
                        target.write_text("changed", encoding="utf-8")
                    return BaselineVerifyResult(mode != "red", "passed" if mode != "red" else "project-check", "control")
                with self.assertRaises((source.SourceCaptureError, upgrade.CheckpointBuildUpgradeError)):
                    upgrade.reopen_checkpoint_source(root / "run", checkpoint, config, verify=verify, progress=lambda *args: None)
                self.assertEqual(list((root / "run/checkpoint-build-upgrades").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
