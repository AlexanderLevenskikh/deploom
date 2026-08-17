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
        self.assertIn("_notification_is_authoritative_mutation(", source)
        self.assertIn("FILE_ACTION_MODIFIED", source)

    def test_directory_modified_is_non_authoritative_but_missing_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dir").mkdir()
            self.assertFalse(
                fastpath._notification_is_authoritative_mutation(
                    root,
                    fastpath._DirectoryWatcher.FILE_ACTION_MODIFIED,
                    "dir",
                )
            )
            self.assertTrue(
                fastpath._notification_is_authoritative_mutation(
                    root,
                    fastpath._DirectoryWatcher.FILE_ACTION_MODIFIED,
                    "missing",
                )
            )


if __name__ == "__main__":
    unittest.main()
