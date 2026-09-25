from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dependency_live_roadmap_generator import BaselineProgressReporter  # noqa: E402


def _progress_payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class BaselineProgressRunScopingTests(unittest.TestCase):
    """Baseline progress is scoped to the current run (runId/startedAt)."""

    def test_begin_run_rotates_previous_run_to_history_and_stamps_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".dependency-roadmap" / "state" / "baseline-verification-progress.json"
            old = BaselineProgressReporter(
                path, run_id="old-fast-run", started_at="2026-09-24T22:00:00+00:00"
            )
            old.emit("Demo", "yellow", "budget-exhausted", elapsedSeconds="903.933")

            started_at = "2026-09-25T10:00:00+00:00"
            reporter = BaselineProgressReporter(path)
            reporter.begin_run("Demo", "yellow", "run-new", started_at=started_at)

            current = _progress_payload(path)
            self.assertEqual("run-started", current["phase"])
            self.assertEqual("run-new", current["runId"])
            self.assertEqual(started_at, current["startedAt"])

            history = list(path.parent.glob(path.name + ".previous-*"))
            self.assertEqual(1, len(history), history)
            prior = _progress_payload(history[0])
            self.assertEqual("budget-exhausted", prior["phase"])
            self.assertEqual("old-fast-run", prior["runId"])

    def test_emits_carry_run_identity_of_the_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "progress.json"
            reporter = BaselineProgressReporter(path)
            reporter.begin_run("Demo", "yellow", "deep-run", started_at="2026-09-25T10:00:00+00:00")
            reporter.emit("Demo", "yellow", "localization-heartbeat", checksStarted=4)
            payload = _progress_payload(path)
            self.assertEqual("deep-run", payload["runId"])
            self.assertEqual("2026-09-25T10:00:00+00:00", payload["startedAt"])
            self.assertEqual("localization-heartbeat", payload["phase"])

    def test_preflight_failed_is_terminal_and_not_overwritten_by_late_wave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "progress.json"
            reporter = BaselineProgressReporter(path)
            reporter.begin_run("Demo", "yellow", "deep-run")
            reporter.emit(
                "Demo", "yellow", "preflight-failed",
                code="SOURCE_CHECKOUT_DIRTY",
                message="SOURCE_CHECKOUT_DIRTY: Demo: commit/stash/remove changes "
                        "before generation: M package.json",
            )
            reporter.emit("Demo", "yellow", "localization-check-running", check=9)
            payload = _progress_payload(path)
            self.assertEqual("preflight-failed", payload["phase"])
            self.assertEqual("SOURCE_CHECKOUT_DIRTY", payload["code"])
            self.assertIn("M package.json", payload["message"])

    def test_same_run_begin_does_not_rotate_its_own_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "progress.json"
            reporter = BaselineProgressReporter(path)
            reporter.begin_run("Demo", "yellow", "run-a")
            reporter.emit("Demo", "yellow", "localization-started", checksStarted=0)
            reporter.begin_run("Demo", "yellow", "run-a")
            payload = _progress_payload(path)
            self.assertEqual("run-started", payload["phase"])
            self.assertEqual("run-a", payload["runId"])
            self.assertEqual(0, len(list(path.parent.glob(path.name + ".previous-*"))))


