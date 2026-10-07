from __future__ import annotations
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import iterative_migration as migration
from iterative_resume import reopen_terminal_run
from source_snapshot import capture_durable_source_snapshot

spec = importlib.util.spec_from_file_location("resume_fixture", Path(__file__).with_name("test_iterative_finish_baseline_and_feedback_validation.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
REPO = Path(__file__).resolve().parents[2]


class ResumeTests(unittest.TestCase):
    def setup_run(self, root):
        project = root / "project"
        project.mkdir()
        (project / "package.json").write_text('{"name":"resume-demo","version":"1.0.0","private":true}', encoding="utf-8")
        snapshot = capture_durable_source_snapshot(project, root / "snapshot", timeout_seconds=30)
        run = fixture._RunDir(root / "run")
        run.write_config(fixture._config_dict(budget={"runBudgetMinutes": 0, "maxInfraRetries": 2}))
        run.write_checkpoint(fixture._checkpoint_dict(checkpointId="C4", seq=4,
            sourceSnapshotKey=snapshot.key, sourceSnapshotContainer=str(snapshot.container)))
        run.write_run(fixture._run_dict(phase="TERMINAL", terminal="BLOCKED_BASELINE",
            activeCheckpointId="C4", terminalOutcome={"outcome":"BLOCKED_BASELINE"},
            auditRecovery={"C4:scope":{"attempts":3}}, attempts=17))
        (run.root / "ledger.json").write_text('{"nogoods":[{"pkg-a":"2.0.0"}]}', encoding="utf-8")
        return run, snapshot

    def cli(self, root):
        return subprocess.run([sys.executable, str(REPO / "iterative_migration.py"),
            "--run-dir", str(root), "resume"], cwd=REPO, capture_output=True, text=True, encoding="utf-8")

    def test_real_cli_preserves_checkpoint_constraints_and_spent_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, snapshot = self.setup_run(Path(tmp))
            files = [run.root / "run-config.json", run.root / "checkpoints/C4.json", run.root / "ledger.json"]
            before = [p.read_bytes() for p in files]
            result = self.cli(run.root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = migration.load_run(run.root)
            self.assertEqual(state["activeCheckpointId"], "C4")
            self.assertEqual(state["phase"], "READY")
            self.assertIsNone(state["terminalOutcome"])
            self.assertEqual(state["attempts"], 17)
            self.assertEqual(state["auditRecovery"], {"C4:scope":{"attempts":3}})
            self.assertEqual(state["continuations"][0]["terminalOutcome"], {"outcome":"BLOCKED_BASELINE"})
            self.assertEqual(before, [p.read_bytes() for p in files])
            self.assertEqual(self.cli(run.root).returncode, 0)
            self.assertEqual(len(migration.load_run(run.root)["continuations"]), 1)

    def test_mutated_source_fails_closed_and_keeps_terminal_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, snapshot = self.setup_run(Path(tmp))
            target = snapshot.root / "package.json"
            target.chmod(0o600)
            target.write_text('{"changed":true}', encoding="utf-8")
            before = (run.root / "run.json").read_bytes()
            self.assertNotEqual(self.cli(run.root).returncode, 0)
            self.assertEqual((run.root / "run.json").read_bytes(), before)

    def test_complete_unverified_and_inflight_states_are_rejected(self):
        checkpoint = fixture._checkpoint_dict()
        for run, cp, candidate in (
            (fixture._run_dict(phase="TERMINAL", terminal="COMPLETE"), checkpoint, None),
            (fixture._run_dict(phase="TERMINAL", terminal="BLOCKED_BASELINE"), {**checkpoint,"status":"UNKNOWN"}, None),
            (fixture._run_dict(phase="TERMINAL", terminal="BLOCKED_BASELINE"), checkpoint, {"stage":"VERIFYING"}),
        ):
            with self.assertRaises(ValueError):
                reopen_terminal_run(run, cp, candidate)

    def test_expired_run_deadline_is_not_reset_by_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, _ = self.setup_run(Path(tmp))
            run.write_config(fixture._config_dict(budget={"runBudgetMinutes":1}))
            state = migration.load_run(run.root)
            migration.save_run(run.root, {**state, "deadlineAt": "2000-01-01T00:00:00Z"})
            before = (run.root / "run.json").read_bytes()
            result = self.cli(run.root)
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertIn("RUN_DEADLINE_EXCEEDED", result.stdout)
            self.assertEqual((run.root / "run.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
