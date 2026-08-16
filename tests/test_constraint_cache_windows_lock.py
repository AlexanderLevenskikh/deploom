from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import constraint_cache


class ConstraintCacheWindowsLockTests(unittest.TestCase):
    def test_permission_error_for_existing_lock_is_contention(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache.json"
            lock_path = root / "cache.json.lock"
            lock_path.write_text("held\n", encoding="utf-8")

            real_open = constraint_cache.os.open
            attempts = 0

            def windows_open(path, flags):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError(
                        13,
                        "simulated Windows sharing violation",
                        path,
                    )
                return real_open(path, flags)

            def release_owner(_seconds):
                lock_path.unlink(missing_ok=True)

            with mock.patch.object(
                constraint_cache.os,
                "open",
                side_effect=windows_open,
            ), mock.patch.object(
                constraint_cache.time,
                "sleep",
                side_effect=release_owner,
            ):
                with constraint_cache._exclusive_cache_write_lock(cache):
                    self.assertTrue(lock_path.exists())

            self.assertGreaterEqual(attempts, 2)
            self.assertFalse(lock_path.exists())

    def test_permission_error_without_lock_is_not_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "cache.json"
            with mock.patch.object(
                constraint_cache.os,
                "open",
                side_effect=PermissionError(
                    13,
                    "real permission failure",
                    str(cache) + ".lock",
                ),
            ):
                with self.assertRaises(PermissionError):
                    with constraint_cache._exclusive_cache_write_lock(cache):
                        pass


if __name__ == "__main__":
    unittest.main()
