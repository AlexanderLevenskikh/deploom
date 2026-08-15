from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manual_dependency_audit as manual
import validate_dependency_update as validator
import lockfile_consistency as locks


class ValidatorTests(unittest.TestCase):
    def test_exact_scope_rejects_changes_to_deferred_packages(self) -> None:
        rows = [
            {"project": "Demo", "name": "planned", "section": "dependencies", "requested_spec": "1.0.0", "shouldUpdate": True},
            {"project": "Demo", "name": "deferred", "section": "devDependencies", "requested_spec": "2.0.0", "shouldUpdate": False},
            {"project": "Demo", "name": "untouched", "section": "devDependencies", "requested_spec": "3.0.0", "shouldUpdate": False},
        ]
        direct = {
            ("dependencies", "planned"): "2.0.0",
            ("devDependencies", "deferred"): "2.5.0",
            ("devDependencies", "untouched"): "3.0.0",
        }
        findings = validator.scope_boundary_findings(rows, direct)
        self.assertEqual(["deferred"], [finding["package"] for finding in findings])
        self.assertEqual("error", findings[0]["severity"])
        self.assertEqual("out-of-scope-direct-change", findings[0]["code"])

    def test_compact_scope_v5_accepts_reasoned_exclusion_and_hashes_it(self) -> None:
        row = {
            "project": "Demo", "group": 4, "subgroup": "blocked", "kind": "runtime",
            "package": "oidc-client", "section": "dependencies", "requestedSpec": "1.11.5",
            "current": "1.11.5", "target": "—", "targetReason": "excluded",
            "shouldUpdate": False, "action": "excluded", "lagPolicyMonths": 3,
            "lagPolicyTarget": "2.0.0", "targetArtifactStatus": "not-applicable",
            "targetArtifactUrl": "", "targetArtifactError": "", "compatibilityCohort": "",
            "compatibilityNote": "", "scopeExcluded": True,
            "exclusionReason": "backend client migration is not ready",
            "exclusionSource": "dashboard-state", "testPolicy": "required",
            "testReason": "runtime behavior",
        }
        manifest = {
            "schemaVersion": 2, "scopeHashVersion": 5, "selectedRows": 1,
            "actionRows": 0, "deferredRows": 0, "excludedRows": 1,
            "scopeHash": validator.fnv1a_scope_hash([row], hash_version=5),
            "rows": [row],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            rows, meta = validator.read_scope_manifest(path, "Demo")
            self.assertEqual(1, meta["excludedRows"])
            self.assertTrue(rows[0]["scopeExcluded"])

            manifest["rows"][0]["exclusionReason"] = ""
            manifest["scopeHash"] = validator.fnv1a_scope_hash(manifest["rows"], hash_version=5)
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exclusionReason"):
                validator.read_scope_manifest(path, "Demo")

    def test_compact_scope_v4_requires_configured_registry_tarball_evidence(self) -> None:
        registry = "https://nexus.example/repository/npm-group"
        row = {
            "project": "Demo", "group": 2, "subgroup": "", "kind": "runtime",
            "package": "foo", "section": "dependencies", "requestedSpec": "^1.0.0",
            "current": "1.0.0", "target": "2.0.0", "targetReason": "lag",
            "shouldUpdate": True, "action": "update", "lagPolicyMonths": 12,
            "lagPolicyTarget": "2.0.0", "targetArtifactStatus": "available",
            "targetArtifactUrl": f"{registry}/foo/-/foo-2.0.0.tgz",
            "targetArtifactError": "", "compatibilityCohort": "",
            "compatibilityNote": "", "testPolicy": "required", "testReason": "runtime",
        }
        manifest = {
            "schemaVersion": 2, "scopeHashVersion": 4, "format": "compact-v1",
            "registry": registry,
            "registryPolicy": {"requireTargetTarball": True, "forbidForeignPackageManagerUrls": True},
            "selectedRows": 1, "actionRows": 1,
            "scopeHash": validator.fnv1a_scope_hash([row], hash_version=4),
            "rows": [row],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            rows, meta = validator.read_scope_manifest(path, "Demo")
            self.assertEqual(1, len(rows))
            self.assertEqual(registry, meta["registry"])

            manifest["rows"][0]["targetArtifactUrl"] = "https://registry.npmjs.org/foo/-/foo-2.0.0.tgz"
            manifest["scopeHash"] = validator.fnv1a_scope_hash(manifest["rows"], hash_version=4)
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "FOREIGN_REGISTRY_URL"):
                validator.read_scope_manifest(path, "Demo")

    def test_yarn_lock_foreign_registry_url_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "yarn.lock"
            lock.write_text(
                'foo@^1.0.0:\n  version "1.0.0"\n  resolved "https://registry.yarnpkg.com/foo/-/foo-1.0.0.tgz"\n',
                encoding="utf-8",
            )
            issues = locks.validate_registry_artifact_urls(
                "yarn", lock, "https://nexus.example/repository/npm-group"
            )
            self.assertEqual(1, len(issues))
            self.assertEqual("FOREIGN_REGISTRY_URL", issues[0].code)

    def test_direct_dependencies_preserve_section_identity(self) -> None:
        direct = validator.direct_dependencies({
            "dependencies": {"react": "19.0.0"},
            "peerDependencies": {"react": ">=18.0.0"},
        })
        self.assertEqual("19.0.0", direct[("dependencies", "react")])
        self.assertEqual(">=18.0.0", direct[("peerDependencies", "react")])

    def test_scope_hash_and_manifest_validation(self) -> None:
        rows = [
            {"project":"Demo","group":2,"subgroup":"a","kind":"runtime","package":"foo","section":"dependencies","current":"1.0.0","target":"2.0.0","shouldUpdate":True,"requestedSpec":"^1.0.0"},
            {"project":"Demo","group":5,"subgroup":"","kind":"peer","package":"react","section":"peerDependencies","current":"18.0.0","target":"—","shouldUpdate":False,"requestedSpec":">=18.0.0"},
        ]
        scope_hash = validator.fnv1a_scope_hash(rows)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps({"selectedRows":2,"actionRows":1,"scopeHash":scope_hash,"rows":rows}), encoding="utf-8")
            normalized, meta = validator.read_scope_manifest(path, "Demo")
            self.assertEqual(2, len(normalized))
            self.assertEqual(1, meta["actionRows"])
            self.assertEqual(scope_hash, meta["scopeHash"])


    def test_compact_scope_manifest_is_supported(self) -> None:
        columns = [
            "project", "group", "subgroup", "kind", "package", "section",
            "requestedSpec", "current", "target", "shouldUpdate", "lagPolicyMonths",
        ]
        compact_rows = [
            ["Demo", 2, "core", "runtime", "foo", "dependencies", "^1.0.0", "1.0.0", "2.0.0", True, 12],
            ["Demo", 5, "", "peer", "react", "peerDependencies", ">=18.0.0", "18.0.0", "—", False, 3],
        ]
        hash_rows = [dict(zip(columns, row)) for row in compact_rows]
        scope_hash = validator.fnv1a_scope_hash(hash_rows)
        manifest = {
            "schemaVersion": 2,
            "format": "compact-v1",
            "columns": columns,
            "selectedRows": 2,
            "actionRows": 1,
            "scopeHash": scope_hash,
            "rows": compact_rows,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            normalized, meta = validator.read_scope_manifest(path, "Demo")
            self.assertEqual(["foo", "react"], [row["name"] for row in normalized])
            self.assertEqual("^1.0.0", normalized[0]["requested_spec"])
            self.assertEqual(1, meta["actionRows"])
            self.assertEqual(scope_hash, meta["scopeHash"])

    def test_compact_scope_v3_hash_covers_policy_target_and_reason(self) -> None:
        columns = [
            "project", "group", "subgroup", "kind", "package", "section",
            "requestedSpec", "current", "target", "targetReason", "shouldUpdate", "action",
            "lagPolicyMonths", "lagPolicyTarget", "testPolicy", "testReason",
        ]
        compact_rows = [[
            "Demo", 4, "quarterly", "runtime", "@company/icons", "dependencies",
            "1.13.1", "1.13.1", "2.0.9", "manual lag policy", True, "update",
            3, "2.0.9", "required", "runtime behavior",
        ]]
        hash_rows = [dict(zip(columns, row)) for row in compact_rows]
        scope_hash = validator.fnv1a_scope_hash(hash_rows, hash_version=3)
        manifest = {
            "schemaVersion": 2,
            "scopeHashVersion": 3,
            "format": "compact-v1",
            "columns": columns,
            "selectedRows": 1,
            "actionRows": 1,
            "scopeHash": scope_hash,
            "rows": compact_rows,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            normalized, meta = validator.read_scope_manifest(path, "Demo")
            self.assertEqual("2.0.9", normalized[0]["lagPolicyTarget"])
            self.assertEqual(scope_hash, meta["scopeHash"])

    def test_strict_lag_policy_deferred_manifest_is_rejected_as_desync(self) -> None:
        row = {
            "project": "Demo", "group": 4, "subgroup": "quarterly", "kind": "runtime",
            "package": "@company/icons", "section": "dependencies", "requestedSpec": "1.13.1",
            "current": "1.13.1", "target": "—", "targetReason": "—",
            "shouldUpdate": False, "action": "deferred", "lagPolicyMonths": 3,
            "lagPolicyTarget": "2.0.9", "testPolicy": "required", "testReason": "runtime behavior",
        }
        manifest = {
            "schemaVersion": 2,
            "scopeHashVersion": 3,
            "selectedRows": 1,
            "actionRows": 0,
            "scopeHash": validator.fnv1a_scope_hash([row], hash_version=3),
            "rows": [row],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ROADMAP_TARGET_DESYNC"):
                validator.read_scope_manifest(path, "Demo")

    def test_multi_project_manifest_uses_per_project_scope_metadata(self) -> None:
        rows = [
            {"project":"Demo","group":2,"subgroup":"core","kind":"runtime","package":"foo","section":"dependencies","current":"1.0.0","target":"2.0.0","shouldUpdate":True},
            {"project":"Other","group":5,"subgroup":"tools","kind":"dev","package":"bar","section":"devDependencies","current":"1.0.0","target":"—","shouldUpdate":False},
        ]
        demo_rows = [rows[0]]
        other_rows = [rows[1]]
        manifest = {
            "selectedRows": 2,
            "actionRows": 1,
            "scopeHash": validator.fnv1a_scope_hash(rows),
            "projects": ["Demo", "Other"],
            "projectScopes": {
                "Demo": {"selectedRows": 1, "actionRows": 1, "deferredRows": 0, "scopeHash": validator.fnv1a_scope_hash(demo_rows)},
                "Other": {"selectedRows": 1, "actionRows": 0, "deferredRows": 1, "scopeHash": validator.fnv1a_scope_hash(other_rows)},
            },
            "rows": rows,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            normalized, meta = validator.read_scope_manifest(path, "Demo")
            self.assertEqual(["foo"], [row["name"] for row in normalized])
            self.assertEqual(1, meta["selectedRows"])
            self.assertEqual(1, meta["actionRows"])
            self.assertEqual(manifest["projectScopes"]["Demo"]["scopeHash"], meta["scopeHash"])

    def test_multi_project_manifest_without_project_scopes_is_rejected(self) -> None:
        rows = [
            {"project":"Demo","group":2,"subgroup":"","kind":"runtime","package":"foo","section":"dependencies","current":"1.0.0","target":"2.0.0","shouldUpdate":True},
            {"project":"Other","group":2,"subgroup":"","kind":"runtime","package":"bar","section":"dependencies","current":"1.0.0","target":"2.0.0","shouldUpdate":True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scope.json"
            path.write_text(json.dumps({
                "selectedRows": 2, "actionRows": 2,
                "scopeHash": validator.fnv1a_scope_hash(rows), "rows": rows,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "multi-project scope manifest"):
                validator.read_scope_manifest(path, "Demo")

    def test_scope_hash_matches_javascript_for_unicode(self) -> None:
        rows = [{
            "project": "Демо 🐼", "group": 2, "subgroup": "регулярные",
            "kind": "runtime", "package": "@scope/пакет",
            "current": "1.0.0", "target": "2.0.0", "shouldUpdate": True,
        }]
        expected = validator.fnv1a_scope_hash(rows)
        script = """
const rows = JSON.parse(process.argv[1]);
const text = rows.map(r => `${r.project}|${r.group}|${r.subgroup}|${r.kind}|${r.package}|${r.current}|${r.target}|${r.shouldUpdate}`).sort().join('\\n');
let hash = 2166136261;
for (let i=0;i<text.length;i++) { hash ^= text.charCodeAt(i); hash = Math.imul(hash, 16777619); }
console.log(('00000000' + (hash >>> 0).toString(16)).slice(-8));
"""
        result = subprocess.run(["node", "-e", script, json.dumps(rows, ensure_ascii=False)], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(expected, result.stdout.strip())

    def test_validator_cli_accepts_scope_manifest_and_peer_without_lock_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"; project.mkdir()
            (project / "package.json").write_text(json.dumps({
                "dependencies":{"foo":"^2.0.0"},
                "peerDependencies":{"react":">=18.0.0"}
            }), encoding="utf-8")
            (project / "package-lock.json").write_text(json.dumps({
                "lockfileVersion":3,
                "packages":{"node_modules/foo":{"version":"2.0.0"}}
            }), encoding="utf-8")
            roadmap = root / "roadmap.json"
            roadmap.write_text(json.dumps({"projects":{"Demo":[]}}), encoding="utf-8")
            rows = [
                {"project":"Demo","group":2,"subgroup":"a","kind":"runtime","package":"foo","section":"dependencies","current":"1.0.0","target":"2.0.0","shouldUpdate":True,"requestedSpec":"^1.0.0"},
                {"project":"Demo","group":5,"subgroup":"","kind":"peer","package":"react","section":"peerDependencies","current":"18.0.0","target":"18.0.0","shouldUpdate":True,"requestedSpec":">=18.0.0"},
            ]
            scope = root / "scope.json"
            scope.write_text(json.dumps({"selectedRows":2,"actionRows":2,"scopeHash":validator.fnv1a_scope_hash(rows),"rows":rows}), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(Path(validator.__file__)),
                "--roadmap-json", str(roadmap), "--project", "Demo", "--project-dir", str(project),
                "--target-mode", "yellow", "--scope-manifest", str(scope), "--strict-above-target", "--json"
            ], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(2, payload["checkedActionRows"])
            self.assertEqual(0, payload["errors"])

    def test_validator_cli_accepts_compact_scope_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({"dependencies": {"foo": "^2.0.0"}}), encoding="utf-8")
            (project / "package-lock.json").write_text(json.dumps({
                "lockfileVersion": 3,
                "packages": {"node_modules/foo": {"version": "2.0.0"}},
            }), encoding="utf-8")
            roadmap = root / "roadmap.json"
            roadmap.write_text(json.dumps({"projects": {"Demo": []}}), encoding="utf-8")
            columns = [
                "project", "group", "subgroup", "kind", "package", "section",
                "requestedSpec", "current", "target", "shouldUpdate", "lagPolicyMonths",
            ]
            compact_rows = [["Demo", 2, "core", "runtime", "foo", "dependencies", "^1.0.0", "1.0.0", "2.0.0", True, 12]]
            hash_rows = [dict(zip(columns, row)) for row in compact_rows]
            scope = root / "scope.json"
            scope.write_text(json.dumps({
                "schemaVersion": 2,
                "format": "compact-v1",
                "columns": columns,
                "selectedRows": 1,
                "actionRows": 1,
                "scopeHash": validator.fnv1a_scope_hash(hash_rows),
                "rows": compact_rows,
            }), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(Path(validator.__file__)),
                "--roadmap-json", str(roadmap), "--project", "Demo", "--project-dir", str(project),
                "--target-mode", "yellow", "--scope-manifest", str(scope), "--strict-above-target", "--json",
            ], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(1, payload["checkedActionRows"])
            self.assertEqual(0, payload["errors"])

    def test_wrong_section_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text('{"devDependencies":{"foo":"2.0.0"}}', encoding="utf-8")
            direct = validator.direct_dependencies(json.loads((project / "package.json").read_text()))
            findings = validator.compare_package(project, direct, {"name":"foo","kind":"runtime","target_yellow":"2.0.0","requested_spec":"1.0.0"}, "yellow", True)
            self.assertEqual("wrong-section", findings[0]["code"])


class ManualAuditTests(unittest.TestCase):
    def test_unknown_publish_date_does_not_fail_completed_vulnerability_audit(self) -> None:
        report = {
            "complete": False,
            "auditComplete": True,
            "lagComplete": False,
            "audit": {"complete": True},
            "lag": [{"name": "internal-package", "status": "unknown"}],
        }

        self.assertEqual(0, manual.audit_command_exit_code(report))

        report["auditComplete"] = False
        report["audit"]["complete"] = False
        self.assertEqual(2, manual.audit_command_exit_code(report))

    def test_exit_code_supports_legacy_report_without_top_level_audit_flag(self) -> None:
        self.assertEqual(0, manual.audit_command_exit_code({"audit": {"complete": True}}))
        self.assertEqual(2, manual.audit_command_exit_code({"audit": {"complete": False}}))

    def test_npm_audit_details_separate_nodes_packages_and_advisories(self) -> None:
        payload = {
            "vulnerabilities": {
                "form-data": {
                    "severity": "critical",
                    "isDirect": False,
                    "range": "<2.5.4",
                    "nodes": [
                        "node_modules/form-data",
                        "node_modules/request/node_modules/form-data",
                    ],
                    "via": [{
                        "source": 123,
                        "name": "form-data",
                        "title": "unsafe random boundary",
                        "severity": "critical",
                        "range": "<2.5.4",
                    }],
                    "effects": [],
                    "fixAvailable": True,
                }
            },
            "metadata": {
                "vulnerabilities": {
                    "critical": 2, "high": 0, "moderate": 0, "low": 0, "unknown": 0,
                }
            },
        }
        details = manual.parse_npm_audit_details(json.dumps(payload))
        self.assertEqual(2, details["nodeTotals"]["critical"])
        self.assertEqual(1, details["packageTotals"]["critical"])
        self.assertEqual(1, details["advisoryTotals"]["critical"])
        self.assertEqual(2, len(details["packages"]["form-data"]["nodes"]))

    def test_bridge_reconciliation_rejects_npm_only_package_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "yarn.lock").write_text(
                'foo@^1.0.0:\n  version "1.0.0"\n', encoding="utf-8"
            )
            (root / "package-lock.json").write_text(json.dumps({
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/foo": {"version": "1.0.0"},
                    "node_modules/bar": {"version": "2.0.0"},
                },
            }), encoding="utf-8")
            result = manual.reconcile_yarn_bridge(root / "yarn.lock", root / "package-lock.json")
            self.assertFalse(result["faithful"])
            self.assertIn("bar@2.0.0", result["extraPairs"])

    def test_parse_iso_normalizes_two_digit_fraction_for_python_310(self) -> None:
        normalized = manual._normalize_iso_for_fromisoformat("2022-06-14T19:46:48.37Z")
        self.assertEqual("2022-06-14T19:46:48.370000+00:00", normalized)
        parsed = manual.parse_iso("2022-06-14T19:46:48.37Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(370000, parsed.microsecond)
        self.assertEqual(0, parsed.utcoffset().total_seconds())

    def test_parse_npm_audit_v2(self) -> None:
        data = {
            "vulnerabilities": {
                "foo": {"severity":"high","via":[{"severity":"high"},{"severity":"moderate"}]},
                "bar": {"severity":"critical","via":["foo"]},
            },
            "metadata":{"vulnerabilities":{"critical":1,"high":1,"moderate":1,"low":0,"unknown":0}}
        }
        packages, totals, notes = manual.parse_audit("npm", json.dumps(data))
        self.assertEqual(1, packages["foo"]["high"])
        self.assertEqual(1, packages["foo"]["moderate"])
        self.assertEqual(1, totals["critical"])
        self.assertFalse(notes)

    def test_npm_audit_transitive_via_strings_do_not_inflate_package_count(self) -> None:
        data = {
            "vulnerabilities": {
                "wrapper": {"severity": "high", "via": ["one", "two", "three"]},
            },
            "metadata": {"vulnerabilities": {"critical": 0, "high": 1, "moderate": 0, "low": 0, "unknown": 0}},
        }
        packages, totals, _ = manual.parse_audit("npm", json.dumps(data))
        self.assertEqual(1, packages["wrapper"]["high"])
        self.assertEqual(1, totals["high"])

    def test_package_json_range_uses_declared_semver_without_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(json.dumps({
                "dependencies": {"foo": "^1.0.0"},
            }), encoding="utf-8")
            deps = manual.direct_dependencies(project)
            self.assertEqual("1.0.0", deps[0]["current"])
            self.assertEqual("package.json", deps[0]["source"])
            self.assertFalse(deps[0]["declaredExact"])
            self.assertTrue(deps[0]["resolvedExact"])
            with patch.object(manual, "npm_view_times", return_value=({
                "time": {"1.0.0": "2026-01-01T00:00:00Z", "2.0.0": "2026-07-01T00:00:00Z"},
                "dist-tags": {"latest": "2.0.0"},
            }, "")):
                result = manual.check_lag(project, deps, "", 12, {})
            self.assertEqual("ok", result[0]["status"])
            self.assertEqual("", result[0]["error"])

    def test_parse_yarn_audit_lines(self) -> None:
        lines = "\n".join([
            json.dumps({"type":"auditAdvisory","data":{"advisory":{"module_name":"foo","severity":"high"}}}),
            json.dumps({"type":"auditSummary","data":{"vulnerabilities":{"high":1}}}),
        ])
        packages, totals, _ = manual.parse_audit("yarn", lines)
        self.assertEqual(1, packages["foo"]["high"])
        self.assertEqual(1, totals["high"])

    def test_section_aware_dashboard_lag_policy_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "packageOverrides": {
                    "Demo": {
                        "runtime:react": {"lagMonths": 3},
                        "peer:react": {"lagMonths": 6},
                    }
                }
            }), encoding="utf-8")
            policies = manual.load_lag_policies(state, "Demo")
            metadata = ({
                "time": {"18.0.0": "2026-01-01T00:00:00Z", "19.0.0": "2026-07-01T00:00:00Z"},
                "dist-tags": {"latest": "19.0.0"},
            }, "")
            with patch.object(manual, "npm_view_times", return_value=metadata):
                result = manual.check_lag(
                    Path(tmp),
                    [{"section": "peerDependencies", "name": "react", "spec": ">=18", "current": "18.0.0", "source": "package.json"}],
                    "", 12, policies,
                )
            self.assertEqual(6, result[0]["policyMonths"])

    def test_missing_executable_is_reported_without_traceback(self) -> None:
        code, stdout, stderr = manual.run(["dependency-roadmap-command-that-does-not-exist"], Path.cwd())
        self.assertEqual(127, code)
        self.assertEqual("", stdout)
        self.assertIn("cannot execute", stderr)

    def test_manual_audit_cli_is_complete_when_audit_exit_means_vulnerabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({
                "name": "demo",
                "dependencies": {"foo": "1.0.0"},
            }), encoding="utf-8")
            (project / "package-lock.json").write_text(json.dumps({
                "lockfileVersion": 3,
                "packages": {"node_modules/foo": {"version": "1.0.0"}},
            }), encoding="utf-8")
            state = root / "state.json"
            state.write_text(json.dumps({
                "packageOverrides": {"Demo": {"runtime:foo": {"lagMonths": 3}}},
            }), encoding="utf-8")

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_impl = bin_dir / "fake_npm.py"
            fake_program = "\n".join([
                "import json, sys",
                "if len(sys.argv) > 1 and sys.argv[1] == 'audit':",
                "    print(json.dumps({'vulnerabilities': {'foo': {'severity': 'high', 'via': [{'severity': 'high'}]}}, 'metadata': {'vulnerabilities': {'critical': 0, 'high': 1, 'moderate': 0, 'low': 0, 'unknown': 0}}}))",
                "    raise SystemExit(1)",
                "if len(sys.argv) > 1 and sys.argv[1] == 'view':",
                "    print(json.dumps({'time': {'1.0.0': '2026-01-01T00:00:00Z', '2.0.0': '2026-07-01T00:00:00Z'}, 'dist-tags': {'latest': '2.0.0'}}))",
                "    raise SystemExit(0)",
                "raise SystemExit(2)",
                "",
            ])
            fake_impl.write_text(fake_program, encoding="utf-8")

            if os.name == "nt":
                fake_npm = bin_dir / "npm.cmd"
                fake_npm.write_text(
                    f'@echo off\r\n"{sys.executable}" "{fake_impl}" %*\r\nexit /b %ERRORLEVEL%\r\n',
                    encoding="utf-8",
                )
            else:
                fake_npm = bin_dir / "npm"
                fake_npm.write_text(
                    f'#!/bin/sh\nexec "{sys.executable}" "{fake_impl}" "$@"\n',
                    encoding="utf-8",
                )
                fake_npm.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            result = subprocess.run([
                sys.executable,
                str(Path(manual.__file__)),
                "--project-dir", str(project),
                "--project-name", "Demo",
                "--dashboard-state", str(state),
            ], text=True, encoding="utf-8", capture_output=True, check=False, env=env)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Completeness: `COMPLETE`", result.stdout)
            self.assertIn("Affected dependency nodes: **H:1**", result.stdout)
            self.assertIn("Unique advisories: **H:1**", result.stdout)
            self.assertIn("≤3m", result.stdout)


    def test_npm_view_times_falls_back_to_separate_field_shapes(self) -> None:
        calls = []

        def fake_view(project: Path, name: str, fields: list[str], registry: str):
            calls.append(tuple(fields))
            if fields == ["time", "dist-tags"]:
                return {"unexpected": True}, ""
            if fields == ["time"]:
                return {
                    "created": "2020-01-01T00:00:00Z",
                    "18.2.0": "2022-06-14T19:46:48.37Z",
                    "19.2.7": "2026-06-01T18:01:02.438Z",
                }, ""
            if fields == ["dist-tags"]:
                return {"latest": "19.2.7"}, ""
            raise AssertionError(fields)

        with patch.object(manual, "_npm_view_json", side_effect=fake_view):
            metadata, error = manual.npm_view_times(Path.cwd(), "react-dom", "https://registry.example")

        self.assertEqual("", error)
        self.assertEqual("19.2.7", metadata["dist-tags"]["latest"])
        self.assertEqual("2022-06-14T19:46:48.37Z", metadata["time"]["18.2.0"])
        self.assertEqual([
            ("time", "dist-tags"),
            ("time",),
            ("dist-tags",),
        ], calls)

    def test_check_lag_accepts_dates_proven_by_npm_view(self) -> None:
        metadata = ({
            "time": {
                "18.2.0": "2022-06-14T19:46:48.37Z",
                "19.2.7": "2026-06-01T18:01:02.438Z",
            },
            "dist-tags": {"latest": "19.2.7"},
        }, "")
        with patch.object(manual, "npm_view_times", return_value=metadata):
            result = manual.check_lag(
                Path.cwd(),
                [{
                    "section": "dependencies",
                    "name": "react-dom",
                    "spec": "18.2.0",
                    "current": "18.2.0",
                    "source": "yarn.lock",
                    "resolvedExact": True,
                }],
                "https://registry.example",
                12,
                {},
            )
        self.assertEqual("lagging", result[0]["status"])
        self.assertEqual("2022-06-14T19:46:48.370000+00:00", result[0]["currentPublishedAt"])
        self.assertEqual("2026-06-01T18:01:02.438000+00:00", result[0]["latestPublishedAt"])
        self.assertEqual("", result[0]["error"])

    def test_check_lag_recovers_missing_dates_with_exact_npm_fields(self) -> None:
        metadata = ({
            "time": {
                "created": "2020-01-01T00:00:00Z",
                "modified": "2026-06-01T18:01:02.438Z",
            },
            "dist-tags": {"latest": "19.2.7"},
        }, "")

        def exact_time(project: Path, name: str, version: str, registry: str):
            values = {
                "18.2.0": manual.parse_iso("2022-06-14T19:46:48.37Z"),
                "19.2.7": manual.parse_iso("2026-06-01T18:01:02.438Z"),
            }
            return values.get(version), ""

        with patch.object(manual, "npm_view_times", return_value=metadata),              patch.object(manual, "npm_view_exact_publish_time", side_effect=exact_time) as exact_mock:
            result = manual.check_lag(
                Path.cwd(),
                [{
                    "section": "dependencies",
                    "name": "react-dom",
                    "spec": "18.2.0",
                    "current": "18.2.0",
                    "source": "yarn.lock",
                    "resolvedExact": True,
                }],
                "https://registry.example",
                12,
                {},
            )

        self.assertEqual("lagging", result[0]["status"])
        self.assertEqual("", result[0]["error"])
        self.assertEqual(2, exact_mock.call_count)

    def test_npm_view_exact_publish_time_accepts_direct_scalar(self) -> None:
        with patch.object(manual, "_npm_view_json", return_value=("2022-06-14T19:46:48.370Z", "")) as view_mock:
            parsed, error = manual.npm_view_exact_publish_time(
                Path.cwd(), "react-dom", "18.2.0", "https://registry.example"
            )
        self.assertEqual("", error)
        self.assertIsNotNone(parsed)
        self.assertEqual(2022, parsed.year)
        self.assertEqual(["time[18.2.0]"], view_mock.call_args.args[2])

    def test_incomplete_audit_is_rendered_as_unknown_not_zero(self) -> None:
        report = {
            "projectDir": "C:/demo",
            "packageManager": "yarn",
            "registry": "https://registry.example",
            "generatedAt": "2026-07-13T00:00:00+00:00",
            "audit": {
                "complete": False,
                "packages": {},
                "totals": {level: 0 for level in manual.SEVERITIES},
                "command": ["yarn", "audit", "--json"],
                "exitCode": 1,
                "notes": ["ENOTFOUND registry.yarnpkg.com"],
            },
            "lag": [],
        }
        rendered = manual.markdown(report)
        self.assertIn("counts are unknown", rendered)
        self.assertIn("Affected dependency nodes: **UNKNOWN**", rendered)
        self.assertNotIn("Vulnerable packages were not found", rendered)

    def test_version_time_accepts_v_prefixed_metadata_key(self) -> None:
        parsed = manual._version_time({"v18.2.0": "2022-06-14T19:46:48.37Z"}, "18.2.0")
        self.assertIsNotNone(parsed)
        self.assertEqual(2022, parsed.year)


if __name__ == "__main__":
    unittest.main()
