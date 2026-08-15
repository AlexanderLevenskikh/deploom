from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dependency_live_roadmap_generator as roadmap

from constraint_cache import (
    LearnedConstraintProof,
    dependency_failure_signature,
    load_verified_nogoods,
    persist_verified_nogood,
    resolver_environment_fingerprint,
)


class ConstraintCacheTests(unittest.TestCase):
    def test_environment_fingerprint_changes_with_manifest_lock_or_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"name":"demo","dependencies":{"a":"1"}}\n', encoding="utf-8")
            (root / "yarn.lock").write_text("a@1:\n  version 1.0.0\n", encoding="utf-8")
            first = resolver_environment_fingerprint(root, registry="https://nexus/a")
            self.assertEqual(first, resolver_environment_fingerprint(root, registry="https://nexus/a"))
            self.assertNotEqual(first, resolver_environment_fingerprint(root, registry="https://nexus/b"))
            (root / "package.json").write_text('{"name":"demo","dependencies":{"a":"2"}}\n', encoding="utf-8")
            self.assertNotEqual(first, resolver_environment_fingerprint(root, registry="https://nexus/a"))

    def test_environment_fingerprint_includes_effective_node_and_package_manager_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"name":"demo","packageManager":"yarn@1.22.22"}\n', encoding="utf-8")
            (root / "yarn.lock").write_text("", encoding="utf-8")
            with mock.patch("constraint_cache._command_identity") as identity:
                identity.side_effect = lambda _root, command: {"command": command, "executable": f"/{command}", "version": "v1"}
                first = resolver_environment_fingerprint(root, registry="https://nexus/a")
                identity.side_effect = lambda _root, command: {"command": command, "executable": f"/{command}", "version": "v2" if command == "node" else "v1"}
                second = resolver_environment_fingerprint(root, registry="https://nexus/a")
            self.assertNotEqual(first, second)

    def test_only_reproducible_environment_matching_clauses_are_loaded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache.json"
            project = root / "project"
            project.mkdir()
            proof = LearnedConstraintProof(
                project_path=str(project.resolve()),
                environment_fingerprint="env-a",
                literals={"a": "2", "b": "3"},
                failure_signature="sig",
                verified_count=2,
            )
            self.assertTrue(persist_verified_nogood(cache, proof))
            self.assertFalse(persist_verified_nogood(cache, proof))
            self.assertEqual([{"a": "2", "b": "3"}], load_verified_nogoods(
                cache, project_path=project, environment_fingerprint="env-a"
            ))
            self.assertEqual([], load_verified_nogoods(
                cache, project_path=project, environment_fingerprint="env-b"
            ))

            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["entries"].append({
                "projectPath": str(project.resolve()),
                "environmentFingerprint": "env-a",
                "literals": {"x": "9"},
                "failureSignature": "sig2",
                "source": "package-manager-resolver",
                "verifiedCount": 1,
            })
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual([{"a": "2", "b": "3"}], load_verified_nogoods(
                cache, project_path=project, environment_fingerprint="env-a"
            ))

    def test_failure_signature_ignores_temp_workspace_name_and_whitespace(self):
        a = dependency_failure_signature(
            summary="npm resolver failed",
            output="ERR at dependency-flow-baseline-verify-abc123/repo   peer conflict",
        )
        b = dependency_failure_signature(
            summary="npm resolver failed",
            output="ERR at dependency-flow-baseline-verify-xyz987/repo\npeer conflict",
        )
        self.assertEqual(a, b)

    def test_solve_and_verify_loads_matching_persistent_nogood_into_all_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text(
                '{"name":"demo","devDependencies":{"a":"1.0.0","b":"1.0.0"}}\n',
                encoding="utf-8",
            )
            cache = root / "constraint-cache.json"
            env = resolver_environment_fingerprint(project, registry="https://nexus.example/repository/npm-group")
            persist_verified_nogood(
                cache,
                LearnedConstraintProof(
                    project_path=str(project.resolve()),
                    environment_fingerprint=env,
                    literals={"a": "2.0.0", "b": "2.0.0"},
                    failure_signature="stable",
                    verified_count=2,
                ),
            )
            rows = [
                roadmap.DependencyRow(
                    project="Demo", package_dir=".", name=name, kind="dev", requested_spec="*",
                    current_version="1.0.0", current_source="lockfile", latest_version="2.0.0",
                    current_vulns="0", min_no_critical="1.0.0", min_no_high="1.0.0",
                    min_no_vuln="1.0.0", min_lag_12m="1.0.0", min_lag_9m="1.0.0",
                    min_lag_6m="1.0.0", min_lag_3m="1.0.0", group=1, reason="cache", notes="",
                    target_default="1.0.0", target_yellow="1.0.0", target_green="1.0.0",
                )
                for name in ("a", "b")
            ]
            spec = roadmap.ProjectSpec(
                "Demo", project,
                constraint_verify_config={"enabled": True, "persistentLearning": True},
                constraint_cache_path=cache,
            )
            captured = []

            def fake_resolve(rows_by_project, client, **kwargs):
                learned = kwargs.get("learned_nogoods_by_project_mode") or {}
                captured.append(learned)
                modes = kwargs.get("modes", ("default",))
                result = {"Demo": {}}
                for mode in modes:
                    result["Demo"][mode] = {"a": "1.0.0", "b": "1.0.0"}
                return result

            client = roadmap.LiveDataClient(
                "https://nexus.example/repository/npm-group", timeout=1, batch_size=10, sleep_sec=0
            )
            with mock.patch.object(roadmap, "resolve_peer_compatibility", side_effect=fake_resolve):
                roadmap.resolve_peer_compatibility_with_verification(
                    {"Demo": rows}, {"Demo": spec}, client, modes=("default",)
                )

            self.assertGreaterEqual(len(captured), 1)
            self.assertIn({"a": "2.0.0", "b": "2.0.0"}, captured[0]["Demo"]["default"])


if __name__ == "__main__":
    unittest.main()
