from __future__ import annotations

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

    def test_record_is_not_authority(self):
        store.publish_prepared_artifact_record(
            key=self.key, workspace_root=self.stage, project_relative=Path("project"),
            source_project=self.source, storage_mode="test",
            observed_resolved_versions={}, observed_resolved_hash="d" * 64,
        )
        index = store.configured_prepared_artifact_root() / "index" / f"{self.key}.json"
        self.assertIn('"authority": "PRECONDITION_CACHE"', index.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
