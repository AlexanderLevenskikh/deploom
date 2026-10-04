"""Durable source upgrades never transfer old proof authority."""
from __future__ import annotations
import json
import tempfile
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
            with self.assertRaisesRegex(upgrade.CheckpointBuildUpgradeError, "UNCONFIRMED"):
                upgrade.reopen_checkpoint_source(root / "run", checkpoint, config, verify=lambda *args: BaselineVerifyResult(False, "unknown", "no evidence"), progress=lambda *args: None)
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


if __name__ == "__main__":
    unittest.main()
