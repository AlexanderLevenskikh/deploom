from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import validate_dependency_update as validator


class ScopeHashTestPolicyRegression(unittest.TestCase):
    def test_v2_hash_changes_when_required_policy_is_downgraded(self) -> None:
        row = {
            "project": "Demo", "group": 3, "subgroup": "router", "kind": "runtime",
            "package": "react-router-dom", "current": "6.0.0", "target": "7.0.0",
            "shouldUpdate": True, "testPolicy": "required", "testReason": "runtime behavior",
        }
        changed = copy.deepcopy(row)
        changed["testPolicy"] = "not-required-allowed"
        changed["testReason"] = "agent downgraded it"
        self.assertNotEqual(
            validator.fnv1a_scope_hash([row], hash_version=2),
            validator.fnv1a_scope_hash([changed], hash_version=2),
        )

    def test_v1_hash_remains_backward_compatible(self) -> None:
        row = {
            "project": "Demo", "group": 3, "subgroup": "router", "kind": "runtime",
            "package": "react-router-dom", "current": "6.0.0", "target": "7.0.0",
            "shouldUpdate": True, "testPolicy": "required", "testReason": "runtime behavior",
        }
        changed = dict(row, testPolicy="not-required-allowed", testReason="old manifest ignores it")
        self.assertEqual(validator.fnv1a_scope_hash([row]), validator.fnv1a_scope_hash([changed]))


if __name__ == "__main__":
    unittest.main()
