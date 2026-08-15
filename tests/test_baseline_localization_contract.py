from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import dependency_live_roadmap_generator as roadmap
from baseline_constraint_verifier import _apply_assignment
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


if __name__ == "__main__":
    unittest.main()