@unittest.skipUnless(shutil.which("git"), "git is required")
class SourceCheckoutDirtyCliTests(unittest.TestCase):
    """Real CLI: SOURCE_CHECKOUT_DIRTY preflight is scoped to the current run."""

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "Demo"
        self.project.mkdir()
        for args in (
            ("init",),
            ("config", "user.name", "Roadmap Test"),
            ("config", "user.email", "roadmap@example.test"),
            ("checkout", "-b", "master"),
        ):
            result = self.git(self.project, *args)
            if result.returncode != 0:
                self.fail(f"git {' '.join(args)} failed: {result.stderr}")
        (self.project / "package.json").write_text(
            '{"name":"demo","version":"1.0.0"}\n', encoding="utf-8"
        )
        self.git(self.project, "add", "package.json")
        self.git(self.project, "commit", "-m", "initial")
        (self.project / "package.json").write_text(
            '{"name":"demo","version":"1.0.0-dirty"}\n', encoding="utf-8"
        )
        settings = self.root / "settings.project.json"
        settings.write_text(json.dumps({
            "root": str(self.root),
            "sourceCheckoutGuard": True,
            "projects": [{"name": "Demo", "path": "Demo", "sourceBranch": "master"}],
            "out": "artifacts/report.md",
            "jsonOut": "artifacts/report.json",
            "htmlOut": "artifacts/report.html",
            "historyDir": "history",
            "dashboardState": "state.json",
            "releaseIntelEnabled": False,
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(ROOT / "dependency_live_roadmap_generator.py"),
                "--project-settings", str(self.root / "settings.project.json"),
                "--only-project", "Demo",
                "--skip-release-intel",
                "--no-history-snapshot",
                "--capture-baseline",
            ],
            cwd=self.root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    @property
    def progress_path(self) -> Path:
        return self.root / ".dependency-roadmap" / "state" / "baseline-verification-progress.json"

    def seed_old_fast_progress(self) -> None:
        # Persist what an earlier FAST run's budget-exhausted phase would look
        # like, exactly like the production repro: stale state predating the
        # Deep run's preflight stop.
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        self.progress_path.write_text(json.dumps({
            "schemaVersion": 1,
            "project": "Demo",
            "mode": "yellow",
            "phase": "budget-exhausted",
            "runId": "old-fast-run",
            "startedAt": "2026-09-24T22:00:00+00:00",
            "updatedAt": "2026-09-24T22:15:00+00:00",
            "elapsedSeconds": "903.933",
            "candidatesAttempted": 1,
        }) + "\n", encoding="utf-8")

    def test_dirty_checkout_preflight_fails_once_and_scopes_progress_to_current_run(self) -> None:
        self.seed_old_fast_progress()
        result = self.run_cli()

        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("SOURCE_CHECKOUT_DIRTY", result.stderr)
        self.assertIn("package.json", result.stderr)
        # It is a deterministic preflight rejection, not a search/budget failure.
        self.assertNotIn("budget-exhausted", result.stderr)

        current = _progress_payload(self.progress_path)
        self.assertEqual("preflight-failed", current["phase"])
        self.assertEqual("SOURCE_CHECKOUT_DIRTY", current["code"])
        self.assertEqual("Demo", current["project"])
        self.assertNotEqual("old-fast-run", current["runId"])
        self.assertIn("M package.json", current["message"])

        # The previous run's budget-exhausted diagnostics stay readable in
        # history and are NOT the current outcome.
        history = list(self.progress_path.parent.glob(self.progress_path.name + ".previous-*"))
        self.assertEqual(1, len(history), history)
        prior = _progress_payload(history[0])
        self.assertEqual("budget-exhausted", prior["phase"])
        self.assertEqual("old-fast-run", prior["runId"])

    def test_second_run_gets_its_own_identity_and_keeps_history(self) -> None:
        first = self.run_cli()
        self.assertEqual(2, first.returncode, first.stderr)
        first_run_id = _progress_payload(self.progress_path)["runId"]

        second = self.run_cli()
        self.assertEqual(2, second.returncode, second.stderr)

        current = _progress_payload(self.progress_path)
        self.assertEqual("preflight-failed", current["phase"])
        self.assertNotEqual(first_run_id, current["runId"])
        history = list(self.progress_path.parent.glob(self.progress_path.name + ".previous-*"))
        saved_run_ids = {
            _progress_payload(item).get("runId") for item in history
        }
        self.assertIn(first_run_id, saved_run_ids)
        self.assertEqual(1, len(history), history)


if __name__ == "__main__":
    unittest.main()
