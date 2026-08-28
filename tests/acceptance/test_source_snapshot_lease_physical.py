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
from unittest.mock import patch

import source_snapshot
from source_snapshot import (
    SourceCaptureError,
    validate_source_snapshot,
)


def _defeat_write_protection(path: Path) -> None:
    """Model an adversary who first clears the sealed tree's write protection.
    Content detection must not depend on that protection holding."""
    import stat as _stat
    try:
        os.chmod(path, os.stat(path).st_mode | _stat.S_IWRITE)
    except OSError:
        pass


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
            _defeat_write_protection(victim)
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


    def test_mutation_consumed_before_notification_must_not_pass(self) -> None:
        """P0-A(C): consume immediately after mutating, with no wait for the
        watcher event. Silence must not be read as proof of an unchanged tree."""
        with tempfile.TemporaryDirectory() as raw:
            project = _project(Path(raw))
            snapshot = self._snapshot(project)
            victim = next(snapshot.root.rglob("index.js"))
            _defeat_write_protection(victim)
            victim.write_text("export const sealed = 666;\n", encoding="utf-8")
            # Deliberately no sleep and no event wait.
            with self.assertRaises(SourceCaptureError):
                validate_source_snapshot(snapshot)

    def test_hardlink_alias_cannot_rewrite_sealed_bytes(self) -> None:
        """P0-A(B): protection applies through aliases, and a real mutation
        through an external alias must invalidate the sealed snapshot."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = _project(root)
            snapshot = self._snapshot(project)
            victim = next(snapshot.root.rglob("index.js"))
            original = victim.read_text(encoding="utf-8")
            alias = root / "alias.js"
            try:
                os.link(victim, alias)
            except OSError:
                self.skipTest("hardlinks unavailable in this environment")

            with self.assertRaises(OSError):
                with open(alias, "w", encoding="utf-8") as handle:
                    handle.write("EVIL\n")
            self.assertEqual(victim.read_text(encoding="utf-8"), original)

            # Creating an alias outside the watched tree is not a content
            # notification. This test pins the seal guarantee only: ordinary
            # writes through every alias remain blocked and bytes stay intact.
            # Link-topology detection belongs to the authoritative-path test
            # below, where it is exercised explicitly on every platform.

    def test_authoritative_validation_rejects_hardlink_topology(self) -> None:
        """The full manifest path must reject an external alias even when no
        content write occurred and therefore no watcher event exists."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = _project(root)
            snapshot = self._snapshot(project)
            victim = next(snapshot.root.rglob("index.js"))
            alias = root / "alias.js"
            try:
                os.link(victim, alias)
            except OSError:
                self.skipTest("hardlinks unavailable in this environment")

            # Force the platform-independent authoritative path. Creating a
            # link outside the watched tree is not itself a content event.
            source_snapshot._retire_snapshot_watcher(snapshot)
            with self.assertRaisesRegex(
                SourceCaptureError,
                "SOURCE_(?:SNAPSHOT_LINK_TOPOLOGY_VIOLATION|HARDLINK_UNSUPPORTED)",
            ):
                validate_source_snapshot(snapshot)

    @unittest.skipUnless(os.name == "nt", "the watcher memo fast path is Windows-only")
    def test_hot_validation_does_not_traverse_the_whole_tree(self) -> None:
        """A proof-grade lease on an unchanged tree must stay O(1)."""
        with tempfile.TemporaryDirectory() as raw:
            project = _project(Path(raw))
            for index in range(200):
                (project / f"file{index}.js").write_text(
                    f"export const n{index} = {index};\n", encoding="utf-8"
                )
            snapshot = self._snapshot(project)
            calls: list[str] = []
            real = source_snapshot.build_source_tree_manifest

            def counting(root, **kwargs):
                calls.append(str(kwargs.get("pass_role") or ""))
                return real(root, **kwargs)

            with patch.object(source_snapshot, "build_source_tree_manifest", counting):
                self.assertIs(validate_source_snapshot(snapshot), snapshot)
            self.assertEqual(
                calls, [], "hot lease performed a whole-tree content traversal"
            )


if __name__ == "__main__":
    unittest.main()
