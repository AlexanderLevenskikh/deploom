from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import source_snapshot


class FakeWatcher:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


class Psi532LiveWatcherCleanupTests(unittest.TestCase):
    def tearDown(self) -> None:
        with source_snapshot._LOCK:
            source_snapshot._ACTIVE.clear()
            source_snapshot._ACTIVE_WATCHERS.clear()
            source_snapshot._LIVE_SOURCE_WATCHERS.clear()

    def test_clear_retires_active_and_live_watchers(self) -> None:
        key = source_snapshot._active_key(ROOT)
        active = FakeWatcher()
        live = FakeWatcher()
        marker = object()
        with source_snapshot._LOCK:
            source_snapshot._ACTIVE_WATCHERS[key] = (marker, active, None)
            source_snapshot._LIVE_SOURCE_WATCHERS[key] = (
                marker, live, None, None
            )

        source_snapshot.clear_source_snapshot_epochs()

        self.assertEqual(1, active.stops)
        self.assertEqual(1, live.stops)
        with source_snapshot._LOCK:
            self.assertNotIn(key, source_snapshot._ACTIVE_WATCHERS)
            self.assertNotIn(key, source_snapshot._LIVE_SOURCE_WATCHERS)

    def test_replace_retires_active_and_live_watchers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deploom-psi532-") as tmp:
            project = Path(tmp).resolve()
            key = source_snapshot._active_key(project)
            old = SimpleNamespace(
                original_project_path=project,
                container=project / "old-container",
            )
            new = SimpleNamespace(
                original_project_path=project,
                container=project / "new-container",
            )
            active = FakeWatcher()
            live = FakeWatcher()
            with source_snapshot._LOCK:
                source_snapshot._ACTIVE[key] = old
                source_snapshot._ACTIVE_WATCHERS[key] = (old, active, None)
                source_snapshot._LIVE_SOURCE_WATCHERS[key] = (
                    old, live, None, None
                )

            with patch.object(
                source_snapshot,
                "capture_source_snapshot",
                return_value=new,
            ), patch.object(source_snapshot, "_force_rmtree"):
                observed = source_snapshot.activate_source_snapshot_epoch(
                    project,
                    replace=True,
                )

            self.assertIs(observed, new)
            self.assertEqual(1, active.stops)
            self.assertEqual(1, live.stops)


class Psi532WorkerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = (
            ROOT / "desktop" / "electron" / "baseline-worker.ts"
        ).read_text(encoding="utf-8")
        cls.harness = (
            ROOT / "desktop" / "scripts" /
            "check-baseline-worker-hardening.mjs"
        ).read_text(encoding="utf-8")

    def test_worker_routes_all_stdin_failures_to_retirement(self) -> None:
        self.assertIn("child.stdin.on('error'", self.worker)
        self.assertIn("BASELINE_WORKER_STDIN_FAILED", self.worker)
        self.assertIn("retire?:", self.worker)
        self.assertIn("record.retire = beginRetirement", self.worker)
        self.assertIn("stdin.write(", self.worker)
        self.assertIn("retireWriteFailure", self.worker)
        self.assertIn("catch (error)", self.worker)

    def test_harness_is_deterministic_and_not_temp_cwd_dependent(self) -> None:
        self.assertIn("const workerCwd = process.cwd()", self.harness)
        self.assertNotIn("cwd: root,", self.harness)
        self.assertIn("stdinStreamErrorRetiresWorker", self.harness)
        self.assertIn("stdinWriteCallbackFailureRetiresWorker", self.harness)
        self.assertIn("stdinWriteThrowRetiresWorker", self.harness)
        self.assertNotIn("sys.stdin.close()", self.harness)


if __name__ == "__main__":
    unittest.main()
