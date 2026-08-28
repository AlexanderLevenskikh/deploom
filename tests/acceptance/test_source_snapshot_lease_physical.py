"""Physical adversarial tests for the SourceSnapshot watcher fast path.

These use real filesystem operations, not mocked watchers. They pin two
properties of the memo hit:

  * a watched directory that is swapped for a different directory object at the
    same textual path must NOT keep validating under the old key, even though
    `is_dir()` still succeeds and the watcher (holding a handle to the original
    object) never fires;
  * watcher uncertainty -- a benign attribute-only touch, an overflow, a dead
    watcher thread -- must never be reported as a content mismatch and must
    never be reported as a pass. It must fall back to authoritative validation.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import source_snapshot
from source_snapshot import (
    SourceCaptureError,
    validate_source_snapshot,
)


def _project(root: Path) -> Path:
    project = root / "project"
    project.mkdir(parents=True)
    (project / "package.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
    (project / "index.js").write_text("export const sealed = 1;\n", encoding="utf-8")
    return project


class SourceSnapshotLeasePhysicalAcceptance(unittest.TestCase):
    def _snapshot(self, project: Path):
        # The memo/watcher fast path only exists for the ACTIVE epoch, so the
        # snapshot must be registered -- otherwise these tests are vacuous.
        snapshot = source_snapshot.activate_source_snapshot_epoch(
            project, replace=True
        )
        # Arm the watcher / establish the memo.
        validate_source_snapshot(snapshot)
        return snapshot

    def tearDown(self) -> None:
        source_snapshot.clear_source_snapshot_epochs()

    def test_cold_and_hot_validation_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = _project(Path(raw))
            snapshot = self._snapshot(project)
            self.assertIs(validate_source_snapshot(snapshot), snapshot)

    def test_real_content_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = _project(Path(raw))
            snapshot = self._snapshot(project)
            target = snapshot.root
            victim = next(target.rglob("index.js"))
            victim.write_text("export const sealed = 999;\n", encoding="utf-8")
            with self.assertRaises(SourceCaptureError):
                validate_source_snapshot(snapshot)

    def test_attribute_only_touch_is_not_a_content_mismatch(self) -> None:
        """Benign metadata change: identical bytes must not become a FAIL."""
        with tempfile.TemporaryDirectory() as raw:
            project = _project(Path(raw))
            snapshot = self._snapshot(project)
            victim = next(snapshot.root.rglob("index.js"))
            os.utime(victim, (0, 0))
            # Either the lease survives, or authoritative revalidation runs and
            # passes. What must never happen is a content-mismatch verdict.
            self.assertIs(validate_source_snapshot(snapshot), snapshot)

    def test_watcher_failure_does_not_fabricate_a_content_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = _project(Path(raw))
            snapshot = self._snapshot(project)
            key = source_snapshot._active_key(snapshot.original_project_path)
            entry = source_snapshot._ACTIVE_WATCHERS.get(key)
            if entry is None:
                self.skipTest("no active watcher on this platform")
            # Simulate overflow / thread failure: the watcher is unusable.
            entry[1].errors.append("SIMULATED_WATCHER_OVERFLOW")
            # Content is untouched, so authoritative fallback must PASS.
            self.assertIs(validate_source_snapshot(snapshot), snapshot)

    @unittest.skipUnless(os.name == "nt", "Windows directory-swap acceptance")
    def test_directory_swap_at_same_path_must_not_pass_under_old_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = _project(root)
            snapshot = self._snapshot(project)
            watched = snapshot.root
            key = source_snapshot._active_key(snapshot.original_project_path)
            if source_snapshot._ACTIVE_WATCHERS.get(key) is None:
                self.skipTest("no active watcher on this platform")

            # Build a hostile replacement carrying different bytes.
            evil = root / "evil"
            evil.mkdir()
            for item in watched.rglob("*"):
                if item.is_file():
                    relative = item.relative_to(watched)
                    (evil / relative).parent.mkdir(parents=True, exist_ok=True)
                    (evil / relative).write_text("swapped\n", encoding="utf-8")

            retired = watched.parent / (watched.name + ".retired")
            try:
                os.rename(watched, retired)
                os.rename(evil, watched)
            except OSError:
                self.skipTest("directory swap not permitted in this environment")

            self.assertTrue(watched.is_dir(), "attack precondition: path still valid")
            with self.assertRaises(SourceCaptureError):
                validate_source_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
