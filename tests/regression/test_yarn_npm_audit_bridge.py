from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manual_dependency_audit as audit


class YarnNpmAuditBridgeRegressionTests(unittest.TestCase):
    def test_canonical_inventory_contains_only_reachable_yarn_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(json.dumps({
                "name": "demo",
                "dependencies": {"foo": "^1.0.0"},
            }), encoding="utf-8")
            (project / "yarn.lock").write_text(
                'foo@^1.0.0:\n'
                '  version "1.0.0"\n'
                '  dependencies:\n'
                '    bar "^2.0.0"\n'
                'bar@^2.0.0:\n'
                '  version "2.0.0"\n'
                'unused@^9.0.0:\n'
                '  version "9.0.0"\n',
                encoding="utf-8",
            )

            inventory = audit.canonical_yarn_inventory(project)

            self.assertTrue(inventory["complete"])
            self.assertEqual([("bar", "2.0.0"), ("foo", "1.0.0")], inventory["pairs"])
            self.assertEqual(["foo"], inventory["directPackages"])
            self.assertEqual([], inventory["unresolvedEdges"])

    def test_yarn_inventory_audit_uses_exact_lock_inventory_without_npm_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            workspace = Path(tmp) / "audit"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({
                "name": "demo",
                "dependencies": {"foo": "^1.0.0"},
            }), encoding="utf-8")
            (project / "yarn.lock").write_text(
                'foo@^1.0.0:\n  version "1.0.0"\n',
                encoding="utf-8",
            )
            calls = []

            def fake_run(command, cwd, env=None, timeout=None):
                calls.append(list(command))
                if command[:4] == ["npm", "config", "get", "registry"]:
                    return 0, "https://nexus.example/repository/npm\n", ""
                if command[:2] == ["npm", "audit"]:
                    lock = json.loads((Path(cwd) / "package-lock.json").read_text(encoding="utf-8"))
                    foo_path = next(
                        path for path, entry in lock["packages"].items()
                        if isinstance(entry, dict) and entry.get("name") == "foo"
                    )
                    payload = {
                        "vulnerabilities": {
                            "foo": {
                                "severity": "high",
                                "isDirect": True,
                                "nodes": [foo_path],
                                "via": [{
                                    "source": 101,
                                    "name": "foo",
                                    "severity": "high",
                                    "title": "demo advisory",
                                    "range": "<1.0.1",
                                }],
                                "fixAvailable": True,
                            },
                        },
                        "metadata": {
                            "vulnerabilities": {
                                "critical": 0, "high": 1, "moderate": 0,
                                "low": 0, "unknown": 0,
                            },
                        },
                    }
                    return 1, json.dumps(payload), ""
                if command[:2] == ["npm", "install"]:
                    raise AssertionError("inventory audit must not invoke npm install")
                raise AssertionError(command)

            with patch.object(audit, "run", side_effect=fake_run):
                result = audit.run_audit(
                    project,
                    "yarn",
                    "https://nexus.example/repository/npm",
                    workspace,
                    yarn_audit_engine="yarn-inventory",
                )

            self.assertTrue(result["complete"])
            self.assertEqual("yarn-inventory", result["engine"])
            self.assertEqual(1, result["packageVersionTotals"]["high"])
            self.assertEqual(
                "yarn.lock:foo@1.0.0",
                result["packageDetails"]["foo"]["nodeVersions"][0]["path"],
            )
            self.assertEqual("not-evaluated", result["packageDetails"]["foo"]["fixAvailable"])
            self.assertFalse(any(command[:2] == ["npm", "install"] for command in calls))

    def test_auto_mode_uses_reproducible_npm_lock_without_yarn_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            workspace = Path(tmp) / "audit"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({
                "name": "demo", "dependencies": {"foo": "1.0.0"},
            }), encoding="utf-8")
            (project / "yarn.lock").write_text(
                'foo@1.0.0:\n  version "1.0.0"\n', encoding="utf-8"
            )

            def fake_run(command, cwd, env=None, timeout=None):
                if command[:4] == ["npm", "config", "get", "registry"]:
                    return 0, "https://nexus.example/repository/npm\n", ""
                if command[:2] == ["npm", "install"]:
                    (Path(cwd) / "package-lock.json").write_text(json.dumps({
                        "lockfileVersion": 3,
                        "packages": {"node_modules/foo": {"version": "1.0.0"}},
                    }), encoding="utf-8")
                    return 0, "lock created", ""
                if command[:2] == ["npm", "audit"]:
                    return 0, json.dumps({
                        "vulnerabilities": {},
                        "metadata": {"vulnerabilities": {
                            "critical": 0, "high": 0, "moderate": 0,
                            "low": 0, "unknown": 0,
                        }},
                    }), ""
                if command[0] == "yarn":
                    raise AssertionError("auto mode must not invoke Yarn audit")
                raise AssertionError(command)

            with patch.object(audit, "run", side_effect=fake_run):
                result = audit.run_audit(
                    project,
                    "yarn",
                    "https://nexus.example/repository/npm",
                    workspace,
                    yarn_audit_engine="auto",
                )

            self.assertTrue(result["complete"])
            self.assertEqual("npm-lock-bridge", result["engine"])
            self.assertEqual("package-lock.json", result["reproducibleFrom"])
            self.assertTrue((workspace / "package-lock.json").exists())

    def test_yarn_audit_uses_isolated_npm_lock_and_explicit_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            workspace = project / ".dependency-roadmap-audit"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            calls = []

            def fake_run(command, cwd, env=None, timeout=None):
                calls.append((list(command), Path(cwd), dict(env or {})))
                self.assertEqual("https://nexus.example/repository/npm", (env or {}).get("npm_config_registry"))
                if command[:4] == ["npm", "config", "get", "registry"]:
                    return 0, "https://nexus.example/repository/npm/\n", ""
                if command[:2] == ["npm", "install"]:
                    (Path(cwd) / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8")
                    return 0, "lock created", ""
                if command[:2] == ["npm", "audit"]:
                    payload = {"vulnerabilities": {}, "metadata": {"vulnerabilities": {"critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0}}}
                    return 0, json.dumps(payload), ""
                raise AssertionError(command)

            with patch.dict(os.environ, {"ROADMAP_YARN_AUDIT_ENGINE": "npm-lock-bridge"}), \
                 patch.object(audit, "run", side_effect=fake_run):
                result = audit.run_audit(project, "yarn", "https://nexus.example/repository/npm", workspace)

            self.assertTrue(result["complete"])
            self.assertEqual("npm-lock-bridge", result["engine"])
            self.assertEqual("https://nexus.example/repository/npm", result["effectiveRegistry"])
            self.assertFalse(result["lockReused"])
            self.assertEqual("npm", result["command"][0])
            self.assertNotIn("yarn", [call[0][0] for call in calls])
            self.assertTrue((workspace / "package-lock.json").exists())
            self.assertTrue((workspace / "npm-audit.json").exists())
            reproduce = (workspace / "reproduce-npm-audit.cmd").read_text(encoding="utf-8")
            self.assertIn("npm audit --json --package-lock-only --legacy-peer-deps %*", reproduce)
            self.assertNotIn("nexus.example", reproduce)
            reproduce_sh = (workspace / "reproduce-npm-audit.sh").read_text(encoding="utf-8")
            self.assertIn('exec npm audit --json --package-lock-only --legacy-peer-deps "$@"', reproduce_sh)
            self.assertNotIn("nexus.example", reproduce_sh)
            self.assertFalse((project / "package-lock.json").exists())

    def test_persistent_workspace_reuses_existing_package_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            workspace = project / ".dependency-roadmap-audit"
            project.mkdir()
            workspace.mkdir()
            (project / "package.json").write_text(json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            (workspace / ".dependency-roadmap-audit-workspace.json").write_text(json.dumps({"managedBy": "dependency-roadmap-tool"}), encoding="utf-8")
            (workspace / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "reused": True}), encoding="utf-8")
            input_state = audit._audit_input_state(project, "https://nexus.example/repository/npm")
            (workspace / "audit-input.json").write_text(json.dumps(input_state), encoding="utf-8")
            (workspace / "old-report.txt").write_text("stale", encoding="utf-8")
            calls = []

            def fake_run(command, cwd, env=None, timeout=None):
                calls.append(list(command))
                if command[:4] == ["npm", "config", "get", "registry"]:
                    return 0, "https://nexus.example/repository/npm\n", ""
                if command[:2] == ["npm", "install"]:
                    raise AssertionError("unchanged persistent inputs must not rebuild package-lock")
                payload = {"vulnerabilities": {}, "metadata": {"vulnerabilities": {"critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0}}}
                return 0, json.dumps(payload), ""

            with patch.dict(os.environ, {"ROADMAP_YARN_AUDIT_ENGINE": "npm-lock-bridge"}), \
                 patch.object(audit, "run", side_effect=fake_run):
                result = audit.run_audit(project, "yarn", "https://nexus.example/repository/npm", workspace)

            self.assertTrue(result["complete"])
            self.assertTrue(result["lockReused"])
            self.assertTrue(json.loads((workspace / "package-lock.json").read_text(encoding="utf-8"))["reused"])
            self.assertFalse((workspace / "old-report.txt").exists())
            self.assertFalse(any(command[:2] == ["npm", "install"] for command in calls))

    def test_changed_yarn_lock_rebuilds_persistent_package_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            workspace = project / ".dependency-roadmap-audit"
            project.mkdir()
            workspace.mkdir()
            (project / "package.json").write_text(json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\nnew-state\n", encoding="utf-8")
            (workspace / ".dependency-roadmap-audit-workspace.json").write_text(json.dumps({"managedBy": "dependency-roadmap-tool"}), encoding="utf-8")
            (workspace / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3}), encoding="utf-8")
            stale = dict(audit._audit_input_state(project, "https://nexus.example/repository/npm"))
            stale["yarnLockSha256"] = "stale"
            (workspace / "audit-input.json").write_text(json.dumps(stale), encoding="utf-8")
            install_calls = 0

            def fake_run(command, cwd, env=None, timeout=None):
                nonlocal install_calls
                if command[:4] == ["npm", "config", "get", "registry"]:
                    return 0, "https://nexus.example/repository/npm\n", ""
                if command[:2] == ["npm", "install"]:
                    install_calls += 1
                    self.assertFalse((Path(cwd) / "package-lock.json").exists(), "stale audit lock must be removed before rebuild")
                    (Path(cwd) / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "rebuilt": True}), encoding="utf-8")
                    return 0, "rebuilt", ""
                payload = {"vulnerabilities": {}, "metadata": {"vulnerabilities": {"critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0}}}
                return 0, json.dumps(payload), ""

            with patch.dict(os.environ, {"ROADMAP_YARN_AUDIT_ENGINE": "npm-lock-bridge"}), \
                 patch.object(audit, "run", side_effect=fake_run):
                result = audit.run_audit(project, "yarn", "https://nexus.example/repository/npm", workspace)

            self.assertTrue(result["complete"])
            self.assertFalse(result["lockReused"])
            self.assertEqual(1, install_calls)

    def test_explicit_yarn_native_mode_uses_canonical_yarn_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            calls = []

            def fake_run(command, cwd, env=None, timeout=None):
                calls.append(list(command))
                if command[:4] == ["yarn", "config", "get", "registry"]:
                    return 0, "yarn config v1.22.22\nhttps://nexus.example/repository/npm/\nDone in 0.03s.\n", ""
                if command[:2] == ["yarn", "audit"]:
                    payload = "\n".join([
                        json.dumps({"type": "auditAdvisory", "data": {"advisory": {"module_name": "foo", "severity": "high"}}}),
                        json.dumps({"type": "auditSummary", "data": {"vulnerabilities": {"critical": 0, "high": 1, "moderate": 0, "low": 0, "unknown": 0}}}),
                    ])
                    return 8, payload, ""
                raise AssertionError(command)

            with patch.dict(os.environ, {"ROADMAP_YARN_AUDIT_ENGINE": "yarn-native"}), \
                 patch.object(audit, "run", side_effect=fake_run):
                result = audit.run_audit(project, "yarn", "https://nexus.example/repository/npm")

            self.assertTrue(result["complete"])
            self.assertEqual("yarn-native", result["engine"])
            self.assertFalse(any(command[:2] == ["npm", "install"] for command in calls))

    def test_bridge_graph_drift_is_reported_without_failing_npm_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            workspace = Path(tmp) / "audit"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({"name": "demo", "dependencies": {"foo": "1.0.0"}}), encoding="utf-8")
            (project / "yarn.lock").write_text('foo@1.0.0:\n  version "1.0.0"\n', encoding="utf-8")

            def fake_run(command, cwd, env=None, timeout=None):
                if command[:4] == ["npm", "config", "get", "registry"]:
                    return 0, "https://nexus.example/repository/npm\n", ""
                if command[:2] == ["npm", "install"]:
                    (Path(cwd) / "package-lock.json").write_text(json.dumps({
                        "lockfileVersion": 3,
                        "packages": {
                            "node_modules/foo": {"version": "1.0.0"},
                            "node_modules/bar": {"version": "2.0.0"},
                        },
                    }), encoding="utf-8")
                    return 0, "", ""
                if command[:2] == ["npm", "audit"]:
                    return 1, json.dumps({
                        "vulnerabilities": {
                            "bar": {"severity": "critical", "nodes": ["node_modules/bar"], "via": [{"source": 1, "severity": "critical", "title": "bad"}]},
                        },
                        "metadata": {"vulnerabilities": {"critical": 1, "high": 0, "moderate": 0, "low": 0, "unknown": 0}},
                    }), ""
                raise AssertionError(command)

            with patch.dict(os.environ, {"ROADMAP_YARN_AUDIT_ENGINE": "npm-lock-bridge"}), \
                 patch.object(audit, "run", side_effect=fake_run):
                result = audit.run_audit(project, "yarn", "https://nexus.example/repository/npm", workspace)

            self.assertTrue(result["complete"])
            self.assertTrue(result["rawAuditComplete"])
            self.assertFalse(result["bridgeReconciliation"]["faithful"])
            self.assertEqual("npm-resolved-approximation", result["accuracy"])
            self.assertEqual(1, result["totals"]["critical"])
            self.assertEqual({}, result["rawAuditEvidence"])
            self.assertTrue(any("YARN_NPM_BRIDGE_DRIFT" in note for note in result["notes"]))


if __name__ == "__main__":
    unittest.main()
