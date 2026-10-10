from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_tool_tests


class ToolSuiteIsolationTests(unittest.TestCase):
    def relative(self, paths):
        return [path.relative_to(ROOT).as_posix() for path in paths]

    def test_unit_command_does_not_collect_regression_suite(self) -> None:
        files = self.relative(run_tool_tests.files_for("unit"))
        self.assertNotIn("tests/regression/test_prompt_test_contract.py", files)
        self.assertIn("tests/test_dependency_roadmap.py", files)
        self.assertTrue(all(not path.startswith("tests/regression/") for path in files))

    def test_regression_command_collects_only_regression_suite(self) -> None:
        files = self.relative(run_tool_tests.files_for("regression"))
        self.assertIn("tests/regression/test_prompt_test_contract.py", files)
        self.assertNotIn("tests/test_dependency_roadmap.py", files)
        self.assertTrue(all(path.startswith("tests/regression/") for path in files))

    def test_storage_demos_have_a_separate_suite(self) -> None:
        demos = self.relative(run_tool_tests.files_for("storage-delivery"))
        self.assertEqual(demos, ["tests/storage_delivery/test_storage_delivery_demo.py"])
        self.assertGreater(run_tool_tests.load_files(run_tool_tests.files_for("storage-delivery")).countTestCases(), 0)
        for name in ("unit", "regression", "acceptance", "all", "production-fast"):
            with self.subTest(suite=name):
                self.assertTrue(set(demos).isdisjoint(self.relative(run_tool_tests.files_for(name))))

    def test_release_requires_separate_cross_platform_demos(self) -> None:
        import yaml
        workflow = yaml.load((ROOT / ".github/workflows/storage-delivery.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn("workflow_call", workflow["on"])
        self.assertIn("workflow_dispatch", workflow["on"])
        job = workflow["jobs"]["storage-delivery"]
        self.assertEqual(set(job["strategy"]["matrix"]["os"]), {"ubuntu-latest", "windows-latest"})
        self.assertIn("python run_tool_tests.py --suite storage-delivery", [step.get("run") for step in job["steps"]])
        release = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(release["jobs"]["storage-delivery"]["uses"], "./.github/workflows/storage-delivery.yml")
        self.assertIn("storage-delivery", release["jobs"]["publish-release"]["needs"])


if __name__ == "__main__":
    unittest.main()
