"""Fast private filtered capture, with identical exclusion and isolation semantics."""
from __future__ import annotations
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from verification_workspace_backend import materialize_private_tree


class FilteredReflinkTests(unittest.TestCase):
    def _fixture(self, raw):
        root = Path(raw) / 'source'
        root.mkdir()
        (root / 'package.json').write_text('{"name":"fixture"}', encoding='utf-8')
        (root / 'source.bin').write_bytes(b'X' * 262144)
        (root / 'node_modules').mkdir()
        (root / 'node_modules' / 'do-not-copy').write_bytes(b'not source')
        (root / 'excluded.txt').write_bytes(b'dont copy')
        return root

    def test_excluded_source_and_dependency_tree_never_in_trial(self):
        with tempfile.TemporaryDirectory() as raw:
            root = self._fixture(raw)
            target = Path(raw) / 'private'
            method = materialize_private_tree(root, target, timeout_seconds=30,
                exclude_dir_names=('node_modules',), exclude_relative_paths=('excluded.txt',))
            self.assertFalse((target / 'node_modules').exists())
            self.assertFalse((target / 'excluded.txt').exists())
            self.assertEqual((root / 'source.bin').read_bytes(), (target / 'source.bin').read_bytes())
            (target / 'source.bin').write_bytes(b'Y')
            self.assertEqual(b'X', (root / 'source.bin').read_bytes()[:1])
            self.assertIn(method, {
                'portable-scoped-private-copy', 'linux-scoped-file-reflink',
            })

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux FICLONE only')
    def test_unsupported_reflink_falls_back_to_independent_bytes(self):
        from verification_workspace_backend import _filtered_reflink_copy_function
        with tempfile.TemporaryDirectory() as raw:
            root = self._fixture(raw)
            target = Path(raw) / 'clone.bin'
            import fcntl
            with patch.object(fcntl, 'ioctl', side_effect=OSError(errno.EOPNOTSUPP, 'fixture')):
                stats = {}
                _filtered_reflink_copy_function(stats)(str(root / 'source.bin'), str(target))
            self.assertEqual(1, stats['fallback'])
            self.assertEqual(1, stats['unsupported'])
            target.write_bytes(b'changed')
            self.assertEqual(262144, (root / 'source.bin').stat().st_size)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux FICLONE only')
    def test_existing_destination_is_never_deleted(self):
        from verification_workspace_backend import _filtered_reflink_copy_function
        with tempfile.TemporaryDirectory() as raw:
            root = self._fixture(raw)
            target = Path(raw) / 'existing.bin'
            target.write_bytes(b'preserve existing bytes')
            with self.assertRaises(FileExistsError):
                _filtered_reflink_copy_function({})(str(root / 'source.bin'), str(target))
            self.assertEqual(b'preserve existing bytes', target.read_bytes())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux FICLONE only')
    def test_unexpected_clone_error_does_not_silently_fallback(self):
        from verification_workspace_backend import _filtered_reflink_copy_function
        with tempfile.TemporaryDirectory() as raw:
            root = self._fixture(raw)
            target = Path(raw) / 'clone.bin'
            import fcntl
            with patch.object(fcntl, 'ioctl', side_effect=OSError(errno.EIO, 'device fault')):
                with self.assertRaises(OSError):
                    _filtered_reflink_copy_function({})(str(root / 'source.bin'), str(target))
            self.assertFalse(target.exists())


if __name__ == '__main__': unittest.main()
