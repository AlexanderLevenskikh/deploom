from __future__ import annotations
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]

class DesktopUiLifecyclePolishRegressionTests(unittest.TestCase):
    def test_project_removal_is_nondestructive_and_guarded(self) -> None:
        main = (ROOT / "desktop/electron/main.ts").read_text(encoding="utf-8")
        self.assertIn("flow:remove-project", main)
        self.assertIn("settings.projects = remaining", main)
        self.assertNotIn("rmSync(project.path", main)

    def test_model_is_persisted_before_manual_or_autopilot_launch(self) -> None:
        flow = (ROOT / "desktop/src/components/FlowWorkspace.tsx").read_text(encoding="utf-8")
        self.assertIn("await persistAgentModel()", flow)
        self.assertIn("startAutopilotWithCurrentModel", flow)

    def test_actual_agent_binding_is_logged(self) -> None:
        main = (ROOT / "desktop/electron/main.ts").read_text(encoding="utf-8")
        self.assertIn("Agent execution binding:", main)
        self.assertIn("spec.args.indexOf('--model')", main)

if __name__ == "__main__":
    unittest.main()
