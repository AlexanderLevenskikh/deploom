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

import dependency_live_roadmap_generator as roadmap


@unittest.skipUnless(shutil.which("git"), "git is required")
class SourceCheckoutGuardRegressionTests(unittest.TestCase):
    def git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
        return result

    def make_remote_and_clone(self, root: Path) -> tuple[Path, Path, Path]:
        remote = root / "remote.git"
        seed = root / "seed"
        target = root / "target"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
        self.git(seed, "config", "user.name", "Roadmap Test")
        self.git(seed, "config", "user.email", "roadmap@example.test")
        self.git(seed, "checkout", "-b", "master")
        (seed / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
        self.git(seed, "add", "package.json")
        self.git(seed, "commit", "-m", "initial")
        self.git(seed, "remote", "add", "origin", str(remote))
        self.git(seed, "push", "-u", "origin", "master")
        subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
        subprocess.run(["git", "clone", str(remote), str(target)], check=True, capture_output=True)
        self.git(target, "config", "user.name", "Roadmap Test")
        self.git(target, "config", "user.email", "roadmap@example.test")
        return remote, seed, target

    def spec(self, target: Path) -> roadmap.ProjectSpec:
        return roadmap.ProjectSpec(
            name="Demo",
            path=target,
            source_branch="master",
            git_remote="origin",
        )

    def test_clean_feature_checkout_is_switched_and_fast_forwarded_to_fetched_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, seed, target = self.make_remote_and_clone(Path(tmp))
            self.git(target, "checkout", "-b", "feature")

            (seed / "package.json").write_text('{"name":"demo","version":"1.1.0"}\n', encoding="utf-8")
            self.git(seed, "add", "package.json")
            self.git(seed, "commit", "-m", "remote update")
            self.git(seed, "push", "origin", "master")

            spec = self.spec(target)
            metadata = roadmap.ensure_source_checkout(spec)

            self.assertTrue(metadata["verified"])
            self.assertEqual("master", self.git(target, "branch", "--show-current").stdout.strip())
            self.assertEqual(
                self.git(target, "rev-parse", "HEAD").stdout.strip(),
                self.git(target, "rev-parse", "origin/master").stdout.strip(),
            )
            self.assertEqual("1.1.0", __import__("json").loads((target / "package.json").read_text())["version"])

    def test_source_checkout_and_fast_forward_skip_project_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, seed, target = self.make_remote_and_clone(Path(tmp))
            self.git(target, "checkout", "-b", "feature")

            hooks = target / ".git" / "hooks"
            for hook_name in ("post-checkout", "post-merge"):
                hook = hooks / hook_name
                hook.write_text(
                    "#!/bin/sh\n"
                    f'echo {hook_name} >> "$(git rev-parse --git-dir)/source-guard-hook-ran.txt"\n',
                    encoding="utf-8",
                    newline="\n",
                )
                os.chmod(hook, 0o755)

            (seed / "package.json").write_text('{"name":"demo","version":"1.1.0"}\n', encoding="utf-8")
            self.git(seed, "add", "package.json")
            self.git(seed, "commit", "-m", "remote update")
            self.git(seed, "push", "origin", "master")

            metadata = roadmap.ensure_source_checkout(self.spec(target))

            self.assertTrue(metadata["verified"])
            self.assertFalse((target / ".git" / "source-guard-hook-ran.txt").exists())

    def test_ide_workspace_noise_does_not_make_source_checkout_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, target = self.make_remote_and_clone(Path(tmp))
            idea = target / ".idea"
            idea.mkdir()
            (idea / "workspace.xml").write_text("local IDE state", encoding="utf-8")

            metadata = roadmap.ensure_source_checkout(self.spec(target))

            self.assertTrue(metadata["verified"])
            self.assertTrue((idea / "workspace.xml").exists())

    def test_dirty_checkout_fails_before_branch_switch_or_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, target = self.make_remote_and_clone(Path(tmp))
            self.git(target, "checkout", "-b", "feature")
            (target / "untracked.txt").write_text("local work", encoding="utf-8")

            with self.assertRaises(roadmap.SourceCheckoutGuardError) as context:
                roadmap.ensure_source_checkout(self.spec(target))

            self.assertEqual("SOURCE_CHECKOUT_DIRTY", context.exception.code)
            self.assertEqual("feature", self.git(target, "branch", "--show-current").stdout.strip())
            self.assertTrue((target / "untracked.txt").exists())

    def test_orphaned_tool_managed_audit_workspace_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, target = self.make_remote_and_clone(Path(tmp))
            (target / ".git" / "info" / "exclude").write_text(
                ".dependency-roadmap-audit/\n",
                encoding="utf-8",
            )
            workspace = target / ".dependency-roadmap-audit"
            workspace.mkdir()
            (workspace / ".dependency-roadmap-audit-workspace.json").write_text(
                '{"schemaVersion":1,"managedBy":"dependency-roadmap-tool"}',
                encoding="utf-8",
            )
            (workspace / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
            self.git(target, "add", "-f", ".dependency-roadmap-audit")

            spec = self.spec(target)
            spec.audit_bootstrap_config = {"workspace": ".dependency-roadmap-audit"}
            metadata = roadmap.ensure_source_checkout(spec)

            self.assertTrue(metadata["verified"])
            self.assertFalse(workspace.exists())
            self.assertEqual("", self.git(target, "status", "--short").stdout.strip())

    def test_audit_workspace_without_tool_marker_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, target = self.make_remote_and_clone(Path(tmp))
            (target / ".git" / "info" / "exclude").write_text(
                ".dependency-roadmap-audit/\n",
                encoding="utf-8",
            )
            workspace = target / ".dependency-roadmap-audit"
            workspace.mkdir()
            (workspace / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
            self.git(target, "add", "-f", ".dependency-roadmap-audit")

            spec = self.spec(target)
            spec.audit_bootstrap_config = {"workspace": ".dependency-roadmap-audit"}
            with self.assertRaises(roadmap.SourceCheckoutGuardError) as context:
                roadmap.ensure_source_checkout(spec)

            self.assertEqual("AUDIT_WORKSPACE_RECOVERY_REFUSED", context.exception.code)
            self.assertTrue((workspace / "package-lock.json").exists())
            self.assertIn("package-lock.json", self.git(target, "status", "--short").stdout)

    def test_local_source_branch_ahead_of_remote_is_not_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, target = self.make_remote_and_clone(Path(tmp))
            (target / "local.txt").write_text("local commit", encoding="utf-8")
            self.git(target, "add", "local.txt")
            self.git(target, "commit", "-m", "local only")
            local_head = self.git(target, "rev-parse", "HEAD").stdout.strip()

            with self.assertRaises(roadmap.SourceCheckoutGuardError) as context:
                roadmap.ensure_source_checkout(self.spec(target))

            self.assertEqual("SOURCE_BRANCH_DIVERGED", context.exception.code)
            self.assertEqual(local_head, self.git(target, "rev-parse", "HEAD").stdout.strip())
            self.assertTrue((target / "local.txt").exists())


if __name__ == "__main__":
    unittest.main()
