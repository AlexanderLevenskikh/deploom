from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import verification_process_supervisor as supervisor
import verification_workspace_backend as workspace


class BlockUVerificationSubstrateTests(unittest.TestCase):
    def test_workspace_backend_is_platform_boundary(self) -> None:
        self.assertTrue(workspace.workspace_backend_summary())

    def test_private_materialization_does_not_alias_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            (source / "payload.txt").write_text("sealed", encoding="utf-8")
            mode = workspace.materialize_private_tree(
                source, target, timeout_seconds=30
            )
            self.assertTrue(mode)
            (target / "payload.txt").write_text("trial", encoding="utf-8")
            self.assertEqual(
                "sealed", (source / "payload.txt").read_text(encoding="utf-8")
            )

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_timeout_kills_descendant_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            script = (
                "import subprocess,time,sys;"
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
                "time.sleep(60)"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                supervisor.run_supervised(
                    [sys.executable, "-c", script],
                    Path(raw),
                    timeout_seconds=1,
                    progress_interval_seconds=1,
                )

    def test_generator_contains_proof_safe_early_screen(self) -> None:
        source = (
            ROOT / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")
        self.assertIn("BLOCK_U_EARLY_PROJECT_SCREEN_V1", source)
        self.assertIn("adaptive-screen-introduced-regression", source)
        self.assertIn("screen_structural_evidence", source)

    def test_baseline_uses_substrate_boundaries(self) -> None:
        source = (
            ROOT / "baseline_constraint_verifier.py"
        ).read_text(encoding="utf-8")
        self.assertIn("run_supervised", source)
        self.assertIn("materialize_private_tree", source)
        self.assertIn("verification substrate:", source)


if __name__ == "__main__":
    unittest.main()
