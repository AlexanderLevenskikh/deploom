"""Physical regression coverage for prepared/trial directory stability."""
import concurrent.futures
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import artifact_integrity as integrity


class ArtifactDirectoryStabilityTests(unittest.TestCase):
    def during_hash(self, mutate, *, source_only=False):
        with tempfile.TemporaryDirectory() as raw:
            container = Path(raw)
            root = container / "tree"
            assets = root / ".artifacts" / "widget" / "assets"
            assets.mkdir(parents=True)
            file = assets / "widget.js"
            file.write_bytes(b"original widget")
            expected = integrity.build_artifact_tree_integrity(root, max_workers=1,
                source_only=source_only)
            original_hash = integrity._hash_file

            def changing(path, cancelled=None):
                result = original_hash(path, cancelled)
                mutate(container, root, assets, file)
                return result

            with mock.patch.object(integrity, "_hash_file", side_effect=changing):
                actual = integrity.build_artifact_tree_integrity(root, max_workers=1,
                    source_only=source_only)
            return expected.key, actual.key

    def test_directory_timestamp_noise_preserves_whole_tree_seal(self):
        def touch(_container, root, assets, _file):
            for directory in (root, assets):
                before = directory.stat()
                os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000_000))
        self.assertEqual(*self.during_hash(touch))

    def test_added_file_is_rejected_with_named_evidence(self):
        with self.assertRaisesRegex(integrity.ArtifactIntegrityError, r"CONTENT_UNSTABLE.*added=.*new.js"):
            self.during_hash(lambda _c, _r, assets, _f: (assets / "new.js").write_bytes(b"new"))

    def test_deleted_file_after_hash_is_rejected(self):
        with self.assertRaisesRegex(integrity.ArtifactIntegrityError, r"CONTENT_UNSTABLE.*removed=.*widget.js"):
            self.during_hash(lambda _c, _r, _a, file: file.unlink())

    def test_renamed_file_after_hash_is_rejected(self):
        with self.assertRaisesRegex(integrity.ArtifactIntegrityError, r"CONTENT_UNSTABLE.*added=.*renamed.js"):
            self.during_hash(lambda _c, _r, assets, file: file.rename(assets / "renamed.js"))

    def test_overwritten_file_after_hash_is_rejected(self):
        with self.assertRaisesRegex(integrity.ArtifactIntegrityError, r"CONTENT_UNSTABLE.*changed=.*widget.js"):
            self.during_hash(lambda _c, _r, _a, file: file.write_bytes(b"changed widget contents"))

    def test_replaced_file_with_same_bytes_and_mtime_is_rejected(self):
        def replace(container, _root, _assets, file):
            before = file.stat()
            replacement = container / "replacement.js"
            replacement.write_bytes(file.read_bytes())
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, file)
        with self.assertRaisesRegex(integrity.ArtifactIntegrityError, r"CONTENT_UNSTABLE.*changed=.*widget.js"):
            self.during_hash(replace)

    def test_replaced_directory_with_same_bytes_and_mtime_is_rejected(self):
        def replace(container, _root, assets, file):
            before = assets.stat()
            data = file.read_bytes()
            assets.rename(container / "retired")
            assets.mkdir()
            file.write_bytes(data)
            os.utime(assets, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(integrity.ArtifactIntegrityError, "CONTENT_UNSTABLE"):
            self.during_hash(replace)

    def test_external_hardlink_added_after_hash_is_rejected(self):
        def link(container, _root, _assets, file):
            try:
                os.link(file, container / "external-alias.js")
            except OSError:
                self.skipTest("hardlinks unavailable")
        with self.assertRaisesRegex(integrity.ArtifactIntegrityError, r"CONTENT_UNSTABLE.*changed=.*widget.js"):
            self.during_hash(link)

    def test_source_only_omits_dependency_membership_consistently(self):
        self.assertEqual(*self.during_hash(
            lambda _c, root, _a, _f: (root / "node_modules").mkdir(), source_only=True))

    def test_full_seal_rejects_added_dependencies(self):
        with self.assertRaisesRegex(integrity.ArtifactIntegrityError, r"CONTENT_UNSTABLE.*added=.*node_modules"):
            self.during_hash(lambda _c, root, _a, _f: (root / "node_modules").mkdir())

    def test_three_parallel_project_seals_remain_isolated(self):
        with tempfile.TemporaryDirectory() as raw:
            roots = [Path(raw) / str(index) for index in range(3)]
            for index, root in enumerate(roots):
                assets = root / ".artifacts" / "widget" / "assets"
                assets.mkdir(parents=True)
                (assets / "widget.js").write_text(str(index), encoding="utf-8")
            expected = [integrity.build_artifact_tree_integrity(root).key for root in roots]
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                actual = list(pool.map(integrity.build_artifact_tree_integrity, roots))
            self.assertEqual(expected, [item.key for item in actual])
            self.assertEqual(len(set(expected)), 3)


if __name__ == "__main__":
    unittest.main()
