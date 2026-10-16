"""Durable publication consumes call-owned snapshots without a second copy."""
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import source_snapshot as source


class SnapshotPromotionTests(unittest.TestCase):
    def test_owned_snapshot_moves_without_copy_and_revalidates_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root / "project"; project.mkdir()
            (project / "payload").write_bytes(b"sealed" * 10000)
            from source_snapshot_storage import prefer_snapshot_parent
            with prefer_snapshot_parent(root):
                snapshot = source.capture_source_snapshot(project)
            self.addCleanup(source._force_rmtree, snapshot.container)
            destination = (root / "durable").resolve()
            self.addCleanup(source._force_rmtree, destination)
            identity = (snapshot.root / "payload").stat().st_ino
            with patch.object(source.shutil, "copytree", side_effect=AssertionError("extra copy")), \
                 patch.object(source, "open_source_snapshot", wraps=source.open_source_snapshot) as opened:
                saved = source.persist_source_snapshot(snapshot, destination, _consume=True)
            self.assertFalse(snapshot.container.exists())
            self.assertEqual(identity, (saved.root / "payload").stat().st_ino)
            self.assertEqual(snapshot.key, saved.key)
            self.assertEqual(destination, opened.call_args.args[0])
            self.assertEqual(snapshot.key, source.open_source_snapshot(destination).key)

    def test_default_publication_preserves_shared_active_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root / "project"; project.mkdir()
            (project / "payload").write_text("keep")
            snapshot = source.capture_source_snapshot(project)
            self.addCleanup(source._force_rmtree, snapshot.container)
            destination = root / "durable"; self.addCleanup(source._force_rmtree, destination)
            saved = source.persist_source_snapshot(snapshot, destination)
            self.assertTrue(snapshot.container.exists())
            self.assertFalse((saved.root / "payload").samefile(snapshot.root / "payload"))

    def test_cross_volume_move_uses_independent_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root / "project"; project.mkdir()
            (project / "payload").write_text("keep")
            snapshot = source.capture_source_snapshot(project)
            self.addCleanup(source._force_rmtree, snapshot.container)
            destination = root / "durable"; self.addCleanup(source._force_rmtree, destination)
            with patch.object(source.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device")):
                saved = source.persist_source_snapshot(snapshot, destination, _consume=True)
            self.assertTrue(snapshot.container.exists())
            self.assertFalse((saved.root / "payload").samefile(snapshot.root / "payload"))

    def test_destination_local_orphan_is_reaped_without_touching_live_or_unrelated_data(self):
        import json
        import os
        import time
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp).resolve()
            orphan = parent / (source.SOURCE_SNAPSHOT_CONTAINER_PREFIX + "orphan")
            (orphan / "tree").mkdir(parents=True)
            (orphan / ".deploom-trial-owner.json").write_text(json.dumps({"pid":999999999}))
            (orphan / "tree/payload").write_text("abandoned")
            old = time.time() - 48 * 3600
            os.utime(orphan, (old, old))
            live = parent / (source.SOURCE_SNAPSHOT_CONTAINER_PREFIX + "live")
            live.mkdir()
            (live / ".deploom-trial-owner.json").write_text(json.dumps({"pid":os.getpid()}))
            os.utime(live, (old, old))
            unrelated = parent / "unrelated"; unrelated.mkdir()
            (unrelated / "payload").write_text("preserve")
            with patch("source_snapshot_storage.candidate_snapshot_parents", return_value=()):
                result = source.reap_orphaned_source_snapshots(additional_parents=(parent,))
            self.assertEqual(1, result["reaped"])
            self.assertFalse(orphan.exists())
            self.assertTrue(live.is_dir())
            self.assertEqual("preserve", (unrelated / "payload").read_text())

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); project = root / "project"; project.mkdir()
            (project / "payload").write_text("keep")
            snapshot = source.capture_source_snapshot(project)
            self.addCleanup(source._force_rmtree, snapshot.container)
            destination = root / "durable"; destination.mkdir()
            (destination / "unrelated").write_text("preserve")
            with self.assertRaisesRegex(source.SourceCaptureError, "DESTINATION_EXISTS"):
                source.persist_source_snapshot(snapshot, destination, _consume=True)
            self.assertTrue(snapshot.container.exists())
            self.assertEqual("preserve", (destination / "unrelated").read_text())


if __name__ == "__main__":
    unittest.main()
