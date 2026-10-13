"""Snapshot placement is only a storage optimization; source authority is unchanged."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import source_snapshot as snapshot
from source_snapshot_storage import SNAPSHOT_SUBDIR, candidate_snapshot_parents, new_snapshot_container


class SnapshotColocationTests(unittest.TestCase):
    def tearDown(self) -> None:
        snapshot.clear_source_snapshot_epochs()

    def test_explicit_verification_volume_contains_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "project"
            root.mkdir()
            (root / "package.json").write_text('{"name":"fixture"}', encoding="utf-8")
            (root / "source.ts").write_text("export const n = 1\n", encoding="utf-8")
            verify = Path(raw) / "verification"
            with patch.dict(os.environ, {"DEPLOOM_VERIFICATION_ROOT": str(verify)}):
                result = snapshot.capture_source_snapshot(root)
                self.assertEqual(verify / SNAPSHOT_SUBDIR, result.container.parent)
                self.assertEqual((root / "source.ts").read_bytes(), (result.root / "source.ts").read_bytes())
                snapshot.validate_source_snapshot(result)

    def test_root_inside_source_is_rejected_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "project"
            root.mkdir()
            forbidden = root / "nested" / "verification"
            with patch.dict(os.environ, {"DEPLOOM_VERIFICATION_ROOT": str(forbidden)}):
                container = new_snapshot_container(root, prefix="dependency-flow-source-snapshot-")
            self.assertFalse(forbidden.exists())
            self.assertFalse(container.is_relative_to(root))
            snapshot._force_rmtree(container)

    def test_unwritable_storage_falls_back_without_trusting_its_contents(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "project"
            root.mkdir()
            occupied = Path(raw) / "occupied-file"
            occupied.write_text("not a directory", encoding="utf-8")
            with patch.dict(os.environ, {"DEPLOOM_VERIFICATION_ROOT": str(occupied)}):
                container = new_snapshot_container(root, prefix="dependency-flow-source-snapshot-")
            self.assertNotIn(occupied, container.parents)
            snapshot._force_rmtree(container)

    def test_reaper_discovers_orphan_in_verification_volume(self) -> None:
        from unittest.mock import patch as mock_patch
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "source"
            root.mkdir()
            verify = Path(raw) / "verification"
            with patch.dict(os.environ, {"DEPLOOM_VERIFICATION_ROOT": str(verify)}):
                container = new_snapshot_container(root, prefix=snapshot.SOURCE_SNAPSHOT_CONTAINER_PREFIX)
                (container / "tree").mkdir()
                (container / ".deploom-trial-owner.json").write_text('{"pid":999999999}', encoding="utf-8")
                import time
                past = time.time() - 48 * 3600
                os.utime(container, (past, past))
                with mock_patch("verification_storage_maintenance.dead_trial_owner", return_value=True):
                    result = snapshot.reap_orphaned_source_snapshots()
                self.assertGreaterEqual(result["reaped"], 1)
                self.assertFalse(container.exists())


if __name__ == "__main__":
    unittest.main()
