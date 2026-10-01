from __future__ import annotations

# P5 acceptance: a fixable local readiness blocker (uninitialized submodule) is
# detected BEFORE the expensive registry discovery / source snapshot, shares the
# SAME verification code as the authoritative capture check, carries a
# copyable-but-not-auto-run Git command, and a re-run after fixing the blocker
# works without any manual state cleanup.

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import source_snapshot
from iterative_migration import ProjectUnreadyError, project_readiness_preflight, _begin_locked
from source_snapshot import incomplete_submodules


def git(cwd: Path, *args: str, check: bool = True, env=None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        env=env,
    )


def init_repo(root: Path) -> None:
    git(root, "init", "-b", "master")
    git(root, "config", "user.email", "preflight@example.invalid")
    git(root, "config", "user.name", "Preflight")


def make_repo_with_uninitialized_submodule(root: Path, subtmp: Path) -> Path:
    init_repo(root)
    (root / "package.json").write_text(
        json.dumps({"name": "preflight-demo", "private": True, "dependencies": {}}),
        encoding="utf-8",
    )
    (root / "src.txt").write_text("committed", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    sub = subtmp
    init_repo(sub)
    (sub / "sub.txt").write_text("submodule", encoding="utf-8")
    git(sub, "add", ".")
    git(sub, "commit", "-m", "sub")
    added = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(root), "submodule", "add", str(sub), "vendor/sub"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if added.returncode != 0:
        raise unittest.SkipTest(f"local submodule add unavailable: {added.stderr}")
    git(root, "commit", "-am", "submodule")
    git(root, "submodule", "deinit", "-f", "--", "vendor/sub")
    return root


def make_plain_repo(root: Path) -> Path:
    init_repo(root)
    (root / "package.json").write_text(
        json.dumps({"name": "preflight-plain", "private": True, "dependencies": {}}),
        encoding="utf-8",
    )
    (root / "src.txt").write_text("committed", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    return root


class IterativePreflightReadinessTests(unittest.TestCase):
    def _cli(self, project: Path) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="deploom-preflight-run-") as runtmp:
            return subprocess.run(
                [sys.executable, "iterative_migration.py", "--run-dir", runtmp, "begin",
                 "--project-dir", str(project), "--project-name", "PreflightDemo"],
                cwd=str(Path(__file__).resolve().parents[2]),
                text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=300, check=False,
            )

    def test_preflight_blocks_uninitialized_submodule_before_discovery(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as subtmp:
            project = make_repo_with_uninitialized_submodule(Path(tmp), Path(subtmp))
            with self.assertRaises(ProjectUnreadyError) as ctx:
                project_readiness_preflight(project)
            error = ctx.exception
            self.assertEqual(error.code, "SOURCE_SUBMODULE_INCOMPLETE")
            self.assertIn("vendor/sub", str(error))
            self.assertIn("--init --recursive", error.command)
            self.assertIn("vendor/sub", error.command)
            self.assertTrue(error.fixable)

    def test_preflight_shares_the_capture_check_code(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as subtmp:
            project = make_repo_with_uninitialized_submodule(Path(tmp), Path(subtmp))
            issues = incomplete_submodules(project)
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0][0], "-")
            self.assertEqual(issues[0][1], "vendor/sub")
            with self.assertRaisesRegex(source_snapshot.SourceCaptureError, "SOURCE_SUBMODULE_INCOMPLETE"):
                source_snapshot._submodule_preflight(project)

    def test_cli_fails_fast_with_machine_readable_blocker_and_command(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as subtmp:
            project = make_repo_with_uninitialized_submodule(Path(tmp), Path(subtmp))
            result = self._cli(project)
            self.assertNotEqual(result.returncode, 0)
            combined = result.stdout + result.stderr
            marker = "ITERATIVE_MIGRATION_FAILURE_V1 "
            self.assertIn(marker, combined)
            payload = json.loads(combined.split(marker, 1)[1].strip())
            self.assertEqual(payload["code"], "SOURCE_SUBMODULE_INCOMPLETE")
            self.assertTrue(payload["fixable"])
            self.assertIn("vendor/sub", payload["summary"])
            self.assertIn("--init --recursive --", payload["command"])
            self.assertIn("vendor/sub", payload["command"])
            # P5: the failure happens BEFORE the expensive phases — no capture,
            # no registry discovery, no control verification.
            self.assertNotIn("begin.capture", combined)
            self.assertNotIn("begin.discovery-progress", combined)
            self.assertNotIn("begin.c0-verify", combined)

    def test_fixing_the_submodule_makes_retry_pass_without_state_cleanup(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as subtmp:
            project = make_repo_with_uninitialized_submodule(Path(tmp), Path(subtmp))
            # Confirm the blocker first.
            self.assertEqual(len(incomplete_submodules(project)), 1)
            # "Fix" = the SAME command the failure suggests, run by the user.
            fixed = subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "-C", str(project), "submodule", "update", "--init", "--recursive", "--", "vendor/sub"],
                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(fixed.returncode, 0, fixed.stderr)
            self.assertEqual(incomplete_submodules(project), [])
            project_readiness_preflight(project)  # does not raise
            # A re-run of the SAME command (no manual file deletion) completes
            # begin end-to-end and creates the durable run.
            result = self._cli(project)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("begin.capture", result.stdout)

    def test_unsettled_discovery_never_creates_a_fake_run(self) -> None:
        # P-review #3: when registry discovery returns ONLY "data unavailable" /
        # "budget skipped", begin must NOT create a run as a fake success — the
        # migration is not complete and retry needs no manual state cleanup.
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as runtmp:
            project = make_plain_repo(Path(tmp))
            run_dir = Path(runtmp)
            config = {
                "projectDir": str(project),
                "projectName": "Plain",
                "targets": {},
                "targetDiscovery": [
                    {"package": "alpha", "status": "registry-unavailable"},
                    {"package": "beta", "status": "discovery-budget-skipped"},
                ],
            }
            with self.assertRaises(ProjectUnreadyError) as ctx:
                _begin_locked(run_dir, config, Namespace(run_id="iter-unsettled"))
            self.assertEqual(ctx.exception.code, "DISCOVERY_UNSETTLED")
            self.assertFalse((run_dir / "run.json").exists(), "an unsettled discovery must not create a run")
            self.assertFalse((run_dir / "SOURCE").exists(), "no expensive capture must happen on an unsettled discovery")
