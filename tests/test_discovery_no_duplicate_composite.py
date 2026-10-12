"""Regression tests for safe discovery of composite lint commands."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baseline_constraint_verifier import discover_baseline_project_checks


class CompositeLintDiscoveryTests(unittest.TestCase):
    def discover(self, scripts):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "package.json").write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
            with patch("baseline_constraint_verifier.detect_package_manager", return_value="yarn"):
                return discover_baseline_project_checks(root)

    def test_logan_runs_single_composite_in_original_order(self):
        commands = self.discover({
            "lint:types": "tsc", "lint:scripts": "eslint .",
            "lint:styles": "stylelint .",
            "lint": "yarn lint:types && yarn lint:scripts && yarn lint:styles",
            "build": "webpack",
        })
        self.assertEqual(("yarn lint", "yarn build"), commands)

    def test_additional_stage_preserves_both_checks(self):
        commands = self.discover({
            "lint:types": "tsc", "lint:scripts": "eslint .",
            "lint": "yarn lint:types && yarn lint:scripts && yarn audit-custom",
        })
        self.assertIn("yarn lint", commands)
        self.assertIn("yarn lint:scripts", commands)

    def test_redirection_disallows_dedup(self):
        commands = self.discover({
            "lint:types": "tsc", "lint:scripts": "eslint .",
            "lint": "yarn lint:types && yarn lint:scripts > output.txt",
        })
        self.assertIn("yarn lint", commands)
        self.assertIn("yarn lint:scripts", commands)

    def test_partial_composite_keeps_remaining_checks(self):
        commands = self.discover({
            "lint:types": "tsc", "lint:scripts": "eslint .", "lint:styles": "stylelint .",
            "lint": "yarn lint:types && yarn lint:scripts",
        })
        self.assertIn("yarn lint:styles", commands)
        self.assertIn("yarn lint", commands)

    def test_single_alias_keeps_previous_focused_check(self):
        commands = self.discover({"lint:types": "tsc", "lint": "tsc"})
        self.assertEqual(("yarn lint:types",), commands)

    def test_build_and_test_are_never_removed(self):
        commands = self.discover({
            "lint:types":"tsc", "lint:scripts":"eslint .",
            "lint":"yarn lint:types && yarn lint:scripts",
            "build":"webpack", "test":"jest",
        })
        self.assertEqual(("yarn lint", "yarn build", "yarn test"),commands)


if __name__ == "__main__":
    unittest.main()
