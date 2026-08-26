from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import block_v_prepared_artifact as store


class PreparedArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = os.environ.get("DEPLOOM_VERIFICATION_ROOT")
        os.environ["DEPLOOM_VERIFICATION_ROOT"] = str(self.root / "substrate")
        store.configure_prepared_artifact_store(self.root / "proofs")
        self.source = self.root / "source"
        self.source.mkdir()
        self.key = "a" * 32
        trees = store.prepared_snapshot_storage_root()
        assert trees is not None
        self.stage = Path(tempfile.mkdtemp(prefix="artifact-", dir=trees)) / "workspace"
        (self.stage / "project").mkdir(parents=True)
        (self.stage / "project" / "package.json").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("DEPLOOM_VERIFICATION_ROOT", None)
        else:
            os.environ["DEPLOOM_VERIFICATION_ROOT"] = self.old_root
        self.temp.cleanup()

    def test_publish_then_load_exact_record(self):
        ok = store.publish_prepared_artifact_record(
            key=self.key,
            workspace_root=self.stage,
            project_relative=Path("project"),
            source_project=self.source,
            storage_mode="test",
            observed_resolved_versions={"x": "1.0.0"},
            observed_resolved_hash="b" * 64,
        )
        self.assertTrue(ok)
        record = store.load_prepared_artifact_record(self.key, self.source)
        self.assertIsNotNone(record)
        self.assertEqual(record["observedResolvedVersions"], {"x": "1.0.0"})

    def test_other_source_path_does_not_alias(self):
        store.publish_prepared_artifact_record(
            key=self.key, workspace_root=self.stage, project_relative=Path("project"),
            source_project=self.source, storage_mode="test",
            observed_resolved_versions={}, observed_resolved_hash="c" * 64,
        )
        other = self.root / "other"
        other.mkdir()
        self.assertIsNone(store.load_prepared_artifact_record(self.key, other))

    def test_same_size_restored_mtime_tampering_invalidates_cross_process_hit(self):
        self.assertTrue(store.publish_prepared_artifact_record(
            key=self.key, workspace_root=self.stage, project_relative=Path("project"),
            source_project=self.source, storage_mode="test",
            observed_resolved_versions={}, observed_resolved_hash="e" * 64,
        ))
        subject = self.stage / "project" / "package.json"
        before = subject.stat()
        subject.write_text("[]", encoding="utf-8")
        os.utime(subject, ns=(before.st_atime_ns, before.st_mtime_ns))
        store._clear_artifact_integrity_validation_cache_for_tests()

        self.assertIsNone(store.load_prepared_artifact_record(self.key, self.source))
        record_path = store.configured_prepared_artifact_root() / "index" / f"{self.key}.json"
        self.assertFalse(record_path.exists())

    def test_tool_build_change_invalidates_durable_record(self):
        from unittest.mock import patch

        self.assertTrue(store.publish_prepared_artifact_record(
            key=self.key, workspace_root=self.stage, project_relative=Path("project"),
            source_project=self.source, storage_mode="test",
            observed_resolved_versions={}, observed_resolved_hash="f" * 64,
        ))
        store._clear_artifact_integrity_validation_cache_for_tests()
        with patch.object(store, "tool_build_id", return_value="0" * 64):
            self.assertIsNone(store.load_prepared_artifact_record(self.key, self.source))

    def test_legacy_record_schema_is_a_safe_miss(self):
        self.assertTrue(store.publish_prepared_artifact_record(
            key=self.key, workspace_root=self.stage, project_relative=Path("project"),
            source_project=self.source, storage_mode="test",
            observed_resolved_versions={}, observed_resolved_hash="1" * 64,
        ))
        record_path = store.configured_prepared_artifact_root() / "index" / f"{self.key}.json"
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        payload["schemaVersion"] = 1
        record_path.write_text(json.dumps(payload), encoding="utf-8")
        store._clear_artifact_integrity_validation_cache_for_tests()
        self.assertIsNone(store.load_prepared_artifact_record(self.key, self.source))

    def test_record_is_not_authority(self):
        store.publish_prepared_artifact_record(
            key=self.key, workspace_root=self.stage, project_relative=Path("project"),
            source_project=self.source, storage_mode="test",
            observed_resolved_versions={}, observed_resolved_hash="d" * 64,
        )
        index = store.configured_prepared_artifact_root() / "index" / f"{self.key}.json"
        payload = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual("PRECONDITION_CACHE", payload["authority"])
        self.assertEqual(2, payload["schemaVersion"])
        self.assertEqual(64, len(payload["artifactContentKey"]))
        self.assertEqual(64, len(payload["toolBuildId"]))


if __name__ == "__main__":
    unittest.main()
