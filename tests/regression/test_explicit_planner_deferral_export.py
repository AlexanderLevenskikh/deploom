from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as roadmap
import validate_dependency_update as validator


def deferred_row():
    return {
        "project": "Demo", "group": 4, "subgroup": "quarterly", "kind": "runtime",
        "package": "@example/widgets", "section": "dependencies",
        "requestedSpec": "^1.2.0", "current": "1.2.0", "target": "\u2014",
        "targetReason": "lag target; PEER_RESOLUTION_DEFERRED: desired=2.1.0; resolved=current 1.2.0; component unresolved",
        "shouldUpdate": False, "action": "deferred", "lagPolicyMonths": 3,
        "lagPolicyTarget": "2.1.0", "testPolicy": "required", "testReason": "runtime",
        "targetArtifactStatus": "not-applicable", "targetArtifactUrl": "", "targetArtifactError": "",
        "compatibilityCohort": "", "compatibilityNote": "component unresolved",
        "scopeExcluded": False, "exclusionReason": "", "exclusionSource": "",
    }


def manifest(rows):
    return {
        "schemaVersion": 2, "scopeHashVersion": 5,
        "selectedRows": len(rows), "actionRows": sum(bool(r["shouldUpdate"]) for r in rows),
        "scopeHash": validator.fnv1a_scope_hash(rows, hash_version=5), "rows": rows,
    }


class ExplicitPlannerDeferralExportTests(unittest.TestCase):
    def read(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "scope.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            return validator.read_scope_manifest(p, "Demo")

    def test_partial_manifest_keeps_deferred_row_and_other_updates(self):
        deferred = deferred_row()
        update = {**deferred, "package": "other", "requestedSpec": "1.0.0", "current": "1.0.0",
                  "target": "1.1.0", "targetReason": "security", "shouldUpdate": True,
                  "action": "update", "lagPolicyMonths": 12, "lagPolicyTarget": "1.0.0",
                  "targetArtifactStatus": "available", "targetArtifactUrl": "https://registry.npmjs.org/other/-/other-1.1.0.tgz"}
        rows, meta = self.read(manifest([deferred, update]))
        self.assertEqual(2, meta["selectedRows"])
        self.assertEqual(1, meta["actionRows"])
        self.assertEqual(0, meta["excludedRows"])
        self.assertFalse(rows[0]["shouldUpdate"])
        self.assertEqual("2.1.0", rows[0]["lagPolicyTarget"])
        self.assertEqual([], validator.scope_boundary_findings(rows, {("dependencies", "@example/widgets"): "^1.2.0"}))
        self.assertEqual("out-of-scope-direct-change", validator.scope_boundary_findings(
            rows, {("dependencies", "@example/widgets"): "^2.1.0"})[0]["code"])

    def test_missing_stale_or_unexplained_reason_still_rejected(self):
        base = deferred_row()
        for change in [
            {"targetReason": ""}, {"targetReason": "deferred"},
            {"current": "1.3.0"}, {"lagPolicyTarget": "2.2.0"},
            {"targetReason": "PEER_RESOLUTION_DEFERRED: desired=2.1.0; resolved=current 1.2.0; "},
            {"action": "none"},
        ]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "ROADMAP_TARGET_DESYNC"):
                self.read(manifest([{**base, **change}]))

    def test_reason_is_hash_bound_and_legacy_unsigned_reason_not_exempt(self):
        payload = manifest([deferred_row()])
        tampered = copy.deepcopy(payload)
        tampered["rows"][0]["targetReason"] += " altered"
        with self.assertRaisesRegex(ValueError, "scope hash mismatch"):
            self.read(tampered)
        for version in (1, 2):
            old = {**payload, "scopeHashVersion": version,
                   "scopeHash": validator.fnv1a_scope_hash(payload["rows"], hash_version=version)}
            with self.assertRaisesRegex(ValueError, "ROADMAP_TARGET_DESYNC"):
                self.read(old)

    @unittest.skipUnless(shutil.which("node"), "node required for generated dashboard export check")
    def test_generated_javascript_matches_manifest_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "dashboard.html"
            roadmap.write_html({"Demo": []}, p, history_dir=Path(tmp)/"history", history_snapshots=[])
            html = p.read_text(encoding="utf-8")
            def function(name):
                start = html.index("function " + name + "(")
                end = html.find("\nfunction ", start + 1)
                return html[start:end] if end != -1 else html[start:]
            js = "\n".join(function(name) for name in [
                "semverParts", "compareSemverText", "isActionTargetValue",
                "isExplicitPlannerDeferral", "planningConsistencyIssues",
            ])
            base = deferred_row()
            jsrow = {**base, "name": base["package"], "lagThresholdMonths": 3,
                     "hasTarget": False, "plannedAction": "deferred"}
            cases = [jsrow, {**jsrow, "targetReason": ""}, {**jsrow, "current": "1.3.0"},
                     {**jsrow, "lagPolicyTarget": "2.2.0"}, {**jsrow, "plannedAction": ""},
                     {**jsrow, "hasTarget": True, "target": "2.1.0", "registryArtifact": {"status": "missing"}}]
            js += "\nconst cases = " + json.dumps(cases) + ";\n"
            js += "process.stdout.write(JSON.stringify(cases.map(r => planningConsistencyIssues([r]).length)));"
            result = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([0, 1, 1, 1, 1, 1], json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
