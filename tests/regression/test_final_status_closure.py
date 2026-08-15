from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import validate_dependency_update as validator


class FinalStatusClosureRegression(unittest.TestCase):
    def test_red_final_roadmap_exposes_new_closure_actions(self) -> None:
        original = {
            "project_health": {"Demo.App": {"status": "red"}},
            "projects": {"Demo.App": []},
        }
        final = {
            "project_health": {"Demo.App": {"status": "red", "lag_ok_pct": 77.8}},
            "projects": {
                "Demo.App": [
                    {
                        "name": "typescript",
                        "current_version": "5.8.3",
                        "target_default": "5.9.2",
                        "target_yellow": "5.9.2",
                        "target_green": "6.0.2",
                    }
                ]
            },
        }
        findings, meta = validator.validate_final_status(original, final, "Demo.App", "default")
        codes = [item["code"] for item in findings]
        self.assertIn("target-status-not-reached", codes)
        self.assertIn("closure-action-remains", codes)
        self.assertEqual("yellow", meta["expectedStatus"])
        self.assertIn("typescript->5.9.2", meta["remainingActions"])

    def test_yellow_final_status_closes_default_red_to_yellow_run(self) -> None:
        original = {"project_health": {"Demo": {"status": "red"}}, "projects": {"Demo": []}}
        final = {"project_health": {"Demo": {"status": "yellow"}}, "projects": {"Demo": []}}
        findings, meta = validator.validate_final_status(original, final, "Demo", "default")
        self.assertEqual([], findings)
        self.assertEqual("yellow", meta["actualStatus"])

    def test_reached_status_still_fails_when_yellow_action_remains(self) -> None:
        original = {"project_health": {"Demo": {"status": "red"}}, "projects": {"Demo": []}}
        final = {
            "project_health": {"Demo": {"status": "yellow"}},
            "projects": {
                "Demo": [{
                    "name": "vite-plugin-mkcert",
                    "current_version": "1.17.5",
                    "target_default": "2.0.0",
                    "target_yellow": "1.17.10",
                    "target_green": "2.0.0",
                }]
            },
        }
        findings, meta = validator.validate_final_status(original, final, "Demo", "default")
        self.assertNotIn("target-status-not-reached", [item["code"] for item in findings])
        self.assertIn("closure-action-remains", [item["code"] for item in findings])
        self.assertEqual(["vite-plugin-mkcert->1.17.10"], meta["remainingActions"])


if __name__ == "__main__":
    unittest.main()
