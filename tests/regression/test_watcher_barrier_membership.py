"""Watcher barriers cannot change sealed-source membership or remove user state."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from source_snapshot import _drain_watcher

class WatcherBarrierMembershipTests(unittest.TestCase):
    def fixture_watcher(self, root):
        class Watcher:
            errors = []
            @property
            def events(self):
                return [(1, path.relative_to(root).as_posix()) for path in root.rglob('.drain-*')]
        return Watcher()

    def test_created_marker_directory_is_removed_after_delivery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertTrue(_drain_watcher(self.fixture_watcher(root), root))
            self.assertEqual([], list(root.iterdir()))

    def test_existing_directory_and_user_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); state = root / '.dependency-roadmap'; state.mkdir()
            (state / 'user.json').write_text('preserve')
            self.assertTrue(_drain_watcher(self.fixture_watcher(root), root))
            self.assertEqual(['user.json'], [p.name for p in state.iterdir()])

    def test_failed_marker_write_removes_only_created_empty_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with patch.object(Path, 'write_text', side_effect=OSError('fixture')):
                self.assertFalse(_drain_watcher(self.fixture_watcher(root), root))
            self.assertEqual([], list(root.iterdir()))

if __name__ == '__main__': unittest.main()
