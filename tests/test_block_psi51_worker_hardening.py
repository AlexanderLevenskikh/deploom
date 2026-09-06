from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import source_snapshot
from constraint_verify import LocalizationTimeoutError, VerificationUnit, parallel_ddmin


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


class Psi51WorkerUtf8Tests(unittest.TestCase):
    def test_protocol_is_utf8_under_legacy_parent_encoding(self) -> None:
        worker = ROOT / "dependency_live_roadmap_worker.py"
        with tempfile.TemporaryDirectory(prefix="deploom-") as raw:
            cwd = Path(raw) / "кириллица-路径"
            cwd.mkdir()
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "cp1251"
            env["PYTHONUTF8"] = "0"
            process = subprocess.Popen(
                [sys.executable, str(worker)], cwd=str(ROOT), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                ready = process.stdout.readline().decode("utf-8", errors="strict")
                self.assertIn('"encoding":"utf-8"', ready)
                payload = json.dumps(
                    {"id": "utf8", "cwd": str(cwd), "argv": ["--help"], "env": {}},
                    ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                process.stdin.write(payload)
                process.stdin.flush()
                complete = ""
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    line = process.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="strict")
                    if '"type":"complete"' in decoded:
                        complete = decoded
                        break
                self.assertIn('"id":"utf8"', complete)
                self.assertIn('"code":0', complete)
                self.assertIn('"reusable":true', complete)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()

    def test_bad_request_retires_worker(self) -> None:
        worker = ROOT / "dependency_live_roadmap_worker.py"
        process = subprocess.Popen(
            [sys.executable, str(worker)], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None and process.stdin is not None
            process.stdout.readline()
            process.stdin.write(json.dumps(
                {"id": "bad", "cwd": str(ROOT), "argv": ["--definitely-not-a-real-option"], "env": {}},
                separators=(",", ":"),
            ).encode("utf-8") + b"\n")
            process.stdin.flush()
            complete = ""
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="strict")
                if '"type":"complete"' in decoded:
                    complete = decoded
                    break
            self.assertIn('"reusable":false', complete)
            process.wait(timeout=5)
            self.assertNotEqual(0, process.returncode)
        finally:
            if process.poll() is None:
                process.kill()


class Psi51RequestQuiescenceTests(unittest.TestCase):
    def test_screen_timeout_waits_for_started_workers(self) -> None:
        units = tuple(VerificationUnit(name, (name,)) for name in "abcd")
        active = 0
        lock = threading.Lock()

        def fails(_candidate):
            nonlocal active
            with lock:
                active += 1
            try:
                time.sleep(0.16)
                return False
            finally:
                with lock:
                    active -= 1

        with self.assertRaises(LocalizationTimeoutError):
            parallel_ddmin(
                units, fails, parallelism=2, max_checks=2,
                timeout_seconds=0.02, progress_interval_seconds=0.01,
            )
        with lock:
            self.assertEqual(0, active)

    def test_confirmation_timeout_waits_for_started_worker(self) -> None:
        units = tuple(VerificationUnit(name, (name,)) for name in "ab")
        active = 0
        lock = threading.Lock()

        def confirm(_candidate):
            nonlocal active
            with lock:
                active += 1
            try:
                time.sleep(0.16)
                return True
            finally:
                with lock:
                    active -= 1

        with self.assertRaises(LocalizationTimeoutError):
            parallel_ddmin(
                units, lambda _candidate: True, confirm_failure=confirm,
                parallelism=1, max_checks=2,
                timeout_seconds=0.02, progress_interval_seconds=0.01,
            )
        with lock:
            self.assertEqual(0, active)


class Psi51LiveSourceContinuityTests(unittest.TestCase):
    def test_ignored_semantic_file_change_rejects_hot_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deploom-source-") as raw:
            root = Path(raw)
            (root / "package.json").write_text(
                '{"name":"psi51-fixture","version":"1.0.0"}\n',
                encoding="utf-8",
            )
            (root / ".gitignore").write_text(".env\n", encoding="utf-8")
            _git(root, "init")
            _git(root, "config", "user.email", "psi51@example.invalid")
            _git(root, "config", "user.name", "Psi51")
            _git(root, "add", "package.json", ".gitignore")
            _git(root, "commit", "-m", "fixture")
            (root / ".env").write_text("VALUE=one\n", encoding="utf-8")
            self.assertEqual("", _git(root, "status", "--porcelain"))

            snapshot = source_snapshot.activate_source_snapshot_epoch(
                root, replace=True, timeout_seconds=60
            )
            self.assertTrue(source_snapshot.source_snapshot_live_source_continuity(
                snapshot, timeout_seconds=60
            ))

            (root / ".env").write_text("VALUE=two\n", encoding="utf-8")
            self.assertEqual(snapshot.git_head, _git(root, "rev-parse", "HEAD"))
            self.assertEqual("", _git(root, "status", "--porcelain"))
            self.assertFalse(source_snapshot.source_snapshot_live_source_continuity(
                snapshot, timeout_seconds=60
            ))


class Psi51DesktopContractTests(unittest.TestCase):
    def test_node_uses_streaming_utf8_decoder(self) -> None:
        source = (ROOT / "desktop" / "electron" / "baseline-worker.ts").read_text(encoding="utf-8")
        self.assertIn("PYTHONUTF8: '1'", source)
        self.assertIn("PYTHONIOENCODING: 'utf-8'", source)
        self.assertIn("child.stdout.setEncoding('utf8')", source)
        self.assertIn("child.stderr.setEncoding('utf8')", source)
        self.assertNotIn("decodeProcessOutputChunk", source)


if __name__ == "__main__":
    unittest.main()
