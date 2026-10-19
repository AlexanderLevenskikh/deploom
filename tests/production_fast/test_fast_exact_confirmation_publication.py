from __future__ import annotations

import ast
import dataclasses
from types import SimpleNamespace
from pathlib import Path
import unittest

import baseline_constraint_verifier as verifier


class FastExactFailurePublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Evaluate the real configuration callsites with actual config values.
        # Renaming formatting or comments cannot satisfy this contract.
        path = Path(__file__).resolve().parents[2] / "dependency_live_roadmap_generator.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls.calls = [node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "confirmation_config" for target in node.targets)]
        cls.path = str(path)
        cls.confirmations = [node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "confirmation" for target in node.targets)
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "verify_assignment"]

    def test_targeted_confirmation_inherits_preseal_publication_policy(self):
        context = {"dataclasses": dataclasses, "config": verifier.BaselineVerifyConfig(
            publish_durable_prepared_artifact=True, recovery_checkpoints=True, commands=("node lint.cjs", "node test.cjs")),
            "targeted_commands": ("node test.cjs",)}
        self.assertEqual(2, len(self.calls))
        for call in sorted(self.calls, key=lambda node: node.lineno):
            context["confirmation_config"] = eval(compile(ast.Expression(call), self.path, "eval"), context)
            self.assertFalse(context["confirmation_config"].publish_durable_prepared_artifact)
            self.assertEqual("exact-failure-confirmation", context["confirmation_config"].verification_purpose)
        self.assertEqual(("node test.cjs",), context["confirmation_config"].commands)
        self.assertEqual(("node lint.cjs", "node test.cjs"), context["config"].commands)
        # Retain the old confirmation-call invariant, now checking the actual
        # arguments evaluated at the callsite rather than a source substring.
        self.assertEqual(1, len(self.confirmations))
        received = []
        sentinel = object()
        def verifier_boundary(project, assignment, **kwargs):
            received.append((project, assignment, kwargs))
            return sentinel
        context.update(spec=SimpleNamespace(path="fixture"), verification_assignment={"a": "2"},
            verify_assignment=verifier_boundary, _with_candidate_deadline=lambda value, deadline: value,
            candidate_phase_deadline=0, removals=(), mode="fixture", fingerprint="assignment")
        for selected in (False, True):
            context["confirmation_project_checks"] = selected
            self.assertIs(sentinel, eval(compile(ast.Expression(self.confirmations[0]), self.path, "eval"), context))
            project, assignment, kwargs = received[-1]
            self.assertEqual(("fixture", {"a": "2"}), (project, assignment))
            self.assertIs(kwargs["config"], context["confirmation_config"])
            self.assertIs(selected, kwargs["run_project_checks"])

    def test_intermediate_role_never_publishes_even_with_recovery_enabled(self):
        for purpose in ("intermediate-candidate", "exact-failure-confirmation", "diagnostic-probe"):
            for recovery in (False, True):
                for requested in (False, True):
                    with self.subTest(purpose=purpose, recovery=recovery, requested=requested):
                        config = verifier.BaselineVerifyConfig(verification_purpose=purpose,
                            recovery_checkpoints=recovery, publish_durable_prepared_artifact=requested)
                        allowed, _ = verifier._durable_prepared_publication_policy(config)
                        self.assertFalse(allowed)

    def test_final_roles_keep_explicit_publication_policy(self):
        for purpose in ("baseline-control", "incumbent-promotion"):
            for requested in (False, True):
                with self.subTest(purpose=purpose, requested=requested):
                    config = verifier.BaselineVerifyConfig(verification_purpose=purpose,
                        publish_durable_prepared_artifact=requested)
                    self.assertEqual(requested, verifier._durable_prepared_publication_policy(config)[0])


if __name__ == "__main__":
    unittest.main()
