from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import validate_dependency_regression as regression
import validate_dependency_update as update


class RegressionEvidenceValidatorTests(unittest.TestCase):
    def create_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        project = root / "project"
        test_dir = project / "regressionTests" / "dependencyRegression"
        test_dir.mkdir(parents=True)
        test_file = test_dir / "react-router-dom.test.tsx"
        test_file.write_text(
            "import { generatePath } from 'react-router-dom';\n"
            "test('router preserves query parameters', () => {\n"
            "  const route = generatePath('/orders/:id', { id: '42' });\n"
            "  expect(`${route}?page=2`).toBe('/orders/42?page=2');\n"
            "});\n",
            encoding="utf-8",
        )
        source_dir = project / "src"
        source_dir.mkdir()
        (source_dir / "router.ts").write_text(
            "import { generatePath } from 'react-router-dom';\nexport const orderPath = (id: string) => generatePath('/orders/:id', { id });\n",
            encoding="utf-8",
        )
        (project / "package.json").write_text(json.dumps({
            "scripts": {
                "test": "vitest run --exclude regressionTests/dependencyRegression/**",
                "test:dependency-regression": "vitest run regressionTests/dependencyRegression",
            },
            "dependencies": {"react-router-dom": "7.0.0"},
        }), encoding="utf-8")
        rows = [{
            "project": "Demo", "group": 3, "subgroup": "router", "kind": "runtime",
            "package": "react-router-dom", "section": "dependencies", "requestedSpec": "^6.0.0",
            "current": "6.0.0", "target": "7.0.0", "shouldUpdate": True,
            "lagPolicyMonths": 12, "testPolicy": "required", "testReason": "runtime",
        }]
        scope = root / "scope.json"
        scope.write_text(json.dumps({
            "selectedRows": 1, "actionRows": 1, "scopeHash": update.fnv1a_scope_hash(rows), "rows": rows,
        }), encoding="utf-8")
        evidence = root / "evidence.json"
        evidence.write_text(json.dumps({
            "changePolicy": "minimal-compatibility-only",
            "refactoringPerformed": False,
            "regressionDirectory": "regressionTests/dependencyRegression",
            "defaultTestCommand": "yarn test",
            "regressionTestCommand": "yarn test:dependency-regression",
            "includedInDefaultTest": False,
            "defaultCollectedRegressionFiles": [],
            "regressionCollectedFiles": ["regressionTests/dependencyRegression/react-router-dom.test.tsx"],
            "packages": [{
                "package": "react-router-dom", "section": "dependencies", "testRequired": True,
                "testOrigin": "generated",
                "gateType": "test",
                "invariant": "navigation and query parameters preserve existing behavior",
                "relevanceProof": "The test imports generatePath from react-router-dom and checks the route shape used by src/router.ts.",
                "usageFiles": ["src/router.ts"],
                "testFiles": ["regressionTests/dependencyRegression/react-router-dom.test.tsx"],
                "testCases": ["router preserves query parameters"],
                "failureProbe": {
                    "status": "passed",
                    "restored": True,
                    "description": "Temporarily changed the expected route id and observed the dedicated regression test fail, then restored it.",
                },
                "baseline": {"status": "passed", "command": "yarn test:dependency-regression"},
                "postUpdate": {"status": "passed", "command": "yarn test:dependency-regression"},
            }],
        }), encoding="utf-8")
        return project, scope, evidence

    def test_valid_isolated_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, scope, evidence = self.create_fixture(Path(tmp))
            findings, summary = regression.validate(project, scope, evidence, "Demo")
            self.assertEqual([], findings)
            self.assertEqual(1, summary["coveredRequiredRows"])

    def test_generic_test_without_package_mapping_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, scope, evidence = self.create_fixture(Path(tmp))
            data = json.loads(evidence.read_text(encoding="utf-8"))
            data["packages"] = []
            evidence.write_text(json.dumps(data), encoding="utf-8")
            findings, _ = regression.validate(project, scope, evidence, "Demo")
            self.assertIn("required-package-uncovered", [item["code"] for item in findings])

    def test_regression_tests_in_default_run_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, scope, evidence = self.create_fixture(Path(tmp))
            data = json.loads(evidence.read_text(encoding="utf-8"))
            data["includedInDefaultTest"] = True
            data["defaultCollectedRegressionFiles"] = ["regressionTests/dependencyRegression/react-router-dom.test.tsx"]
            evidence.write_text(json.dumps(data), encoding="utf-8")
            findings, _ = regression.validate(project, scope, evidence, "Demo")
            codes = [item["code"] for item in findings]
            self.assertIn("default-test-inclusion-not-proven", codes)
            self.assertIn("regression-files-in-default-test", codes)

    def test_tautological_generated_test_fails_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, scope, evidence = self.create_fixture(Path(tmp))
            test_file = project / "regressionTests" / "dependencyRegression" / "react-router-dom.test.tsx"
            test_file.write_text("test('router preserves query parameters', () => expect(true).toBe(true));", encoding="utf-8")
            findings, _ = regression.validate(project, scope, evidence, "Demo")
            self.assertIn("tautological-assertion", [item["code"] for item in findings])

    def test_generated_test_without_failure_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, scope, evidence = self.create_fixture(Path(tmp))
            data = json.loads(evidence.read_text(encoding="utf-8"))
            del data["packages"][0]["failureProbe"]
            evidence.write_text(json.dumps(data), encoding="utf-8")
            findings, _ = regression.validate(project, scope, evidence, "Demo")
            self.assertIn("failure-probe-missing", [item["code"] for item in findings])


    def test_generated_test_file_must_be_named_for_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, scope, evidence = self.create_fixture(Path(tmp))
            source = project / "regressionTests" / "dependencyRegression" / "react-router-dom.test.tsx"
            target = source.with_name("group-3.test.tsx")
            source.rename(target)
            data = json.loads(evidence.read_text(encoding="utf-8"))
            data["regressionCollectedFiles"] = ["regressionTests/dependencyRegression/group-3.test.tsx"]
            data["packages"][0]["testFiles"] = ["regressionTests/dependencyRegression/group-3.test.tsx"]
            evidence.write_text(json.dumps(data), encoding="utf-8")
            findings, _ = regression.validate(project, scope, evidence, "Demo")
            codes = [item["code"] for item in findings]
            self.assertIn("generated-test-not-package-specific", codes)
            self.assertIn("generated-test-umbrella-name", codes)

    def test_refactoring_must_be_explicitly_disproved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, scope, evidence = self.create_fixture(Path(tmp))
            data = json.loads(evidence.read_text(encoding="utf-8"))
            data["refactoringPerformed"] = True
            evidence.write_text(json.dumps(data), encoding="utf-8")
            findings, _ = regression.validate(project, scope, evidence, "Demo")
            self.assertIn("refactoring-not-disproved", [item["code"] for item in findings])

    def test_generated_file_cannot_be_shared_by_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, scope, evidence = self.create_fixture(root)
            source_dir = project / "src"
            (source_dir / "history.ts").write_text(
                "import { createBrowserHistory } from 'history';\nexport const history = createBrowserHistory();\n",
                encoding="utf-8",
            )
            package_json = json.loads((project / "package.json").read_text(encoding="utf-8"))
            package_json["dependencies"]["history"] = "5.3.0"
            (project / "package.json").write_text(json.dumps(package_json), encoding="utf-8")

            scope_data = json.loads(scope.read_text(encoding="utf-8"))
            rows = scope_data["rows"]
            rows.append({
                "project": "Demo", "group": 3, "subgroup": "router", "kind": "runtime",
                "package": "history", "section": "dependencies", "requestedSpec": "^5.0.0",
                "current": "5.2.0", "target": "5.3.0", "shouldUpdate": True,
                "lagPolicyMonths": 12, "testPolicy": "required", "testReason": "runtime",
            })
            scope_data["selectedRows"] = 2
            scope_data["actionRows"] = 2
            scope_data["scopeHash"] = update.fnv1a_scope_hash(rows)
            scope.write_text(json.dumps(scope_data), encoding="utf-8")

            data = json.loads(evidence.read_text(encoding="utf-8"))
            shared = "regressionTests/dependencyRegression/react-router-dom.test.tsx"
            data["packages"].append({
                "package": "history", "section": "dependencies", "testRequired": True,
                "testOrigin": "generated", "gateType": "test",
                "invariant": "history creation remains available for router integration",
                "relevanceProof": "The shared test is intentionally referenced to prove that generated files may not cover multiple packages.",
                "usageFiles": ["src/history.ts"], "testFiles": [shared],
                "testCases": ["router preserves query parameters"],
                "failureProbe": {
                    "status": "passed", "restored": True,
                    "description": "Temporarily changed the route expectation, observed failure, and restored the original assertion.",
                },
                "baseline": {"status": "passed", "command": "yarn test:dependency-regression"},
                "postUpdate": {"status": "passed", "command": "yarn test:dependency-regression"},
            })
            evidence.write_text(json.dumps(data), encoding="utf-8")
            findings, _ = regression.validate(project, scope, evidence, "Demo")
            self.assertIn("generated-test-file-shared", [item["code"] for item in findings])


if __name__ == "__main__":
    unittest.main()
