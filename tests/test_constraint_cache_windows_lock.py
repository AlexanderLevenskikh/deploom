from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import constraint_cache as cache_module


class ConstraintCacheWindowsLockTests(unittest.TestCase):
    def test_transient_permission_error_without_visible_lock_is_retried(self):
        """A Windows sharing race must be retried, not misclassified as ACL failure."""
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "cache.json"
            original_open = cache_module.os.open
            calls = {"count": 0}

            def transient_open(path, flags, *args, **kwargs):
                if str(path).endswith("cache.json.lock") and calls["count"] == 0:
                    calls["count"] += 1
                    raise PermissionError(
                        13,
                        "simulated transient Windows CREATE_NEW sharing race",
                        str(path),
                    )
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                cache_module.os,
                "open",
                side_effect=transient_open,
            ):
                with cache_module._exclusive_cache_write_lock(cache):
                    pass

            self.assertEqual(1, calls["count"])
            self.assertFalse(cache.with_name("cache.json.lock").exists())

    def test_persistent_permission_error_still_fails_closed(self):
        """The grace retry must not turn a real permissions failure into success."""
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "cache.json"

            def denied(path, flags, *args, **kwargs):
                raise PermissionError(
                    13,
                    "simulated persistent ACL failure",
                    str(path),
                )

            with mock.patch.object(cache_module.os, "open", side_effect=denied):
                with self.assertRaises(PermissionError):
                    with cache_module._exclusive_cache_write_lock(cache):
                        pass


if __name__ == "__main__":
    unittest.main()
