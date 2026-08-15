from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import sys
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = TOOL_ROOT / "dependency_live_roadmap_generator.py"
SPEC = importlib.util.spec_from_file_location("dependency_roadmap_progress_test", GENERATOR_PATH)
assert SPEC and SPEC.loader
roadmap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = roadmap
SPEC.loader.exec_module(roadmap)


class ProgressLoggingRegressionTests(unittest.TestCase):
    def test_project_dependency_progress_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            (project_dir / "package.json").write_text(
                json.dumps({
                    "name": "demo",
                    "dependencies": {
                        "local-one": "file:vendor/one",
                        "local-two": "file:vendor/two",
                    },
                }),
                encoding="utf-8",
            )
            project = roadmap.ProjectSpec("Demo", project_dir)
            stream = io.StringIO()
            with contextlib.redirect_stderr(stream):
                rows = roadmap.analyze_project(
                    project,
                    object(),
                    {},
                    include_prerelease=False,
                    max_candidates=0,
                    progress_prefix="[1/1]",
                )

            output = stream.getvalue()
            self.assertEqual(2, len(rows))
            self.assertIn("[1/1] Demo: dependency analysis started", output)
            self.assertIn("[1/1] [dependency 1/2]", output)
            self.assertIn("[1/1] [dependency 2/2]", output)
            self.assertIn("Demo: dependency analysis completed", output)
            self.assertIn("elapsed=", output)

    def test_generator_has_major_phase_markers(self) -> None:
        source = GENERATOR_PATH.read_text(encoding="utf-8")
        for marker in (
            "target planning started",
            "target planning completed",
            "release intelligence started",
            "release intelligence completed",
            "writing roadmap artifacts",
            "total elapsed=",
            "Baseline localization",
            "Baseline reproduction",
            "baseline-verification-progress.json",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
