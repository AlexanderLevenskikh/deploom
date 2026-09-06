from __future__ import annotations

import ast
import unittest
from pathlib import Path


class FastExactFailurePublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[2]
        cls.generator_path = cls.root / "dependency_live_roadmap_generator.py"
        cls.verifier_path = cls.root / "baseline_constraint_verifier.py"
        cls.generator = cls.generator_path.read_text(encoding="utf-8")
        cls.verifier = cls.verifier_path.read_text(encoding="utf-8")

    def test_targeted_confirmation_inherits_preseal_publication_policy(self) -> None:
        bad = "confirmation_config = dataclasses.replace(config, commands=targeted_commands)"
        self.assertNotIn(bad, self.generator)

        tree = ast.parse(self.generator)
        found_initial_policy = False
        found_inherited_narrowing = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id != "confirmation_config":
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            if not (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "dataclasses"
                and call.func.attr == "replace"
            ):
                continue
            first = call.args[0] if call.args else None
            keywords = {item.arg: item.value for item in call.keywords if item.arg}
            if (
                isinstance(first, ast.Name)
                and first.id == "config"
                and "publish_durable_prepared_artifact" in keywords
            ):
                found_initial_policy = True
            if (
                isinstance(first, ast.Name)
                and first.id == "confirmation_config"
                and isinstance(keywords.get("commands"), ast.Name)
                and keywords["commands"].id == "targeted_commands"
            ):
                found_inherited_narrowing = True

        self.assertTrue(found_initial_policy)
        self.assertTrue(found_inherited_narrowing)

    def test_request_private_snapshot_path_preserves_authoritative_checks(self) -> None:
        for required in (
            "publish_durable_prepared_artifact: bool = True",
            "_durable_prepared_publication_policy(config)",
            "role_allows_durable, publication_reason = (",
            "durable_snapshot_requested = bool(",
            "preparation_publication_allowed and role_allows_durable",
            "shared_reuse_allowed=durable_snapshot_requested",
            "else _same_run_private_prepared_root(trial_parent)",
        ):
            self.assertIn(required, self.verifier)

        self.assertIn(
            "if shared_reuse_allowed and is_durable_prepared_path(published.workspace_root):",
            self.verifier,
        )
        self.assertIn("publish_prepared_artifact_record(", self.verifier)

    def test_confirmation_still_runs_verify_assignment(self) -> None:
        self.assertIn("confirmation = verify_assignment(", self.generator)
        self.assertIn("config=confirmation_config", self.generator)
        self.assertIn("run_project_checks=confirmation_project_checks", self.generator)


if __name__ == "__main__":
    unittest.main()
