"""A vanished observed input discards a capture, never source coverage."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import source_snapshot as source


class MissingSnapshotInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(source._force_rmtree, Path(self.tmp.name))
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.project / "package.json").write_text('{"name":"snapshot-fixture","version":"1.0.0"}')
        self.relative = Path("src/Model/Tasks/bin/Debug/net7.0/Model.pdb")
        original = self.project / self.relative
        original.parent.mkdir(parents=True)
        original.write_bytes(b"physical source fixture\x00\x01")
        self.destination = Path(self.tmp.name) / "durable"

    def vanished_hash(self, *, always=False):
        real_hash = source._hash_regular_file
        lost = []
        def hash_file(path, **kwargs):
            if path.name == "Model.pdb" and not path.is_relative_to(self.project) and (always or not lost):
                lost.append(path)
                os.chmod(path, path.stat().st_mode | stat.S_IWRITE)
                path.unlink()
            return real_hash(path, **kwargs)
        return hash_file, lost

    def test_real_private_file_loss_recaptures_and_keeps_ignored_generated_input(self):
        # Exercise the bounded parallel hashing path, not a fake verifier.
        for index in range(300):
            (self.project / f"input-{index:03}.js").write_text(str(index))
        (self.project / ".gitignore").write_text("bin/\n")
        hash_file, lost = self.vanished_hash()
        messages = []
        with patch.object(source, "_hash_regular_file", side_effect=hash_file):
            sealed = source.capture_durable_source_snapshot(self.project, self.destination, progress=messages.append)
        self.assertEqual(len(lost), 1)
        abandoned = next(parent for parent in lost[0].parents if parent.name.startswith(source.SOURCE_SNAPSHOT_CONTAINER_PREFIX))
        self.assertFalse(abandoned.exists())
        self.assertEqual((sealed.root / self.relative).read_bytes(), (self.project / self.relative).read_bytes())
        self.assertTrue(any("attempt 2/3" in item for item in messages))
        self.assertEqual(source.build_source_tree_manifest(self.project).key, sealed.manifest_key)
        self.assertEqual(source.build_source_tree_manifest(sealed.root).key, sealed.manifest_key)
        self.assertFalse(lost[0].exists())

    def test_repeated_loss_is_bounded_and_never_publishes_durable_snapshot(self):
        hash_file, lost = self.vanished_hash(always=True)
        with patch.object(source, "_hash_regular_file", side_effect=hash_file):
            with self.assertRaisesRegex(source.SourceCaptureError, "SOURCE_CAPTURE_UNSTABLE.*file disappeared"):
                source.capture_durable_source_snapshot(self.project, self.destination)
        self.assertEqual(len(lost), source.SOURCE_CAPTURE_RETRIES)
        self.assertFalse(self.destination.exists())
        self.assertTrue((self.project / self.relative).is_file())

    def test_permission_failure_retains_unreadable_classification(self):
        with patch.object(Path, "open", side_effect=PermissionError("fixture denied")):
            with self.assertRaisesRegex(source.SourceCaptureError, "^SOURCE_FILE_UNREADABLE"):
                source._hash_regular_file(self.project / self.relative)


if __name__ == "__main__":
    unittest.main()
