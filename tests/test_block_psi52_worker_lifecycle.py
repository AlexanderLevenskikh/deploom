from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier
import worker_runtime_state


class Psi52ConfigTests(unittest.TestCase):
    def test_hot_continuity_uses_existing_timeout_field(self) -> None:
        config = verifier.BaselineVerifyConfig()
        self.assertTrue(hasattr(config, "snapshot_copy_timeout_seconds"))
        self.assertFalse(hasattr(config, "hard_timeout_seconds"))
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertNotIn("config.hard_timeout_seconds", source)


class Psi52WorkerTaintTests(unittest.TestCase):
    def tearDown(self) -> None:
        worker_runtime_state.reset_worker_reuse_state()

    def test_taint_is_sticky_until_request_boundary_reset(self) -> None:
        self.assertEqual("", worker_runtime_state.worker_reuse_unsafe_reason())
        worker_runtime_state.mark_worker_reuse_unsafe("first")
        worker_runtime_state.mark_worker_reuse_unsafe("second")
        self.assertEqual("first", worker_runtime_state.worker_reuse_unsafe_reason())
        worker_runtime_state.reset_worker_reuse_state()
        self.assertEqual("", worker_runtime_state.worker_reuse_unsafe_reason())


class Psi52StaticLifecycleTests(unittest.TestCase):
    def test_source_events_trigger_strong_revalidation(self) -> None:
        source = (ROOT / "source_snapshot.py").read_text(encoding="utf-8")
        self.assertIn("watcher-event-strong-revalidation", source)
        self.assertIn("relevant_events = _content_relevant_events", source)

    def test_desktop_owns_protocol_and_idle_retirement(self) -> None:
        source = (ROOT / "desktop" / "electron" / "baseline-worker.ts").read_text(encoding="utf-8")
        self.assertIn("BASELINE_WORKER_RETIRING", source)
        self.assertIn("record.child.stdin.end()", source)
        self.assertIn("this.terminateTree(record.child)", source)
        self.assertIn("gracefulShutdownTimeoutMs", source)

        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("killProcessTree,", main)


if __name__ == "__main__":
    unittest.main()
