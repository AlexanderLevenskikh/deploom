"""Identical script bodies do not erase distinct package-manager hooks."""
import json
from pathlib import Path
import tempfile
import unittest
from baseline_constraint_verifier import discover_baseline_project_checks

class CompositeLintLifecycleTests(unittest.TestCase):
    def test_plain_lint_hooks_prevent_alias_deduplication(self):
        for hook in ("prelint","postlint"):
            with self.subTest(hook=hook),tempfile.TemporaryDirectory() as raw:
                root=Path(raw)
                (root/"package.json").write_text(json.dumps({"packageManager":"npm@10.0.0","scripts":{"lint":"node lint.cjs","lint:scripts":"node lint.cjs",hook:"node extra.cjs"}}))
                self.assertEqual(("npm run lint:scripts","npm run lint"),discover_baseline_project_checks(root))

    def test_test_alias_hooks_are_not_lost(self):
        for hook in ("pretest","posttest"):
            with self.subTest(hook=hook),tempfile.TemporaryDirectory() as raw:
                root=Path(raw)
                (root/"package.json").write_text(json.dumps({"scripts":{"test":"node test.cjs","test:unit":"node test.cjs",hook:"node extra.cjs"}}))
                self.assertEqual(("npm run test:unit","npm run test"),discover_baseline_project_checks(root))

    def test_composite_keeps_hooks_and_extra_stage_in_selected_context(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);app=root/"app";app.mkdir()
            (root/"package.json").write_text(json.dumps({"scripts":{"build":"node wrong.cjs"}}))
            (app/"package.json").write_text(json.dumps({"scripts":{"lint:types":"node types.cjs","lint:scripts":"node lint.cjs","lint":"npm run lint:types && npm run lint:scripts && node extra.cjs","postlint":"node hook.cjs"}}))
            self.assertEqual(("npm run lint:types","npm run lint:scripts","npm run lint"),discover_baseline_project_checks(app))

if __name__=="__main__":unittest.main()
