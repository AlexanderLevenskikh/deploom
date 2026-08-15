from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dependency_live_roadmap_generator import BaselineProgressReporter


class BaselineProgressReporterTests(unittest.TestCase):
    def test_progress_checkpoint_is_atomic_and_keeps_latest_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".dependency-roadmap" / "state" / "baseline-verification-progress.json"
            reporter = BaselineProgressReporter(path)
            reporter.emit("Demo", "yellow", "localization-started", checksStarted=0)
            reporter.emit("Demo", "yellow", "localization-heartbeat", checksStarted=4, active=4)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("Demo", payload["project"])
            self.assertEqual("yellow", payload["mode"])
            self.assertEqual("localization-heartbeat", payload["phase"])
            self.assertEqual(4, payload["checksStarted"])
            self.assertEqual(4, payload["active"])
            self.assertIn("updatedAt", payload)
            self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())

    def test_parallel_progress_writes_do_not_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "progress.json"
            reporter = BaselineProgressReporter(path)
            threads = [
                threading.Thread(target=lambda i=i: reporter.emit("Demo", "yellow", "check", check=i))
                for i in range(12)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("check", payload["phase"])
            self.assertIsInstance(payload["check"], int)

    def test_terminal_progress_is_not_overwritten_by_late_worker_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "progress.json"
            reporter = BaselineProgressReporter(path)
            reporter.emit("Demo", "yellow", "localization-timeout", error="watchdog")
            reporter.emit("Demo", "yellow", "localization-check-running", check=9)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("localization-timeout", payload["phase"])
            self.assertEqual("watchdog", payload["error"])

        # A fresh solve explicitly resets terminal protection.
        reporter = BaselineProgressReporter(None)
        reporter.emit("Demo", "yellow", "localization-timeout")
        reporter.emit("Demo", "yellow", "solve-and-verify-started")
        self.assertFalse(reporter._terminal)


if __name__ == "__main__":
    unittest.main()
