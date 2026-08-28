"""Baseline must work on a repository that has no configured remote.

Reported: creating a baseline for a local project failed with
`SOURCE_REMOTE_NOT_FOUND: remote 'origin' is not configured` and exit code 2.
Desktop now creates workspaces with a plain `git init`, so a repository with no
remote is a normal input, not a misconfiguration. Source Truth is the sealed
captured bytes; Git is a provenance mechanism only.

A remote that IS configured but missing must still fail closed.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as gen


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _repo(root: Path, *, branch: str = "master") -> Path:
    project = root / "tiny-basic"
    project.mkdir(parents=True)
    (project / "package.json").write_text('{"name":"tiny-basic"}\n', encoding="utf-8")
    _git(project, "init", "-b", branch)
    _git(project, "config", "user.email", "local@example.invalid")
    _git(project, "config", "user.name", "Local Only")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "fixture")
    return project


def _spec(project: Path, *, source_branch: str = "master", remote: str = "origin"):
    return gen.ProjectSpec(
        name="Tiny basic",
        path=project,
        source_branch=source_branch,
        git_remote=remote,
    )


class LocalOnlySourceProvenance(unittest.TestCase):
    def test_repository_without_any_remote_is_verified_locally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = _repo(Path(raw))
            metadata = gen.ensure_source_checkout(_spec(project))
            self.assertTrue(metadata["verified"])
            self.assertTrue(metadata["localOnly"])
            self.assertEqual(metadata["remote"], "")
            self.assertEqual(metadata["sourceBranch"], "master")
            self.assertRegex(metadata["sourceCommit"], r"^[0-9a-f]{40}$")

    def test_unconfigured_branch_falls_back_to_current_branch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = _repo(Path(raw), branch="work")
            metadata = gen.ensure_source_checkout(_spec(project, source_branch=""))
            self.assertEqual(metadata["sourceBranch"], "work")

    def test_dirty_local_checkout_still_fails_closed(self) -> None:
        """The guard's real invariant must survive the local-only path."""
        with tempfile.TemporaryDirectory() as raw:
            project = _repo(Path(raw))
            (project / "package.json").write_text('{"name":"dirty"}\n', encoding="utf-8")
            with self.assertRaises(gen.SourceCheckoutGuardError) as ctx:
                gen.ensure_source_checkout(_spec(project))
            self.assertIn("SOURCE_CHECKOUT_DIRTY", str(ctx.exception))

    def test_missing_local_branch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = _repo(Path(raw))
            with self.assertRaises(gen.SourceCheckoutGuardError) as ctx:
                gen.ensure_source_checkout(_spec(project, source_branch="absent"))
            self.assertIn("SOURCE_BRANCH_NOT_FOUND", str(ctx.exception))

    def test_configured_remote_that_is_missing_still_fails_closed(self) -> None:
        """A repo WITH remotes but not the configured one is a misconfiguration."""
        with tempfile.TemporaryDirectory() as raw:
            project = _repo(Path(raw))
            _git(project, "remote", "add", "upstream", "https://example.invalid/x.git")
            with self.assertRaises(gen.SourceCheckoutGuardError) as ctx:
                gen.ensure_source_checkout(_spec(project, remote="origin"))
            self.assertIn("SOURCE_REMOTE_NOT_FOUND", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
