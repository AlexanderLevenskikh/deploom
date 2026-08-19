from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import block_v_prepared_artifact as store


class PreparedArtifactGcHotfixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = os.environ.get("DEPLOOM_VERIFICATION_ROOT")
        os.environ["DEPLOOM_VERIFICATION_ROOT"] = str(self.root / "verification")

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("DEPLOOM_VERIFICATION_ROOT", None)
        else:
            os.environ["DEPLOOM_VERIFICATION_ROOT"] = self.old_root
        self.temp.cleanup()

    def configure_without_gc_thread(self) -> Path:
        with patch.object(store, "schedule_prepared_artifact_maintenance", return_value=True):
            configured = store.configure_prepared_artifact_store(self.root / "proofs")
        self.assertIsNotNone(configured)
        assert configured is not None
        return configured

    def test_configure_never_physically_prunes_inline(self) -> None:
        with (
            patch.object(
                store,
                "prune_prepared_artifact_store",
                side_effect=AssertionError("physical prune must not run on verifier critical path"),
            ),
            patch.object(store, "schedule_prepared_artifact_maintenance", return_value=True) as schedule,
        ):
            configured = store.configure_prepared_artifact_store(self.root / "proofs")

        self.assertIsNotNone(configured)
        schedule.assert_called_once()

    def test_background_prune_is_bounded_to_one_artifact_per_pass(self) -> None:
        configured = self.configure_without_gc_thread()
        source = self.root / "source"
        source.mkdir()

        trees = store.prepared_snapshot_storage_root()
        self.assertIsNotNone(trees)
        assert trees is not None

        for index in range(3):
            workspace = trees / f"artifact-{index}" / "workspace"
            (workspace / "project").mkdir(parents=True)
            key = f"{index + 1:032x}"
            self.assertTrue(store.publish_prepared_artifact_record(
                key=key,
                workspace_root=workspace,
                project_relative=Path("project"),
                source_project=source,
                storage_mode="test",
                observed_resolved_versions={},
                observed_resolved_hash=f"{index + 1:064x}",
            ))

        index_dir = configured / "index"
        records = sorted(index_dir.glob("*.json"))
        for offset, record in enumerate(records, start=1):
            os.utime(record, (offset, offset))

        removed = store.prune_prepared_artifact_store(max_count=1, max_removals=1)
        self.assertEqual(1, removed)
        self.assertEqual(2, len(list(index_dir.glob("*.json"))))

    def test_interrupted_delete_remains_recoverable_in_trash(self) -> None:
        configured = self.configure_without_gc_thread()
        source = self.root / "source"
        source.mkdir()

        trees = store.prepared_snapshot_storage_root()
        self.assertIsNotNone(trees)
        assert trees is not None

        keys = ("a" * 32, "b" * 32)
        for index, key in enumerate(keys):
            workspace = trees / f"artifact-{index}" / "workspace"
            (workspace / "project").mkdir(parents=True)
            self.assertTrue(store.publish_prepared_artifact_record(
                key=key,
                workspace_root=workspace,
                project_relative=Path("project"),
                source_project=source,
                storage_mode="test",
                observed_resolved_versions={},
                observed_resolved_hash=("c" if index == 0 else "d") * 64,
            ))

        index_dir = configured / "index"
        os.utime(index_dir / f"{keys[0]}.json", (1, 1))
        os.utime(index_dir / f"{keys[1]}.json", (2, 2))

        with patch.object(store.shutil, "rmtree", side_effect=OSError("simulated interrupted cleanup")):
            removed = store.prune_prepared_artifact_store(max_count=1, max_removals=1)

        self.assertEqual(1, removed)
        self.assertFalse((index_dir / f"{keys[0]}.json").exists())
        trash = configured / "trash"
        self.assertTrue(trash.is_dir())
        self.assertTrue(any(trash.iterdir()))

    def test_verifier_marks_cache_configuration_boundary(self) -> None:
        verifier = (
            Path(__file__).resolve().parents[1] / "baseline_constraint_verifier.py"
        ).read_text(encoding="utf-8")
        self.assertIn("prepared-artifact cache configure: started", verifier)
        self.assertIn("prepared-artifact cache configure: ready; cleanup=background", verifier)


if __name__ == "__main__":
    unittest.main()
