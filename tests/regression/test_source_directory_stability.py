import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import source_snapshot as snapshot


class DirectoryStabilityTests(unittest.TestCase):
    def _during_hash(self, mutate):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'tree'
            directory = root / 'report' / 'Modal Edit Fixed Sum'
            directory.mkdir(parents=True)
            file = directory / 'image.txt'
            file.write_text('original', encoding='utf-8')
            expected = snapshot.build_source_tree_manifest(root)
            hash_file = snapshot._hash_regular_file
            def changed(path, **kwargs):
                result = hash_file(path, **kwargs)
                mutate(root, directory, file)
                return result
            with mock.patch.object(snapshot, '_hash_regular_file', side_effect=changed):
                actual = snapshot.build_source_tree_manifest(root)
            return expected.key, actual.key

    def test_directory_timestamp_churn_during_real_hash_does_not_change_content(self):
        def touch(_root, directory, _file):
            before = directory.stat()
            os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000_000))
        before, after = self._during_hash(touch)
        self.assertEqual(before, after)

    def test_addition_during_hash_is_rejected_with_named_evidence(self):
        with self.assertRaisesRegex(snapshot.SourceCaptureError, r"SOURCE_CAPTURE_UNSTABLE.*added=.*new.txt"):
            self._during_hash(lambda _root, directory, _file: (directory / 'new.txt').write_text('new'))

    def test_deletion_after_file_was_hashed_is_rejected(self):
        with self.assertRaisesRegex(snapshot.SourceCaptureError, r"SOURCE_CAPTURE_UNSTABLE.*removed=.*image.txt"):
            self._during_hash(lambda _root, _directory, file: file.unlink())

    def test_rename_after_file_was_hashed_is_rejected(self):
        with self.assertRaisesRegex(snapshot.SourceCaptureError, r"SOURCE_CAPTURE_UNSTABLE.*added=.*renamed.txt"):
            self._during_hash(lambda _root, directory, file: file.rename(directory / 'renamed.txt'))

    def test_file_mutation_after_its_hash_completed_is_rejected(self):
        with self.assertRaisesRegex(snapshot.SourceCaptureError, r"SOURCE_CAPTURE_UNSTABLE.*changed=.*image.txt"):
            self._during_hash(lambda _root, _directory, file: file.write_text('changed content'))

    def test_explicitly_excluded_output_churn_does_not_change_the_subject(self):
        def write_noise(root, _directory, _file):
            (root / '.dependency-roadmap').mkdir()
            (root / '.dependency-roadmap' / 'progress.log').write_text('progress')
        before, after = self._during_hash(write_noise)
        self.assertEqual(before, after)

    def test_directory_replacement_with_preserved_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / 'tree'
            directory.mkdir()
            (directory / 'same.txt').write_text('same')
            original = directory.stat()
            before = snapshot._directory_stability_stamp(directory)
            directory.rename(Path(raw) / 'old-tree')
            directory.mkdir()
            (directory / 'same.txt').write_text('same')
            os.utime(directory, ns=(original.st_atime_ns, original.st_mtime_ns))
            after = snapshot._directory_stability_stamp(directory)
            self.assertNotEqual(before, after)
            self.assertEqual('directory identity changed', snapshot._directory_stamp_change(before, after))

    def test_custom_policy_is_used_by_membership_guard(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'keep.txt').write_text('keep')
            policy = snapshot.SourceInputPolicy(excluded_file_names=('custom-output.txt',))
            before = snapshot._directory_stability_stamp(root, policy=policy)
            (root / 'custom-output.txt').write_text('output')
            self.assertEqual(before, snapshot._directory_stability_stamp(root, policy=policy))
            self.assertNotEqual(before, snapshot._directory_stability_stamp(root))


if __name__ == '__main__':
    unittest.main()
