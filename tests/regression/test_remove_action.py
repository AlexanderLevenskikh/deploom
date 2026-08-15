from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import validate_dependency_update as validator


class RemoveActionRegressionTests(unittest.TestCase):
    def test_removed_deprecated_types_stub_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(json.dumps({"devDependencies": {}}), encoding="utf-8")
            direct = validator.direct_dependencies(json.loads((project / "package.json").read_text(encoding="utf-8")))
            row = {
                "name": "@types/uuid", "kind": "dev", "section": "devDependencies",
                "target_yellow": "11.0.0", "requested_spec": "9.0.8", "action": "remove",
            }
            self.assertEqual([], validator.compare_package(project, direct, row, "yellow", True))

    def test_remove_action_fails_while_direct_declaration_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(json.dumps({"devDependencies": {"@types/uuid": "9.0.8"}}), encoding="utf-8")
            direct = validator.direct_dependencies(json.loads((project / "package.json").read_text(encoding="utf-8")))
            row = {
                "name": "@types/uuid", "kind": "dev", "section": "devDependencies",
                "target_yellow": "11.0.0", "requested_spec": "9.0.8", "action": "remove",
            }
            findings = validator.compare_package(project, direct, row, "yellow", True)
            self.assertEqual("remove-action-not-applied", findings[0]["code"])


if __name__ == "__main__":
    unittest.main()
