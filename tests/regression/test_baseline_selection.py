from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = TOOL_ROOT / "dependency_live_roadmap_generator.py"
SPEC = importlib.util.spec_from_file_location("dependency_roadmap_baseline_test", GENERATOR_PATH)
assert SPEC and SPEC.loader
roadmap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = roadmap
SPEC.loader.exec_module(roadmap)


class BaselineSelectionRegressionTests(unittest.TestCase):
    def test_latest_baseline_is_selected_by_captured_at_not_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            baselines = Path(tmp)
            project_dir = roadmap.baseline_project_dir(baselines, "Demo")
            project_dir.mkdir(parents=True)
            older = {"project": "Demo", "capturedAt": "2026-07-13T12:05:00+00:00", "label": "old"}
            newer = {"project": "Demo", "capturedAt": "2026-07-13T13:05:00+00:00", "label": "new"}
            # Deliberately make lexical filename order contradict capturedAt order.
            (project_dir / "zzz-old.json").write_text(json.dumps(older), encoding="utf-8")
            (project_dir / "aaa-new.json").write_text(json.dumps(newer), encoding="utf-8")
            (project_dir / "zzzz-corrupt.json").write_text("{", encoding="utf-8")

            selected = roadmap.load_latest_baseline(baselines, "Demo")

            self.assertIsNotNone(selected)
            self.assertEqual("new", selected["label"])
            self.assertEqual("2026-07-13T13:05:00+00:00", selected["capturedAt"])

    def test_capture_run_is_marked_as_new_baseline_not_previous_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            (project_dir / "package.json").write_text(
                json.dumps({"dependencies": {"react": "18.2.0"}}), encoding="utf-8"
            )
            health = roadmap.ProjectHealth(
                project="Demo",
                status="red",
                status_rank=0,
                total=1,
                lag_ok_12m=0,
                lag_bad_12m=1,
                lag_ok_pct=0.0,
                critical=0,
                high=0,
                moderate=0,
                low=0,
                unknown=0,
                reason="demo",
            )
            baseline = {
                "project": "Demo",
                "capturedAt": "2026-07-13T13:05:00+00:00",
                "label": "deps-baseline-new-2",
                "health": {
                    "status": "red",
                    "status_rank": 0,
                    "lag_ok_pct": 0.0,
                    "critical": 0,
                    "high": 0,
                    "moderate": 0,
                    "low": 0,
                },
                "directDependencies": roadmap.direct_dependency_snapshot(project_dir),
            }

            comparison = roadmap.build_baseline_comparison(
                "Demo", [], health, baseline, project_dir, captured_this_run=True
            )

            self.assertTrue(comparison["baselineCapturedThisRun"])
            self.assertEqual("red baseline captured", roadmap.comparison_badge_text(comparison))
            note = roadmap.comparison_note(comparison)
            self.assertIn("2026-07-13T13:05:00+00:00", note)
            self.assertIn("deps-baseline-new-2", note)
            self.assertIn("without --capture-baseline", note)


if __name__ == "__main__":
    unittest.main()
