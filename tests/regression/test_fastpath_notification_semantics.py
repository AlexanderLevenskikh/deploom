from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import prepared_workspace_fastpath as fastpath


class FastPathNotificationSemanticsRegressionTests(unittest.TestCase):
    def test_windows_notification_action_is_part_of_guard_authority(self) -> None:
        source = Path(fastpath.__file__).read_text(encoding="utf-8")
        self.assertIn('action = int.from_bytes(raw[offset + 4:offset + 8], "little")', source)
        self.assertIn("_classify_integrity_notification(", source)
        self.assertIn("build_dependency_integrity_manifest(", source)
        self.assertIn("FILE_ACTION_MODIFIED", source)

    def test_only_root_verification_caches_are_ephemeral(self) -> None:
        self.assertTrue(fastpath._is_ephemeral_change(".vite/deps/chunk.js"))
        self.assertTrue(fastpath._is_ephemeral_change(".vitest/results.json"))
        self.assertTrue(fastpath._is_ephemeral_change(".cache/eslint/result"))
        self.assertFalse(fastpath._is_ephemeral_change("package/.cache/result"))
        self.assertFalse(fastpath._is_ephemeral_change("package/.vite/chunk.js"))

    def test_integrity_classifier_ignores_directories_but_fails_closed_for_missing_or_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dir").mkdir()
            package = root / "pkg.js"
            package.write_text("sealed\n", encoding="utf-8")
            manifest = {
                fastpath._integrity_key(package, root): fastpath._fingerprint_path(package)
            }
            self.assertEqual(
                "ignored",
                fastpath._classify_integrity_notification(
                    root, root,
                    fastpath._DirectoryWatcher.FILE_ACTION_MODIFIED,
                    "dir", manifest,
                ),
            )
            self.assertEqual(
                "mutation",
                fastpath._classify_integrity_notification(
                    root, root,
                    fastpath._DirectoryWatcher.FILE_ACTION_MODIFIED,
                    "missing", manifest,
                ),
            )
            self.assertEqual(
                "notification-only",
                fastpath._classify_integrity_notification(
                    root, root,
                    fastpath._DirectoryWatcher.FILE_ACTION_MODIFIED,
                    "pkg.js", manifest,
                ),
            )
            package.write_text("mutated\n", encoding="utf-8")
            self.assertEqual(
                "mutation",
                fastpath._classify_integrity_notification(
                    root, root,
                    fastpath._DirectoryWatcher.FILE_ACTION_MODIFIED,
                    "pkg.js", manifest,
                ),
            )


if __name__ == "__main__":
    unittest.main()
