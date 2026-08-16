from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from types import SimpleNamespace

import dependency_live_roadmap_generator as roadmap
from baseline_constraint_verifier import BaselineProjectFailure, BaselineVerifyConfig, BaselineVerifyResult, _apply_assignment
from constraint_verify import VerificationUnit


class BaselineLocalizationContractTests(unittest.TestCase):
    def test_unchanged_context_literal_is_not_part_of_reproduction_materialization(self) -> None:
        rows = {
            "plugin": SimpleNamespace(current_version="1.0.0"),
            "vite": SimpleNamespace(current_version="4.3.9"),
        }
        assignment = {
            "plugin": "2.0.0",
            # An unchanged current companion is intentionally part of the unit:
            # the learned clause may need NOT(plugin@2 + vite@4.3.9).
            "vite": "4.3.9",
        }
        unit = VerificationUnit("interaction", ("plugin", "vite"))

        materialization, clause = roadmap._verification_inputs_for_units(
            assignment, rows, (unit,)
        )

        self.assertEqual({"plugin": "2.0.0"}, materialization)
        self.assertEqual({"plugin": "2.0.0", "vite": "4.3.9"}, clause)
    def test_full_verification_assignment_keeps_unchanged_direct_versions_exact(self) -> None:
        rows = {
            "plugin": SimpleNamespace(current_version="1.0.0"),
            "vite": SimpleNamespace(current_version="4.3.9"),
        }
        assignment = {"plugin": "2.0.0", "vite": "4.3.9"}
        full = roadmap._verification_assignment(assignment, rows)
        delta = roadmap._changed_assignment(assignment, rows)
        self.assertEqual({"plugin": "2.0.0", "vite": "4.3.9"}, full)
        self.assertEqual({"plugin": "2.0.0"}, delta)



    def test_solver_context_clause_would_change_a_ranged_manifest_but_materialization_does_not(self) -> None:
        rows = {
            "plugin": SimpleNamespace(current_version="1.0.0"),
            "vite": SimpleNamespace(current_version="4.3.9"),
        }
        assignment = {"plugin": "2.0.0", "vite": "4.3.9"}
        unit = VerificationUnit("interaction", ("plugin", "vite"))
        materialization, clause = roadmap._verification_inputs_for_units(
            assignment, rows, (unit,)
        )

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            package_json = project / "package.json"
            original = {
                "devDependencies": {
                    "plugin": "^1.0.0",
                    "vite": "^4.0.0",
                }
            }
            package_json.write_text(json.dumps(original), encoding="utf-8")

            _apply_assignment(project, materialization)
            reproduced = json.loads(package_json.read_text(encoding="utf-8"))
            self.assertEqual("^4.0.0", reproduced["devDependencies"]["vite"])

            package_json.write_text(json.dumps(original), encoding="utf-8")
            _apply_assignment(project, clause)
            old_bug = json.loads(package_json.read_text(encoding="utf-8"))
            self.assertEqual(
                "4.3.9",
                old_bug["devDependencies"]["vite"],
                "The solver clause is deliberately richer than the probe delta; "
                "using it for reproduction changes the experiment.",
            )

    def test_combined_preflight_uses_full_verification_assignment(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn(
            "combined = verify_assignment(\n"
            "                            spec.path, verification_assignment, config=config, run_project_checks=True",
            source,
        )
        self.assertNotIn(
            "combined = verify_assignment(\n"
            "                            spec.path, changed, config=config, run_project_checks=True",
            source,
        )


    def test_adaptive_confirmation_runs_only_structurally_responsible_commands(self) -> None:
        result = BaselineVerifyResult(
            False,
            "project",
            "project preflight failed",
            project_failures=(
                BaselineProjectFailure(
                    "yarn lint:types",
                    2,
                    "TS2307 Cannot find module '@vitejs/plugin-react' and could not be resolved under your current moduleResolution setting",
                ),
                BaselineProjectFailure(
                    "yarn lint:scripts",
                    1,
                    "src/App.tsx: ordinary eslint migration debt",
                ),
            ),
        )
        config = BaselineVerifyConfig(
            project_checks="adaptive",
            commands=("yarn lint:types", "yarn lint:scripts", "yarn build"),
        )
        self.assertEqual(
            ("yarn lint:types",),
            roadmap._targeted_adaptive_confirmation_commands(result, config),
        )


    def test_graph_guided_candidate_exactifies_interaction_context(self) -> None:
        rows = {
            "plugin": SimpleNamespace(current_version="1.0.0"),
            "vite": SimpleNamespace(current_version="4.3.9"),
            "eslint": SimpleNamespace(current_version="8.0.0"),
        }
        assignment = {
            "plugin": "2.0.0",
            "vite": "4.3.9",
            "eslint": "9.0.0",
        }
        units = (
            VerificationUnit("plugin-vite", ("plugin", "vite")),
            VerificationUnit("eslint", ("eslint",)),
        )
        candidate = roadmap._select_graph_guided_candidate_clause(
            assignment,
            rows,
            units,
            {"plugin"},
        )
        self.assertEqual(
            {"plugin": "2.0.0", "vite": "4.3.9"},
            candidate,
        )

    def test_graph_guided_candidate_is_diagnostic_until_certified(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn('EVIDENCE_DIAGNOSTIC_HINT = "DIAGNOSTIC_HINT"', source)
        self.assertIn('EVIDENCE_CONFIRMED_CONSTRAINT = "CONFIRMED_CONSTRAINT"', source)
        self.assertIn("global_exact_exclusions[project][mode].append(exact_nogood)", source)
        self.assertNotIn(
            "merge_nogood_edges(graph, global_exact_exclusions",
            source,
        )



if __name__ == "__main__":
    unittest.main()