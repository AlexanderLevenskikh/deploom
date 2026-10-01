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
from types import SimpleNamespace
from unittest import mock

import baseline_constraint_verifier
import iterative_migration
import source_snapshot
from iterative_migration import ProjectUnreadyError, project_readiness_preflight
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


def make_repo_with_passing_check(root: Path) -> Path:
    init_repo(root)
    (root / "package.json").write_text(
        json.dumps({
            "name": "preflight-plain", "private": True, "dependencies": {},
            "scripts": {"test": "node -e \"0\""},
        }),
        encoding="utf-8",
    )
    # A real current-state control needs an OWNING lockfile: without one the
    # topology gate answers infrastructure ("owning lockfile missing") and the
    # honest verdict is INCONCLUSIVE, never "проект проверен". With a minimal
    # lockfile (and no deps) the control genuinely passes.
    (root / "package-lock.json").write_text(
        json.dumps({
            "name": "preflight-plain", "version": "1.0.0", "lockfileVersion": 3,
            "requires": True,
            "packages": {"": {"name": "preflight-plain", "version": "1.0.0", "dependencies": {}}},
        }),
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

    # P1#4: "Проверить проект" is a REAL separate step — the same CLI `begin`,
    # but `--check-only`. It runs the readiness preflight AND the control
    # verification of the CURRENT dependencies, then records a durable non-run
    # verdict (project-check.json). No run.json/config/snapshot are created, so
    # the later "Начать обновление" begin is still a fresh run. The caller owns
    # `run_dir` (kept alive while it asserts on the artifacts).
    def _cli_check(self, project: Path, run_dir: Path, verify_config: str = "") -> subprocess.CompletedProcess[str]:
        args = [sys.executable, "iterative_migration.py", "--run-dir", str(run_dir), "begin",
                "--project-dir", str(project), "--project-name", "PreflightDemo", "--check-only"]
        if verify_config:
            args += ["--verify-config", verify_config]
        return subprocess.run(
            args,
            cwd=str(Path(__file__).resolve().parents[2]),
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=300, check=False,
        )

    def test_check_only_blocks_uninitialized_submodule_without_creating_a_run(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as subtmp:
            project = make_repo_with_uninitialized_submodule(Path(tmp), Path(subtmp))
            with tempfile.TemporaryDirectory() as runtmp:
                run_dir = Path(runtmp) / "run"
            result = self._cli_check(project, run_dir)
            self.assertNotEqual(result.returncode, 0)
            combined = result.stdout + result.stderr
            payload = json.loads(combined.split("ITERATIVE_MIGRATION_FAILURE_V1 ", 1)[1].strip())
            self.assertEqual(payload["code"], "SOURCE_SUBMODULE_INCOMPLETE")
            self.assertIn("--init --recursive --", payload["command"])
            self.assertFalse(run_dir.exists(), "a blocked check must not create a run directory")

    def test_check_only_records_ok_without_a_run(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as runtmp:
            project = make_repo_with_passing_check(Path(tmp))
            result = self._cli_check(project, Path(runtmp))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            check = json.loads((Path(runtmp) / "project-check.json").read_text(encoding="utf-8"))
            self.assertTrue(check["ok"])
            self.assertEqual(check["control"]["status"], "passed")
            self.assertEqual(check["control"]["kind"], "passed")
            self.assertFalse((Path(runtmp) / "run.json").exists(),
                             "a check must not create a run — 'Начать обновление' stays a separate step")
            self.assertFalse((Path(runtmp) / "run-config.json").exists())
            self.assertFalse((Path(runtmp) / "SOURCE").exists())

    def test_check_only_reports_failing_control_then_real_begin_is_fresh(self) -> None:
        # A project that does not pass its own checks is surfaced as a fixable
        # check verdict (PROJECT_CONTROL_FAILED) with a durable ok:false check
        # file and NO run. After the verify "passes", the SAME check command
        # succeeds and a real `begin` proceeds to create the durable run.
        # (The verify verdict is stubbed: a 0-dependency fixture cannot install a
        # real resolved state, so this asserts the check-only gate logic, and the
        # end-to-end real-begin path is covered by the CLI check below.)
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as runtmp:
            project = make_plain_repo(Path(tmp))
            run_dir = Path(runtmp)

            def failing_result(*_args, **_kwargs):
                return SimpleNamespace(
                    ok=False,
                    hard_failure=False,
                    project_failures=(
                        baseline_constraint_verifier.BaselineProjectFailure(
                            command="npm run test", exit_code=1, output="FAILED",
                        ),
                    ),
                    observed_resolved_versions={},
                    resolved_state_key="",
                    observed_resolved_hash=None,
                    preparation_proof_key="",
                    kind="project-check",
                )

            args = Namespace()
            check_config = {
                "projectDir": str(project),
                "projectName": "PreflightDemo",
                "requestedNode": "",
                "runtime": {},
                "verifyConfig": {"commands": []},
            }
            with mock.patch.object(iterative_migration, "verify_assignment", side_effect=failing_result):
                with self.assertRaises(ProjectUnreadyError) as ctx:
                    iterative_migration._begin_check_only_locked(run_dir, args, check_config)
            self.assertEqual(ctx.exception.code, "PROJECT_CONTROL_FAILED")
            check = json.loads((run_dir / "project-check.json").read_text(encoding="utf-8"))
            self.assertFalse(check["ok"])
            self.assertEqual(check["control"]["status"], "failed")
            self.assertFalse((run_dir / "run.json").exists(), "a failed check must not create a run")

            # "Fix": the same verify now passes — the check succeeds.
            passing = SimpleNamespace(
                ok=True,
                hard_failure=False,
                project_failures=(),
                observed_resolved_versions={},
                resolved_state_key="",
                observed_resolved_hash=None,
                preparation_proof_key="",
                kind="project-check",
            )
            with mock.patch.object(iterative_migration, "verify_assignment", return_value=passing):
                self.assertEqual(iterative_migration._begin_check_only_locked(run_dir, args, check_config), 0)
            check = json.loads((run_dir / "project-check.json").read_text(encoding="utf-8"))
            self.assertTrue(check["ok"])
            # A real begin now proceeds end-to-end (its C0 control on the plain
            # repo is unknown-but-not-blocking; the run is still created).
            full = self._cli(project)
            self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
            self.assertIn("begin.capture", full.stdout)

    def test_check_only_infrastructure_is_inconclusive_not_ok(self) -> None:
        # P1 (#1): an infrastructure / unknown / budget outcome must NEVER be
        # written as "Проект проверен". Absence of *project-command* failures is
        # not proof the check passed: the verdict is kept INCOMPLETE (ok:false,
        # control.status="inconclusive") and the panel offers a retry.
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as runtmp:
            project = make_plain_repo(Path(tmp))
            run_dir = Path(runtmp)

            def infra_result(_project_dir, _assignment, **_kwargs):
                return SimpleNamespace(
                    ok=False,
                    hard_failure=False,
                    project_failures=(),
                    observed_resolved_versions={},
                    resolved_state_key="",
                    observed_resolved_hash=None,
                    preparation_proof_key="",
                    kind="infrastructure",
                    summary="npm registry unreachable (ECONNREFUSED)",
                )

            args = Namespace()
            check_config = {
                "projectDir": str(project),
                "projectName": "PreflightDemo",
                "requestedNode": "",
                "runtime": {},
                "verifyConfig": {"commands": []},
            }
            with mock.patch.object(iterative_migration, "verify_assignment", side_effect=infra_result):
                with self.assertRaises(ProjectUnreadyError) as ctx:
                    iterative_migration._begin_check_only_locked(run_dir, args, check_config)
            self.assertEqual(ctx.exception.code, "PROJECT_CHECK_INCONCLUSIVE")
            check = json.loads((run_dir / "project-check.json").read_text(encoding="utf-8"))
            self.assertFalse(check["ok"], "an inconclusive check must never be ok:true")
            self.assertEqual(check["control"]["status"], "inconclusive")
            self.assertEqual(check["control"]["kind"], "infrastructure")
            self.assertFalse((run_dir / "run.json").exists(), "an inconclusive check must not create a run")

    def test_check_only_unknown_kind_is_inconclusive_not_ok(self) -> None:
        # The same honest rule for `unknown` (and budget): no definitive verdict
        # recorded as a pass.
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as runtmp:
            project = make_plain_repo(Path(tmp))
            run_dir = Path(runtmp)
            args = Namespace()
            check_config = {
                "projectDir": str(project),
                "projectName": "PreflightDemo",
                "requestedNode": "",
                "runtime": {},
                "verifyConfig": {"commands": []},
            }
            for kind in ("unknown", "budget"):
                def inconclusive_result(_project_dir, _assignment, **_kwargs):
                    return SimpleNamespace(
                        ok=False, hard_failure=False, project_failures=(),
                        observed_resolved_versions={}, resolved_state_key="",
                        observed_resolved_hash=None, preparation_proof_key="",
                        kind=kind, summary=f"{kind} outcome: no definitive verdict",
                    )
                with mock.patch.object(iterative_migration, "verify_assignment", side_effect=inconclusive_result):
                    with self.assertRaises(ProjectUnreadyError) as ctx:
                        iterative_migration._begin_check_only_locked(run_dir, args, check_config)
                self.assertEqual(ctx.exception.code, "PROJECT_CHECK_INCONCLUSIVE")
                check = json.loads((run_dir / "project-check.json").read_text(encoding="utf-8"))
                self.assertFalse(check["ok"])
                self.assertEqual(check["control"]["kind"], kind)

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
                iterative_migration._begin_locked(run_dir, config, Namespace(run_id="iter-unsettled"))
            self.assertEqual(ctx.exception.code, "DISCOVERY_UNSETTLED")
            self.assertFalse((run_dir / "run.json").exists(), "an unsettled discovery must not create a run")
            self.assertFalse((run_dir / "SOURCE").exists(), "no expensive capture must happen on an unsettled discovery")
