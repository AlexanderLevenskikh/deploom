"""A10: rollback of a failed local release must never destroy new edits.

The rollback inside push-branch-and-tag.ps1 is a `git reset --hard` -- a
destructive command. Regressions for the v0.2.133 static audit:

A10-1  `git reset --hard` may only run inside the dedicated
       Invoke-SafeReleaseRollback function, and only in its clean-worktree
       branch. The old code reset whenever the commit subject matched; a
       tracked file edited while validation was running would have been
       erased, and a rewritten HEAD with the same subject was not detected.
A10-2  Before resetting, the function requires HEAD to be the EXACT sha of
       the release commit this run created (sha AND subject) and the
       worktree/index to be clean (`git status --porcelain`).
A10-3  When the branch was already published, rollback never runs (history
       must not be rewritten).
A10-4  After partial publication (branch pushed, tag push failed) a rerun of
       the same version reuses the existing local tag pointing at the exact
       release commit, and refuses to move a tag pointing elsewhere.

Behavior of the decision logic itself (dirty-kept / clean-reset /
different-head-skipped / published-never) is verified by an interactive
disposable-repo run; these tests keep the safety invariants from drifting.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(ROOT / "push-branch-and-tag.ps1").read_text(encoding="utf-8")

FUNC_HEADER = "function Invoke-SafeReleaseRollback {"
REPO_MARKER = "# ------------------------------------------------------------\n# Repository"


def _function_body() -> str:
    start = SCRIPT.index(FUNC_HEADER)
    end = SCRIPT.index(REPO_MARKER, start)
    return SCRIPT[start:end]


def _function_start_line(func: str) -> int:
    return SCRIPT.index(func)


def _catch_block() -> str:
    start = SCRIPT.index("catch {")
    tail = SCRIPT[start:]
    # The script ends with catch/finally, so everything after "catch {"
    # up to "throw $OriginalError" is the handler.
    end = SCRIPT.index("throw $OriginalError", start)
    return SCRIPT[start:end]


class A10RollbackSafetyTests(unittest.TestCase):
    def test_rollback_is_isolated_in_dedicated_function(self) -> None:
        """A10-1: exactly one reset call exists, inside the function."""
        calls = re.findall(r"(?m)^\s*& git reset --hard", SCRIPT)
        self.assertEqual(len(calls), 1, "the reset command must be called exactly once")

        body = _function_body()
        self.assertIn("& git reset --hard HEAD^", body)
        handler = _catch_block()
        self.assertNotIn("& git reset --hard", handler)

    def test_reset_is_guarded_by_exact_sha_and_clean_worktree(self) -> None:
        """A10-2: reset only in the clean-worktree branch after sha verification."""
        body = _function_body()
        reset_pos = body.index("& git reset --hard HEAD^")
        sha_pos = body.index("git rev-parse HEAD")
        porcelain_pos = body.index("git status --porcelain")
        dirty_check_pos = body.index("DirtyLines.Count -gt 0")

        # sha and cleanliness are established strictly before the reset.
        self.assertLess(sha_pos, reset_pos)
        self.assertLess(porcelain_pos, reset_pos)
        self.assertLess(dirty_check_pos, reset_pos)

        # The reset sits in the clean branch: an `else {` opens between the
        # dirty branch check and the reset (the outer HEAD-not-exact else
        # comes after the reset).
        clean_branch_else = body.index("else {", dirty_check_pos)
        self.assertLess(clean_branch_else, reset_pos)

    def test_message_and_sha_both_checked(self) -> None:
        """A10-2: subject alone is not enough; exact sha is required."""
        body = _function_body()
        self.assertIn('$CurrentHeadSha -eq $ReleaseCommit', body)
        self.assertIn('$CurrentHeadMessage -eq "chore: release $Tag"', body)

    def test_published_branch_never_rolls_back(self) -> None:
        """A10-3: no rollback once the branch is visible on the remote."""
        body = _function_body()
        self.assertIn(
            "if (-not $VersionCommitCreated -or $BranchPublished) {",
            body,
        )

    def test_catch_calls_the_safe_function(self) -> None:
        """The error handler delegates, so a future edit cannot reintroduce an
        unruly reset beside it."""
        handler = _catch_block()
        self.assertIn("Invoke-SafeReleaseRollback", handler)

    def test_tag_reuse_for_partial_publication_recovery(self) -> None:
        """A10-4: rerun after a failed tag push reuses the local tag only when
        it points at the exact release commit and refuses to move it."""
        self.assertIn("git rev-parse -q --verify \"refs/tags/$Tag\"", SCRIPT)
        self.assertIn("refusing to move it", SCRIPT)

    def test_version_commit_and_release_sha_are_captured(self) -> None:
        """The release sha the rollback compares against is captured only
        after the optional version commit exists."""
        version_commit_pos = SCRIPT.index("$VersionCommitCreated = $true")
        release_sha_pos = SCRIPT.index("$ReleaseCommit = (& git rev-parse HEAD).Trim()")
        self.assertLess(version_commit_pos, release_sha_pos)


if __name__ == "__main__":
    unittest.main()
