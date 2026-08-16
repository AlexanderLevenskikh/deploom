from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier


class SnapshotCopyProgressHotfixTests(unittest.TestCase):
    def test_timeout_default_and_mapping(self) -> None:
        self.assertEqual(1800, verifier.BaselineVerifyConfig().snapshot_copy_timeout_seconds)
        config = verifier.BaselineVerifyConfig.from_mapping({"snapshotCopyTimeoutSeconds": 2400})
        self.assertEqual(2400, config.snapshot_copy_timeout_seconds)

    def test_robocopy_uses_heartbeat_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            completed = subprocess.CompletedProcess(["robocopy"], 1, stdout="", stderr=None)
            progress = lambda _message: None
            with (
                mock.patch.object(verifier.os, "name", "nt"),
                mock.patch.object(verifier.shutil, "which", return_value="robocopy"),
                mock.patch.object(verifier, "_run_snapshot_copy", return_value=completed) as run,
            ):
                verifier._copy_tree_snapshot(
                    source, target,
                    progress=progress,
                    progress_label="copy-heartbeat",
                    timeout_seconds=1800,
                    progress_interval_seconds=7,
                )
            kwargs = run.call_args.kwargs
            self.assertEqual(1800, kwargs["timeout_seconds"])
            self.assertEqual(7, kwargs["progress_interval_seconds"])
            self.assertIs(progress, kwargs["progress"])
            self.assertEqual("copy-heartbeat", kwargs["progress_label"])

    def test_markers_exist(self) -> None:
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        for marker in (
            "verify.preparation.snapshot-publish.start",
            "snapshot publish started",
            "snapshot publish PASS",
            "verify.project-check.clone.start",
            "verify.project-check.clone.finish",
            "project clone ",
            "snapshot_copy_timeout_seconds",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
