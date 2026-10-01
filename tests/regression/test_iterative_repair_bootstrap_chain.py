from __future__ import annotations

# P2 (#1): the repair of an initially-red project must be independent of the
# update roadmap. `begin --repair-only` runs the CURRENT-state control with NO
# targets and NO discovery; a red C0 opens the durable BOOTSTRAP_REPAIR run +
# bootstrap repair request, and `bootstrap-materialize` creates the isolated
# version-neutral trial (the repair checkout the agent works in). The whole
# chain is exercised end-to-end through the real CLI — "correct button" alone
# is not acceptance; the isolated repair checkout must actually appear.

import json
import re
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
    def _cli(self, run_dir: Path, command: str, project: Path | None = None, project_name: str = "RepairDemo", repair: bool = False, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        args = [sys.executable, "iterative_migration.py", "--run-dir", str(run_dir), command]
        if project is not None:
            args += ["--project-dir", str(project), "--project-name", project_name, "--target-level", "yellow"]
            if repair:
                args.append("--repair-only")
        if extra:
            args += extra
        return subprocess.run(
            args,
            cwd=str(Path(__file__).resolve().parents[2]),
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
        )

    @staticmethod
    def _archive_result(stdout: str) -> dict:
        match = re.search(r"ITERATIVE_ARCHIVE_RESULT_V1 (\{.*\})", stdout)
        if not match:
            return {}
        return json.loads(match.group(1))

    def _repair_chain_to_verified(self, run_dir: Path, project: Path) -> dict:
        """Red project -> BOOTSTRAP_REPAIR -> isolated repair (fix the trial
        check) -> verify-bootstrap -> finish REPAIR_VERIFIED. Returns the
        repaired C0 checkpoint + the repair run id."""
        began = self._cli(run_dir, "begin", project, repair=True)
        self.assertEqual(began.returncode, 0, began.stdout + began.stderr)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["phase"], "BOOTSTRAP_REPAIR")
        repair_run_id = str(run["runId"])
        mat = self._cli(run_dir, "bootstrap-materialize")
        self.assertEqual(mat.returncode, 0, mat.stdout + mat.stderr)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        refs = run.get("bootstrapRefs") or {}
        trial_project = Path(str(refs["workspaceRoot"])) / str(refs.get("projectRelative") or ".")
        self.assertTrue((trial_project / "package.json").exists(), "isolated trial must be materialized")
        # "The agent repairs the trial in isolation": make the failing check pass
        # ONLY inside the isolated checkout — the developer checkout stays red.
        fixed = json.loads((trial_project / "package.json").read_text(encoding="utf-8"))
        fixed["scripts"]["test"] = 'node -e "0"'
        (trial_project / "package.json").write_text(json.dumps(fixed), encoding="utf-8")
        verified = self._cli(run_dir, "verify-bootstrap")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        c0 = json.loads((run_dir / "checkpoints" / "C0.json").read_text(encoding="utf-8"))
        self.assertEqual(c0["status"], "VERIFIED", "verify-bootstrap must seal the fixed bytes as C0")
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["phase"], "READY")
        finished = self._cli(run_dir, "finish")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["terminal"], "REPAIR_VERIFIED")
        return {"c0": c0, "repairRunId": repair_run_id, "repairedKey": str(c0["sourceSnapshotKey"])}

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

    def test_repair_archive_and_adopt_continue_from_fixed_bytes(self) -> None:
        # P1 (v0.2.163 #1): the END-TO-END play the user asked for — a red
        # project, repaired in isolation, whose fixes are CHECKED, and then a
        # NEW migration that continues from the VERIFIED FIXED BYTES, not from
        # the still-red developer checkout. This is the missing acceptance: the
        # previous tests stop before the repair ever hands off to a migration.
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory(prefix="deploom-repair-adopt-") as tmp:
            project = make_repo_with_failing_check(Path(tmp) / "repo")
            run_dir = Path(tmp) / "run"
            chain = self._repair_chain_to_verified(run_dir, project)

            # -- archive-repair-run: frees the slot transactionally and keeps the
            # repaired C0 snapshot (+ an in-archive copy) as the fixed bytes.
            archived = self._cli(run_dir, "archive-repair-run")
            self.assertEqual(archived.returncode, 0, archived.stdout + archived.stderr)
            payload = self._archive_result(archived.stdout)
            self.assertTrue(payload.get("archived"), archived.stdout)
            archive_dir = Path(str(payload["archiveDir"]))
            self.assertFalse((run_dir / "run.json").exists(), "the repair run slot must be freed")
            self.assertTrue((archive_dir / "run.json").exists(), "run.json archived as evidence")
            self.assertTrue((archive_dir / "checkpoints" / "C0.json").exists(), "C0 proof archived")
            self.assertTrue((archive_dir / "sources" / "C0" / "manifest.json").exists(), "fixed bytes preserved in the archive")
            live_manifest = json.loads((run_dir / "sources" / "C0" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(live_manifest["sourceSnapshotKey"], chain["repairedKey"], "the live C0 must be the repaired bytes")
            handoff = json.loads((run_dir / "repair-handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(handoff["terminal"], "REPAIR_VERIFIED")
            self.assertEqual(handoff["source"]["key"], chain["repairedKey"])
            self.assertFalse(handoff["adopted"])
            # Working references to evidence: every link must still EXIST.
            for ref in (handoff["evidence"]["archiveDir"], handoff["evidence"]["sourceSnapshotCopy"], handoff["evidence"]["reportsDir"], handoff["source"]["container"]):
                self.assertTrue(Path(ref).exists(), f"evidence reference must remain reachable: {ref}")
            project_check = json.loads((run_dir / "project-check.json").read_text(encoding="utf-8"))
            self.assertTrue(project_check["ok"])
            self.assertEqual(project_check["control"]["kind"], "repair")
            self.assertTrue(Path(str(project_check["repair"]["archiveDir"])).exists(), "project-check must carry a living repair ref")

            # -- adopt: the new migration begins from the FIXED source.
            adopted = self._cli(run_dir, "begin", project, extra=["--adopt-repair-source"])
            self.assertEqual(adopted.returncode, 0, adopted.stdout + adopted.stderr)
            self.assertIn("begin.adopted", adopted.stdout)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["phase"], "READY", "the adopted run is ready to migrate from fixed bytes")
            self.assertEqual(run.get("adoptedFromRepairRunId"), chain["repairRunId"])
            c0 = json.loads((run_dir / "checkpoints" / "C0.json").read_text(encoding="utf-8"))
            self.assertEqual(c0["status"], "VERIFIED")
            self.assertEqual(c0["sourceSnapshotKey"], chain["repairedKey"], "C0 IS the repaired source")
            self.assertEqual(c0["verification"]["status"], "passed")
            handoff = json.loads((run_dir / "repair-handoff.json").read_text(encoding="utf-8"))
            self.assertTrue(handoff["adopted"], "handoff consumed exactly once")
            # The adopted C0 snapshot literally holds the FIXED bytes (the fixed
            # test command) — proof the migration runs on repaired sources.
            fixed_pkg = json.loads(
                ((run_dir / "sources" / "C0" / "tree") / (c0.get("projectRelative") or ".") / "package.json").read_text(encoding="utf-8")
            )
            self.assertEqual(fixed_pkg["scripts"]["test"], 'node -e "0"', "adopted C0 must contain the repaired check")
            # The developer checkout was NEVER touched: still red, still clean.
            original = json.loads((project / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(original["scripts"]["test"], 'node -e "process.exit(1)"', "developer checkout stays red")
            self.assertEqual(git(project, "status", "--porcelain").stdout.strip(), "", "developer checkout stays unmodified")
            # double adoption attempts are refused (already a run / already adopted)
            again = self._cli(run_dir, "begin", project, extra=["--adopt-repair-source"])
            self.assertEqual(again.returncode, 2, "a second adoption must be refused")
            self.assertIn("RUN_ALREADY_EXISTS", again.stdout)

    def test_archive_interruption_rolls_back_atomically(self) -> None:
        # P1 (v0.2.163 #2): the user's repro — a move fails halfway through the
        # archive and previously left run.json archived while a checkpoint or
        # the source snapshot was gone. The archive must be a recoverable
        # transaction: on ANY failure every already-moved entry is renamed back,
        # the run stays continuable, and a retry succeeds.
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory(prefix="deploom-repair-rollback-") as tmp:
            project = make_repo_with_failing_check(Path(tmp) / "repo")
            run_dir = Path(tmp) / "run"
            chain = self._repair_chain_to_verified(run_dir, project)
            c0_before = json.loads((run_dir / "checkpoints" / "C0.json").read_text(encoding="utf-8"))
            repair_run_id = chain["repairRunId"]

            real_rename = iterative_migration.os.rename
            calls = {"n": 0}
            fail_after = 2  # 3rd rename fails: some entries already moved

            def flaky_rename(src, dst):
                calls["n"] += 1
                if calls["n"] == fail_after + 1:
                    raise OSError("simulated mid-archive failure")
                return real_rename(src, dst)

            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            iterative_migration.os.rename = flaky_rename  # type: ignore[assignment]
            try:
                with self.assertRaises(iterative_migration.InvalidInputError) as ctx:
                    iterative_migration._archive_repair_run_locked(run_dir, run)
                self.assertIn("REPAIR_ARCHIVE_FAILED_ROLLED_BACK", str(ctx.exception))
            finally:
                iterative_migration.os.rename = real_rename  # type: ignore[assignment]

            # The transaction rolled back: the run is exactly as before the
            # archive — continuable, nothing half-moved, nothing lost.
            self.assertTrue((run_dir / "run.json").exists(), "run.json must be restored")
            restored_run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(restored_run["terminal"], "REPAIR_VERIFIED", "the run keeps its terminal state")
            self.assertEqual(str(restored_run.get("runId")), repair_run_id)
            self.assertTrue((run_dir / "checkpoints" / "C0.json").exists(), "checkpoint C0 must be restored")
            self.assertTrue((run_dir / "run-config.json").exists(), "run-config must be restored")
            manifest = json.loads((run_dir / "sources" / "C0" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["sourceSnapshotKey"], chain["repairedKey"], "the repaired source survives intact")
            self.assertEqual(
                json.loads((run_dir / "checkpoints" / "C0.json").read_text(encoding="utf-8"))["sourceSnapshotKey"],
                c0_before["sourceSnapshotKey"],
            )
            self.assertFalse((run_dir / "repair-handoff.json").exists(), "no handoff may be written for a failed archive")
            # The run is RECOVERABLE: a real retry archives it cleanly.
            retry = self._cli(run_dir, "archive-repair-run")
            self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            payload = self._archive_result(retry.stdout)
            self.assertTrue(payload.get("archived"), retry.stdout)
            self.assertFalse((run_dir / "run.json").exists())
            self.assertTrue(Path(str(payload["archiveDir"])).exists())
            handoff = json.loads((run_dir / "repair-handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(handoff["source"]["key"], chain["repairedKey"])

    def test_recapture_after_repair_supersedes_handoff(self) -> None:
        # P1 (v0.2.163 #1): choosing NOT to adopt (a fresh begin re-captures the
        # developer checkout) must SUPERSEDE the repair handoff — the old fixed
        # bytes must never be silently adopted later. The developer checkout is
        # still red here, so the fresh begin honestly re-opens the repair track.
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory(prefix="deploom-repair-recapture-") as tmp:
            project = make_repo_with_failing_check(Path(tmp) / "repo")
            run_dir = Path(tmp) / "run"
            chain = self._repair_chain_to_verified(run_dir, project)
            archived = self._cli(run_dir, "archive-repair-run")
            self.assertEqual(archived.returncode, 0, archived.stdout + archived.stderr)
            handoff = json.loads((run_dir / "repair-handoff.json").read_text(encoding="utf-8"))
            self.assertFalse(handoff["adopted"])
            self.assertFalse(handoff.get("superseded", False))

            # Fresh begin WITHOUT adoption: re-captures the (still red) developer
            # checkout → BOOTSTRAP_REPAIR again, and the handoff is superseded.
            fresh = self._cli(run_dir, "begin", project)
            self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["phase"], "BOOTSTRAP_REPAIR", "recapture from the still-red checkout opens the repair track")
            fresh_c0 = json.loads((run_dir / "checkpoints" / "C0.json").read_text(encoding="utf-8"))
            self.assertNotEqual(fresh_c0["sourceSnapshotKey"], chain["repairedKey"], "recapture must NOT reuse the fixed bytes")
            handoff = json.loads((run_dir / "repair-handoff.json").read_text(encoding="utf-8"))
            self.assertTrue(handoff.get("superseded"), "a recapture must supersede the repair handoff")
            self.assertEqual(handoff.get("supersededBy"), "recapture")
            # The live fixed source was replaced by the recaptured (red) bytes…
            manifest = json.loads((run_dir / "sources" / "C0" / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotEqual(manifest["sourceSnapshotKey"], chain["repairedKey"])
            # …but the IN-ARCHIVE evidence copy of the fixed bytes is untouched.
            evidence_manifest = json.loads(
                ((Path(str(handoff["evidence"]["sourceSnapshotCopy"])) / "manifest.json").read_text(encoding="utf-8"))
            )
            self.assertEqual(evidence_manifest["sourceSnapshotKey"], chain["repairedKey"], "archive copy of fixed bytes preserved")


if __name__ == "__main__":
    unittest.main()
