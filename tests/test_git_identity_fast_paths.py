from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import git_identity
import source_snapshot


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _write_files(root: Path, count: int, prefix: str = "f") -> None:
    for i in range(count):
        (root / f"{prefix}{i:04d}.txt").write_text(f"content-{i}\n", encoding="utf-8")


class SubjectLayoutCombinedProbeTests(unittest.TestCase):
    """The combined `rev-parse --show-toplevel HEAD` call must keep the exact
    semantics of the former two separate probes, and observe live changes."""

    def test_normal_repo_matches_head_and_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-b", "master")
            git(root, "config", "user.email", "layout@example.invalid")
            git(root, "config", "user.name", "Layout")
            (root / "package.json").write_text('{"name":"layout","private":true}\n', encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-m", "initial")
            expected_head = git(root, "rev-parse", "HEAD").stdout.strip()
            expected_root = Path(git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
            layout = source_snapshot._subject_layout(root)
            self.assertEqual(layout[0], expected_root)
            self.assertEqual(layout[2], expected_head)
            self.assertEqual(layout[1], Path("."))
            with mock.patch.object(source_snapshot, "_run_git", wraps=source_snapshot._run_git) as run:
                again = source_snapshot._subject_layout(root)
                run.assert_called_once()
            self.assertEqual(again, layout)

    def test_new_commit_refreshes_live_provenance_in_same_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-b", "master")
            git(root, "config", "user.email", "layout@example.invalid")
            git(root, "config", "user.name", "Layout")
            git(root, "commit", "--allow-empty", "-m", "first")
            before = source_snapshot.source_snapshot_provenance_head(root, require_git=True)
            git(root, "commit", "--allow-empty", "-m", "second")
            after = source_snapshot.source_snapshot_provenance_head(root, require_git=True)
            self.assertNotEqual(before, after)
            self.assertEqual(after, git(root, "rev-parse", "HEAD").stdout.strip())

    def test_empty_repo_raises_head_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-b", "master")
            with self.assertRaises(source_snapshot.SourceCaptureError) as manager:
                source_snapshot._subject_layout(root, require_git=True)
            self.assertIn("SOURCE_GIT_HEAD_UNAVAILABLE", str(manager.exception))
            with self.assertRaises(source_snapshot.SourceCaptureError) as manager:
                source_snapshot._subject_layout(root, require_git=False)
            self.assertIn("SOURCE_GIT_HEAD_UNAVAILABLE", str(manager.exception))

    def test_non_repo_falls_back_or_requires_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = source_snapshot._subject_layout(root, require_git=False)
            self.assertEqual(layout, (root.resolve(), Path("."), ""))
            with self.assertRaises(source_snapshot.SourceCaptureError) as manager:
                source_snapshot._subject_layout(root, require_git=True)
            self.assertIn("SOURCE_GIT_REQUIRED", str(manager.exception))

    def test_nested_project_relative_to_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-b", "master")
            git(root, "config", "user.email", "layout@example.invalid")
            git(root, "config", "user.name", "Layout")
            (root / "package.json").write_text('{"name":"layout","private":true}\n', encoding="utf-8")
            nested = root / "packages" / "app"
            nested.mkdir(parents=True)
            git(root, "add", ".")
            git(root, "commit", "-m", "initial")
            layout = source_snapshot._subject_layout(nested)
            self.assertEqual(layout[0], root.resolve())
            self.assertEqual(layout[1], Path("packages/app"))
            expected = git(root, "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(layout[2], expected)


class GitIdentityMemoizationTests(unittest.TestCase):
    """Live toplevel probes must observe repository and environment changes."""

    def test_toplevel_refreshed_across_callers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-b", "master")
            git(root, "config", "user.email", "gid@example.invalid")
            git(root, "config", "user.name", "Gid")
            (root / "package.json").write_text('{"name":"gid","private":true}\n', encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-m", "initial")
            self.assertEqual(git_identity.git_toplevel(root), root.resolve())
            with mock.patch.object(git_identity.subprocess, "run", wraps=git_identity.subprocess.run) as run:
                self.assertEqual(git_identity.git_toplevel(root), root.resolve())
                run.assert_called_once()

    def test_non_repo_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(git_identity.git_toplevel(Path(tmp)))

    def test_repository_created_after_negative_probe_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(git_identity.git_toplevel(root))
            git(root, "init", "-b", "master")
            self.assertEqual(git_identity.git_toplevel(root), root.resolve())

    def test_infrastructure_failure_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-b", "master")
            with mock.patch.object(git_identity.subprocess, "run", side_effect=OSError("unavailable")):
                self.assertIsNone(git_identity.git_toplevel(root))
            self.assertEqual(git_identity.git_toplevel(root), root.resolve())

    def test_marker_semantics_match_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-b", "master")
            git(root, "config", "user.email", "gid@example.invalid")
            git(root, "config", "user.name", "Gid")
            (root / "x").write_text("x\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-m", "x")
            nested = root / "sub"
            nested.mkdir()
            out, rc, err, marker = git_identity.git_toplevel_raw(nested)
            self.assertEqual(rc, 0)
            self.assertEqual(Path(out.strip()).resolve(), root.resolve())
            self.assertTrue(marker)


class SourceManifestFastPathEquivalenceTests(unittest.TestCase):
    """The synchronous small-tree hashing path must produce byte-identical
    manifests to the streaming ThreadPoolExecutor path, including trees that
    overflow the buffered threshold and force the executor flush."""

    def _manifest_for(self, root: Path, threshold: int) -> source_snapshot.SourceTreeManifest:
        with mock.patch.object(source_snapshot, "_SYNC_HASH_THRESHOLD", threshold):
            return source_snapshot.build_source_tree_manifest(root, timeout_seconds=60)

    def test_small_tree_matches_across_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_files(root, 5)
            (root / "sub").mkdir()
            _write_files(root / "sub", 3, prefix="s")
            sync = self._manifest_for(root, threshold=10**9)
            exec_only = self._manifest_for(root, threshold=0)
            self.assertEqual(sync, exec_only)
            self.assertEqual(sync.file_count, 8)

    def test_overflow_tree_flush_matches_all_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_files(root, 300)
            flushed = self._manifest_for(root, threshold=source_snapshot._SYNC_HASH_THRESHOLD)
            all_sync = self._manifest_for(root, threshold=10**9)
            all_exec = self._manifest_for(root, threshold=0)
            self.assertEqual(flushed, all_sync)
            self.assertEqual(flushed, all_exec)
            self.assertEqual(flushed.file_count, 300)

    def test_deterministic_order_across_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_files(root, 12)
            sync = self._manifest_for(root, threshold=10**9)
            exec_only = self._manifest_for(root, threshold=0)
            self.assertEqual(
                [entry["path"] for entry in sync.entries if entry["kind"] == "file"],
                [entry["path"] for entry in exec_only.entries if entry["kind"] == "file"],
            )


if __name__ == "__main__":
    unittest.main()
