from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _indent(line: str) -> str:
    stripped = line.lstrip(" \t")
    return line[: len(line) - len(stripped)]


class Psi592DirectHandoffContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "dependency_live_roadmap_generator.py"
        cls.generator = cls.path.read_text(encoding="utf-8")
        cls.marker = cls.generator.index(
            "# BLOCK_PSI592_DIRECT_HANDOFF_CONTEXT_V1"
        )
        cls.end = cls.generator.index(
            "if direct_cohort_plan.actionable:",
            cls.marker,
        )
        cls.block = cls.generator[cls.marker : cls.end]

    def test_context_is_built_before_direct_plan(self):
        context = self.block.index(
            "direct_navigation_graph, "
            "direct_subject_consumers = ("
        )
        plan = self.block.index(
            "direct_cohort_plan = plan_cohort_handoff("
        )
        self.assertLess(context, plan)
        self.assertIn(
            "_baseline_cohort_navigation_context(",
            self.block,
        )

    def test_direct_plan_uses_dedicated_context_only(self):
        self.assertIn(
            "subject_consumers=direct_subject_consumers,",
            self.block,
        )
        self.assertIn(
            "interaction_graph=direct_navigation_graph,",
            self.block,
        )
        self.assertNotIn(
            "subject_consumers=subject_consumers,",
            self.block,
        )
        self.assertNotIn(
            "interaction_graph=navigation_graph,",
            self.block,
        )

    def test_context_is_bound_to_current_assignment(self):
        self.assertIn("rows_by_name,", self.block)
        self.assertIn("verification_assignment,", self.block)
        self.assertIn("client,", self.block)

    def test_context_and_plan_have_same_source_indentation(self):
        lines = self.block.splitlines()
        context_line = next(
            line
            for line in lines
            if (
                "direct_navigation_graph, "
                "direct_subject_consumers = ("
            )
            in line
        )
        plan_line = next(
            line
            for line in lines
            if "direct_cohort_plan = plan_cohort_handoff("
            in line
        )
        self.assertEqual(
            _indent(context_line),
            _indent(plan_line),
        )

    def test_full_generator_ast_is_valid(self):
        ast.parse(
            self.generator,
            filename="dependency_live_roadmap_generator.py",
        )

    def test_ast_context_precedes_direct_plan(self):
        tree = ast.parse(self.generator)
        context_lines = []
        plan_lines = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue

            names = {
                child.id
                for target in node.targets
                for child in ast.walk(target)
                if isinstance(child, ast.Name)
            }

            if {
                "direct_navigation_graph",
                "direct_subject_consumers",
            }.issubset(names):
                context_lines.append(node.lineno)

            if (
                "direct_cohort_plan" in names
                and isinstance(node.value, ast.Call)
            ):
                func = node.value.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "plan_cohort_handoff"
                ):
                    plan_lines.append(node.lineno)

        self.assertEqual(1, len(context_lines))
        self.assertEqual(1, len(plan_lines))
        self.assertLess(
            context_lines[0],
            plan_lines[0],
        )

    def test_projected_transitive_contract_remains_present(self):
        self.assertIn(
            "navigation_graph, subject_consumers = (",
            self.generator,
        )
        self.assertGreaterEqual(
            self.generator.count("plan_cohort_handoff("),
            2,
        )


if __name__ == "__main__":
    unittest.main()
