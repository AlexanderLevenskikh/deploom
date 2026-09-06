from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class SourceSnapshotGitAdminChurnTests(unittest.TestCase):
    def tearDown(self) -> None:
        source_snapshot.clear_source_snapshot_epochs()

    def _repo(self, root: Path) -> None:
        git(root, "init", "-b", "master")
        git(root, "config", "user.email", "source-churn@example.invalid")
        git(root, "config", "user.name", "Source Churn")
        (root / "package.json").write_text('{"name":"source-churn","private":true}\n', encoding="utf-8")
        (root / "src.txt").write_text("stable\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-m", "initial")

    def test_git_lock_churn_is_administrative_but_persistent_entries_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            git_dir = root / ".git"
            before = source_snapshot._directory_stability_stamp(git_dir, relative=Path(".git"))
            lock = git_dir / "index.lock"
            lock.write_text("transient", encoding="utf-8")
            during_lock = source_snapshot._directory_stability_stamp(git_dir, relative=Path(".git"))
            self.assertEqual(before, during_lock)
            lock.unlink()
            persistent = git_dir / "deploom-persistent-probe"
            persistent.write_text("semantic", encoding="utf-8")
            after = source_snapshot._directory_stability_stamp(git_dir, relative=Path(".git"))
            self.assertNotEqual(before, after)

    def test_captured_snapshot_never_inherits_live_git_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root)
            expected = git(root, "rev-parse", "HEAD").stdout.strip()
            (root / ".git" / "index.lock").write_text("transient", encoding="utf-8")
            snapshot = source_snapshot.capture_source_snapshot(root, timeout_seconds=60)
            self.assertFalse((snapshot.root / ".git" / "index.lock").exists())
            observed = subprocess.run(
                ["git", "-C", str(snapshot.project_path), "rev-parse", "HEAD"],
                text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            ).stdout.strip()
            self.assertEqual(expected, observed)

    def test_deploom_git_queries_disable_optional_index_refresh_locks(self) -> None:
        completed = subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, stdout="abc\n", stderr="")
        with mock.patch("source_snapshot.subprocess.run", return_value=completed) as run:
            source_snapshot._run_git(Path("."), ["rev-parse", "HEAD"])
        self.assertEqual("0", run.call_args.kwargs["env"].get("GIT_OPTIONAL_LOCKS"))


if __name__ == "__main__":
    unittest.main()
