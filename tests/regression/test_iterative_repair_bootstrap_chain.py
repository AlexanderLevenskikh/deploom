from __future__ import annotations

# P2 (#1): the repair of an initially-red project must be independent of the
# update roadmap. `begin --repair-only` runs the CURRENT-state control with NO
# targets and NO discovery; a red C0 opens the durable BOOTSTRAP_REPAIR run +
# bootstrap repair request, and `bootstrap-materialize` creates the isolated
# version-neutral trial (the repair checkout the agent works in). The whole
# chain is exercised end-to-end through the real CLI — "correct button" alone
# is not acceptance; the isolated repair checkout must actually appear.

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import iterative_migration


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def init_repo(root: Path) -> None:
    git(root, "init", "-b", "master")
    git(root, "config", "user.email", "repair@example.invalid")
    git(root, "config", "user.name", "Repair")


def make_repo_with_failing_check(root: Path) -> Path:
    # A REAL failing current-state control: the discovered project check exits 1.
    # The lockfile makes the topology gate pass so the failure is a project-level
    # red, not an infrastructure ("owning lockfile missing") inconclusive.
    root.mkdir(parents=True, exist_ok=True)
    init_repo(root)
    (root / "package.json").write_text(
        json.dumps({
            "name": "repair-demo", "private": True, "dependencies": {},
            "scripts": {"test": "node -e \"process.exit(1)\""},
        }),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        json.dumps({
            "name": "repair-demo", "version": "1.0.0", "lockfileVersion": 3,
            "requires": True,
            "packages": {"": {"name": "repair-demo", "version": "1.0.0", "dependencies": {}}},
        }),
        encoding="utf-8",
    )
    (root / "src.txt").write_text("committed", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "init")
    return root


class IterativeRepairBootstrapChainTests(unittest.TestCase):
    def _cli(self, run_dir: Path, command: str, project: Path | None = None, project_name: str = "RepairDemo", repair: bool = False) -> subprocess.CompletedProcess[str]:
        args = [sys.executable, "iterative_migration.py", "--run-dir", str(run_dir), command]
        if project is not None:
            args += ["--project-dir", str(project), "--project-name", project_name]
            if repair:
                args.append("--repair-only")
        return subprocess.run(
            args,
            cwd=str(Path(__file__).resolve().parents[2]),
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
        )

    def test_repair_begin_red_reaches_isolated_checkout_without_roadmap(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory(prefix="deploom-repair-") as tmp:
            project = make_repo_with_failing_check(Path(tmp) / "repo")
            run_dir = Path(tmp) / "run"

            # P2 (#1): repair begin MUST NOT need targets — no targets file, no
            # discovery, and still a durable red run appears.
            began = self._cli(run_dir, "begin", project, repair=True)
            self.assertEqual(began.returncode, 0, began.stdout + began.stderr)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["phase"], "BOOTSTRAP_REPAIR", "a red C0 opens the repair track, not a dead-end")
            self.assertEqual(run["terminal"], None)
            config = json.loads((run_dir / "run-config.json").read_text(encoding="utf-8"))
            self.assertTrue(config.get("repairOnly"), "the run must be marked repair-only")
            # P2 (#1): repair is independent of roadmap targets.
            self.assertEqual(config.get("targets") or {}, {})
            requests = json.loads((run_dir / "repair-requests.json").read_text(encoding="utf-8"))
            self.assertTrue(any(req.get("bootstrap") for req in requests.get("requests", [])),
                            "a bootstrap repair request must be published")
            c0 = json.loads((run_dir / "checkpoints" / "C0.json").read_text(encoding="utf-8"))
            self.assertNotEqual(c0["status"], "VERIFIED", "a red C0 must not be recorded as verified")

            # P2 (#1): materializing the version-neutral trial CREATES the
            # isolated repair checkout — the agent works there, never on the
            # developer checkout.
            mat = self._cli(run_dir, "bootstrap-materialize")
            self.assertEqual(mat.returncode, 0, mat.stdout + mat.stderr)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["phase"], "BOOTSTRAP_REPAIR")
            refs = run.get("bootstrapRefs") or {}
            workspace_root = Path(str(refs.get("workspaceRoot") or ""))
            self.assertTrue(refs.get("workspaceRoot") and workspace_root.exists(),
                            f"bootstrapRefs.workspaceRoot must point at a real isolated checkout: {refs}")
            trial_project = workspace_root / str(refs.get("projectRelative") or ".")
            self.assertTrue((trial_project / "package.json").exists(),
                            "the isolated trial project must be materialized with its manifest")
            # The trial is version-neutral CURRENT state — same failing check
            # bytes as C0, NOT a candidate for a target version.
            self.assertNotIn("candidateId", run)
            self.assertTrue(any(req.get("bootstrap") for req in requests.get("requests", [])),
                            "the bootstrap request must stay open until the agent repairs")
            # The red C0 end-to-end decision is the AGENT GATE (repair needed).
            status = self._cli(run_dir, "status", None)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            payload = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["run"]["phase"], "BOOTSTRAP_REPAIR")

    def test_repair_begin_green_is_repair_verified_not_partial(self) -> None:
        # A project whose control PASSES has nothing to repair: the repair run
        # terminates REPAIR_VERIFIED (control passed), NEVER PARTIAL_VERIFIED —
        # "0 verified upgrades" is not "часть обновлений применена" (P2 #3).
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory(prefix="deploom-repair-green-") as tmp:
            project = make_repo_with_failing_check(Path(tmp) / "repo")
            # "Fix" the check: make the test command pass, so the control is green.
            (project / "package.json").write_text(
                json.dumps({
                    "name": "repair-demo", "private": True, "dependencies": {},
                    "scripts": {"test": "node -e \"0\""},
                }),
                encoding="utf-8",
            )
            git(project, "add", "-A")
            git(project, "commit", "-m", "fix test")

            run_dir = Path(tmp) / "run"
            began = self._cli(run_dir, "begin", project, repair=True)
            self.assertEqual(began.returncode, 0, began.stdout + began.stderr)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            # A green repair begin opens a READY run (no targets); plan-next
            # reaches no actionable upgrades and the finish is REPAIR_VERIFIED.
            self.assertEqual(run["phase"], "READY")
            plan_next = self._cli(run_dir, "plan-next", None)
            self.assertEqual(plan_next.returncode, 0, plan_next.stdout + plan_next.stderr)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["phase"], "TERMINAL", "no targets + verified control → terminal")
            finished = self._cli(run_dir, "finish", None)
            self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["phase"], "TERMINAL")
            self.assertEqual(run["terminal"], "REPAIR_VERIFIED",
                             "a passed repair control is REPAIR_VERIFIED, not PARTIAL_VERIFIED")
            outcome = run["terminalOutcome"]
            self.assertEqual(outcome["outcome"], "REPAIR_VERIFIED")
            self.assertTrue(outcome["satisfied"], "the repair goal is satisfied")
            self.assertTrue((run_dir / iterative_migration.REPORTS_DIR / "MIGRATION_REPORT.md").exists())


if __name__ == "__main__":
    unittest.main()
