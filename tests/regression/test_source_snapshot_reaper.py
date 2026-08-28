"""WS9: orphaned source snapshot containers must be reaped safely.

`_cleanup_all` only runs at a clean exit, so a crash, kill or watchdog stop
leaves the entire sealed tree behind. Sealed trees are write-protected, so a
plain rmtree would fail and make every orphan permanent.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import source_snapshot


class SourceSnapshotReaper(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_tmp = os.environ.get("TMP"), os.environ.get("TEMP")
        os.environ["TMP"] = os.environ["TEMP"] = self._tmp.name
        tempfile.tempdir = self._tmp.name

    def tearDown(self) -> None:
        tempfile.tempdir = None
        for name, value in zip(("TMP", "TEMP"), self._old_tmp):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def _orphan(self, *, age_seconds: float, readonly: bool = True) -> Path:
        container = Path(self._tmp.name) / (
            source_snapshot.SOURCE_SNAPSHOT_CONTAINER_PREFIX + f"x{age_seconds:.0f}"
        )
        (container / "tree").mkdir(parents=True)
        sealed = container / "tree" / "index.js"
        sealed.write_text("sealed\n", encoding="utf-8")
        if readonly:
            # Reproduce a real sealed container: a plain rmtree cannot remove it.
            source_snapshot._apply_tree_write_protection(container, readonly=True)
        old = time.time() - age_seconds
        os.utime(container, (old, old))
        return container

    def test_old_orphan_is_reaped_even_when_write_protected(self) -> None:
        container = self._orphan(age_seconds=48 * 3600)
        result = source_snapshot.reap_orphaned_source_snapshots()
        self.assertGreaterEqual(result["reaped"], 1)
        self.assertFalse(container.exists(), "write-protected orphan survived")

    def test_recent_container_is_not_reaped(self) -> None:
        container = self._orphan(age_seconds=5)
        source_snapshot.reap_orphaned_source_snapshots()
        self.assertTrue(container.is_dir(), "a fresh container was destroyed")

    def test_live_container_of_this_process_is_never_reaped(self) -> None:
        container = self._orphan(age_seconds=48 * 3600)
        with source_snapshot._LOCK:
            source_snapshot._ALL_CONTAINERS.add(container)
        try:
            source_snapshot.reap_orphaned_source_snapshots()
            self.assertTrue(container.is_dir(), "a live snapshot was reaped")
        finally:
            with source_snapshot._LOCK:
                source_snapshot._ALL_CONTAINERS.discard(container)

    def test_work_is_bounded_per_invocation(self) -> None:
        for index in range(12):
            self._orphan(age_seconds=48 * 3600 + index)
        result = source_snapshot.reap_orphaned_source_snapshots(max_containers=3)
        self.assertLessEqual(result["reaped"], 3, "reaper was not bounded")


if __name__ == "__main__":
    unittest.main()
