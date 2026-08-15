from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from git_hook_policy import run_git
from dependency_release_branch import ReleaseBranchError, _assemble_sharded_docs, create_release_branch
from dependency_live_roadmap_generator import normalize_git_hook_policy, normalize_release_policy


@unittest.skipUnless(shutil.which("git"), "git is required")
class GitHookReleasePolicyRegressionTests(unittest.TestCase):
    def git(self, project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(project), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(result.stdout + result.stderr)
        return result

    def init_repo(self, root: Path) -> tuple[Path, str]:
        project = root / "project"
        subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
        self.git(project, "config", "user.name", "Roadmap Test")
        self.git(project, "config", "user.email", "roadmap@example.test")
        # Reproduce repositories/users that configure merge.ff=false. Git
        # translates that setting to --no-ff, which must not break squash.
        self.git(project, "config", "merge.ff", "false")
        self.git(project, "checkout", "-b", "master")
        (project / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
        (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
        self.git(project, "add", "package.json", "yarn.lock")
        self.git(project, "commit", "-m", "initial")
        return project, self.git(project, "rev-parse", "HEAD").stdout.strip()

    def install_hook(self, project: Path, name: str, *, exit_code: int, marker: str = "hook-ran.txt") -> None:
        hooks = project / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / name
        hook.write_text(
            "#!/bin/sh\n"
            f'echo {name} >> "$(git rev-parse --git-dir)/{marker}"\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(hook, 0o755)

    def prepare_merged_branch(self, project: Path, source_commit: str) -> str:
        self.git(project, "checkout", "-b", "libs-merged", source_commit)
        (project / "src.txt").write_text("updated\n", encoding="utf-8")
        workspace = project / ".dependency-roadmap-audit"
        workspace.mkdir()
        (workspace / ".dependency-roadmap-audit-workspace.json").write_text(
            '{"managedBy":"dependency-roadmap-tool"}\n', encoding="utf-8"
        )
        (workspace / "npm-audit.json").write_text('{"metadata":{}}\n', encoding="utf-8")
        self.git(project, "add", "src.txt", ".dependency-roadmap-audit")
        result = run_git(project, ["commit", "-m", "integration"], skip_hooks=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        merged_commit = self.git(project, "rev-parse", "HEAD").stdout.strip()
        self.git(project, "checkout", "master")
        return merged_commit

    def test_intermediate_commit_uses_empty_hooks_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _ = self.init_repo(Path(tmp))
            self.install_hook(project, "pre-commit", exit_code=91)
            (project / "internal.txt").write_text("internal\n", encoding="utf-8")
            self.git(project, "add", "internal.txt")

            result = run_git(project, ["commit", "-m", "internal"], skip_hooks=True, check=False)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse((project / ".git" / "hook-ran.txt").exists())
            self.assertEqual("", self.git(project, "status", "--porcelain").stdout.strip())

    def test_release_commit_runs_hook_and_removes_audit_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source_commit = self.init_repo(Path(tmp))
            merged_commit = self.prepare_merged_branch(project, source_commit)
            self.install_hook(project, "pre-commit", exit_code=0)

            result = create_release_branch(
                project,
                "master",
                source_commit,
                "libs-merged",
                "libs-release",
                gate_commands=["git diff --cached --quiet && exit 1 || exit 0"],
            )

            self.assertTrue(result["released"])
            self.assertFalse(result["releaseCommitHooksBypassed"])
            self.assertEqual(merged_commit, result["mergedCommit"])
            self.assertTrue((project / ".git" / "hook-ran.txt").exists())
            self.assertEqual("libs-release", self.git(project, "branch", "--show-current").stdout.strip())
            self.assertEqual(source_commit, self.git(project, "rev-parse", "HEAD^").stdout.strip())
            self.assertEqual("updated", (project / "src.txt").read_text(encoding="utf-8").strip())
            self.assertFalse((project / ".dependency-roadmap-audit").exists())
            self.assertEqual("", self.git(project, "ls-tree", "-r", "--name-only", "HEAD", "--", ".dependency-roadmap-audit").stdout.strip())
            message = self.git(project, "log", "-1", "--format=%B").stdout
            self.assertIn("Dependency-Roadmap-Hooks-Bypassed: false", message)

    def test_release_succeeds_when_audit_workspace_is_already_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source_commit = self.init_repo(Path(tmp))
            self.git(project, "checkout", "-b", "libs-merged", source_commit)
            (project / "src.txt").write_text("updated\n", encoding="utf-8")
            self.git(project, "add", "src.txt")
            result = run_git(project, ["commit", "-m", "integration"], skip_hooks=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.git(project, "checkout", "master")

            released = create_release_branch(project, "master", source_commit, "libs-merged", "libs-release")

            self.assertTrue(released["released"])
            self.assertEqual("updated", (project / "src.txt").read_text(encoding="utf-8").strip())
            self.assertEqual("", self.git(project, "status", "--porcelain").stdout.strip())

    def test_dirty_worktree_is_saved_in_named_safety_stash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source_commit = self.init_repo(Path(tmp))
            self.prepare_merged_branch(project, source_commit)
            (project / "package.json").write_text('{"name":"local-change"}\n', encoding="utf-8")
            (project / "notes.local.txt").write_text("keep me\n", encoding="utf-8")

            released = create_release_branch(project, "master", source_commit, "libs-merged", "libs-release")

            stash = released["safetyStash"]
            self.assertIsNotNone(stash)
            self.assertRegex(stash["message"], r"^dependency-flow-before-release-\d{8}-\d{6}$")
            self.assertEqual(stash["commit"], self.git(project, "rev-parse", "refs/stash").stdout.strip())
            self.assertIn(stash["commit"], stash["restoreCommand"])
            self.assertFalse((project / "notes.local.txt").exists())
            self.assertEqual('{"name":"demo","version":"1.0.0"}', (project / "package.json").read_text(encoding="utf-8").strip())

    def test_unsafe_hook_policy_override_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "GIT_HOOK_POLICY_UNSAFE"):
            normalize_git_hook_policy({"intermediateCommits": "run"})
        self.assertEqual("skip", normalize_git_hook_policy({})["intermediatePushes"])
        self.assertEqual("run", normalize_git_hook_policy({})["releaseCommit"])

    def test_non_squash_or_unclean_release_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "RELEASE_STRATEGY_UNSUPPORTED"):
            normalize_release_policy({"strategy": "merge"})
        with self.assertRaisesRegex(ValueError, "RELEASE_AUDIT_CLEANUP_REQUIRED"):
            normalize_release_policy({"cleanupAuditWorkspace": False})

    def test_final_gate_must_not_leave_unstaged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source_commit = self.init_repo(Path(tmp))
            self.prepare_merged_branch(project, source_commit)

            with self.assertRaises(ReleaseBranchError) as raised:
                create_release_branch(
                    project,
                    "master",
                    source_commit,
                    "libs-merged",
                    "libs-release",
                    gate_commands=["echo dirty > gate-output.txt"],
                )

            self.assertEqual("RELEASE_FINAL_GATE_DIRTY", raised.exception.code)
            self.assertEqual("libs-release", self.git(project, "branch", "--show-current").stdout.strip())
            self.assertFalse((project / ".git" / "hook-ran.txt").exists())

    def test_failed_release_hook_can_be_fixed_and_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source_commit = self.init_repo(Path(tmp))
            self.prepare_merged_branch(project, source_commit)
            self.install_hook(project, "pre-commit", exit_code=93)

            with self.assertRaises(ReleaseBranchError) as raised:
                create_release_branch(project, "master", source_commit, "libs-merged", "libs-release")
            self.assertEqual("RELEASE_COMMIT_OR_HOOK_FAILED", raised.exception.code)
            self.assertEqual("libs-release", self.git(project, "branch", "--show-current").stdout.strip())
            self.assertNotEqual("", self.git(project, "diff", "--cached", "--name-only").stdout.strip())

            self.install_hook(project, "pre-commit", exit_code=0)
            result = create_release_branch(project, "master", source_commit, "libs-merged", "libs-release")

            self.assertTrue(result["resumed"])
            self.assertEqual("", self.git(project, "status", "--porcelain").stdout.strip())

    def test_sibling_branch_doc_shards_merge_without_conflict_and_assemble_in_order(self) -> None:
        # Every work branch used to write the same flat docs/*.md file, created
        # fresh from the same base commit on every sibling branch, so merging
        # any two of them produced an add/add conflict on all three docs every
        # time -- observed for real, twice. The compact prompt now has each
        # branch write to its own docs/<name>/<branch>.md shard instead, so
        # this merges two real sibling branches with real git and asserts no
        # conflict occurs, then that create_release_branch assembles the
        # shards into the flat files teams actually read, in branch order.
        with tempfile.TemporaryDirectory() as tmp:
            project, source_commit = self.init_repo(Path(tmp))
            self.git(project, "checkout", "-b", "libs-merged", source_commit)

            for branch, note in (("libs-group-1", "postcss"), ("libs-group-2", "react-router-dom")):
                self.git(project, "checkout", "-b", branch, "libs-merged")
                for doc_name in ("dependency-upgrades", "dependency-update-summary", "dependency-update-review-notes"):
                    shard_dir = project / "docs" / doc_name
                    shard_dir.mkdir(parents=True, exist_ok=True)
                    (shard_dir / f"{branch}.md").write_text(f"## {branch}\n\n{doc_name} for {note}.\n", encoding="utf-8")
                self.git(project, "add", "docs")
                result = run_git(project, ["commit", "-m", f"chore(deps): {branch}"], skip_hooks=True, check=False)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.git(project, "checkout", "libs-merged")
                merge = run_git(project, ["merge", "--no-ff", branch, "-m", f"Merge {branch}"], skip_hooks=True, check=False)
                self.assertEqual(0, merge.returncode, f"sibling branch {branch} conflicted on shard docs: {merge.stdout}{merge.stderr}")

            released = create_release_branch(project, "master", source_commit, "libs-merged", "libs-release")

            self.assertEqual(
                ["docs/dependency-update-review-notes.md", "docs/dependency-update-summary.md", "docs/dependency-upgrades.md"],
                sorted(released["shardedDocsAssembled"]),
            )
            upgrades = (project / "docs" / "dependency-upgrades.md").read_text(encoding="utf-8")
            self.assertIn("## libs-group-1", upgrades)
            self.assertIn("## libs-group-2", upgrades)
            self.assertLess(upgrades.index("## libs-group-1"), upgrades.index("## libs-group-2"))
            self.assertFalse((project / "docs" / "dependency-upgrades").exists())
            self.assertFalse((project / "docs" / "dependency-update-summary").exists())
            self.assertFalse((project / "docs" / "dependency-update-review-notes").exists())
            self.assertEqual("", self.git(project, "status", "--porcelain").stdout.strip())
            committed = self.git(project, "ls-tree", "-r", "--name-only", "HEAD", "--", "docs").stdout
            self.assertIn("docs/dependency-upgrades.md", committed)
            self.assertNotIn("docs/dependency-upgrades/", committed)

    def test_missing_shard_directory_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, source_commit = self.init_repo(Path(tmp))
            self.prepare_merged_branch(project, source_commit)

            released = create_release_branch(project, "master", source_commit, "libs-merged", "libs-release")

            self.assertEqual([], released["shardedDocsAssembled"])
            self.assertFalse((project / "docs").exists())


class ShardedDocAssemblyUnitTests(unittest.TestCase):
    def test_shards_are_assembled_in_natural_order_not_lexicographic(self) -> None:
        # Lexicographic sort would put "group-10" before "group-2"; branch
        # order must follow the numeric suffix instead.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            shard_dir = project / "docs" / "dependency-upgrades"
            shard_dir.mkdir(parents=True)
            for branch in ("libs-group-2", "libs-group-10", "libs-group-1"):
                (shard_dir / f"{branch}.md").write_text(f"## {branch}\n\ncontent\n", encoding="utf-8")

            assembled = _assemble_sharded_docs(project)

            self.assertEqual(["docs/dependency-upgrades.md"], assembled)
            flat = (project / "docs" / "dependency-upgrades.md").read_text(encoding="utf-8")
            self.assertLess(flat.index("libs-group-1"), flat.index("libs-group-2"))
            self.assertLess(flat.index("libs-group-2"), flat.index("libs-group-10"))
            self.assertFalse(shard_dir.exists())

    def test_shards_are_appended_after_existing_flat_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "docs").mkdir()
            (project / "docs" / "dependency-upgrades.md").write_text("# Existing history\n\nOld entry.\n", encoding="utf-8")
            shard_dir = project / "docs" / "dependency-upgrades"
            shard_dir.mkdir()
            (shard_dir / "libs-group-1.md").write_text("## libs-group-1\n\nnew content\n", encoding="utf-8")

            _assemble_sharded_docs(project)

            flat = (project / "docs" / "dependency-upgrades.md").read_text(encoding="utf-8")
            self.assertLess(flat.index("Old entry"), flat.index("libs-group-1"))


if __name__ == "__main__":
    unittest.main()
