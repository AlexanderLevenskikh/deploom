from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import dependency_live_roadmap_generator as roadmap

from constraint_cache import (
    LearnedConstraintProof,
    dependency_failure_signature,
    load_verified_nogoods,
    persist_verified_nogood,
    resolver_environment_fingerprint,
    dependency_failure_navigation_signature,
    dependency_failure_predicates,
    matching_dependency_failure_signature,
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
            with mock.patch("verification_proof._version_identity", return_value="manager-v1"), \
                    mock.patch("verification_proof._node_identity", return_value="node-v1"):
                first = resolver_environment_fingerprint(root, registry="https://nexus/a")
            with mock.patch("verification_proof._version_identity", return_value="manager-v1"), \
                    mock.patch("verification_proof._node_identity", return_value="node-v2"):
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
                environment_fingerprint="a" * 64,
                literals={"a": "2", "b": "3"},
                failure_signature="sig",
                verified_count=2,
            )
            self.assertTrue(persist_verified_nogood(cache, proof))
            self.assertFalse(persist_verified_nogood(cache, proof))
            self.assertEqual([{"a": "2", "b": "3"}], load_verified_nogoods(
                cache, project_path=project, environment_fingerprint="a" * 64
            ))
            self.assertEqual([], load_verified_nogoods(
                cache, project_path=project, environment_fingerprint="b" * 64
            ))

            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["entries"].append({
                "projectPath": str(project.resolve()),
                "resolverContextKey": "a" * 64,
                "literals": {"x": "9"},
                "failureSignature": "sig2",
                "source": "package-manager-resolver",
                "verifiedCount": 1,
            })
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual([{"a": "2", "b": "3"}], load_verified_nogoods(
                cache, project_path=project, environment_fingerprint="a" * 64
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

    def test_retention_bound_is_per_project_not_global(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache.json"
            project_a = root / "a"
            project_b = root / "b"
            project_a.mkdir()
            project_b.mkdir()

            def proof(project: Path, literal: str) -> LearnedConstraintProof:
                return LearnedConstraintProof(
                    project_path=str(project.resolve()),
                    environment_fingerprint="e" * 64,
                    literals={literal: "2.0.0"},
                    failure_signature=f"sig-{literal}",
                    verified_count=2,
                )

            self.assertTrue(persist_verified_nogood(cache, proof(project_a, "a1"), max_entries=1))
            self.assertTrue(persist_verified_nogood(cache, proof(project_b, "b1"), max_entries=1))
            self.assertTrue(persist_verified_nogood(cache, proof(project_a, "a2"), max_entries=1))

            self.assertEqual(
                [{"a2": "2.0.0"}],
                load_verified_nogoods(cache, project_path=project_a, environment_fingerprint="e" * 64),
            )
            self.assertEqual(
                [{"b1": "2.0.0"}],
                load_verified_nogoods(cache, project_path=project_b, environment_fingerprint="e" * 64),
            )

    def test_parallel_cache_writers_do_not_lose_proofs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache.json"
            project = root / "project"
            project.mkdir()
            proofs = [
                LearnedConstraintProof(
                    project_path=str(project.resolve()),
                    environment_fingerprint="e" * 64,
                    literals={f"p{index}": "2.0.0"},
                    failure_signature=f"sig-{index}",
                    verified_count=2,
                )
                for index in range(12)
            ]
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda item: persist_verified_nogood(cache, item, max_entries=50), proofs))
            payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(12, len(payload["entries"]))


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
                exact = kwargs.get("global_exact_exclusions_by_project_mode") or {}
                captured.append(exact)
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


    def test_structured_npm_peer_predicate_matches_despite_unrelated_tree_noise(self) -> None:
        full = """
npm ERR! code ERESOLVE
npm ERR! Found: vite@4.3.9
npm ERR! node_modules/vite
npm ERR! vite@"4.3.9" from the root project
npm ERR! Could not resolve dependency:
npm ERR! peer vite@"^5.0.0" from vitest@3.2.6
npm ERR! unrelated full-assignment tree line
"""
        candidate = """
npm ERR! code ERESOLVE
npm ERR! candidate-only warning
npm ERR! Found: vite@4.3.9
npm ERR! Could not resolve dependency:
npm ERR! peer vite@"^5.0.0" from vitest@3.2.6
"""
        match = matching_dependency_failure_signature(
            expected_summary="npm resolver failed",
            expected_output=full,
            observed_summary="npm resolver failed in another workspace",
            observed_output=candidate,
        )
        self.assertTrue(match.startswith("resolver-predicate-v2:"), match)
        self.assertEqual(
            dependency_failure_navigation_signature(
                summary="resolver failed", output=full
            ),
            dependency_failure_navigation_signature(
                summary="resolver failed", output=candidate
            ),
        )

    def test_structured_peer_predicate_requires_same_found_version_and_range(self) -> None:
        base = """
npm ERR! code ERESOLVE
npm ERR! Found: vite@4.3.9
npm ERR! Could not resolve dependency:
npm ERR! peer vite@"^5.0.0" from vitest@3.2.6
"""
        different_found = """
npm ERR! code ERESOLVE
npm ERR! Found: vite@3.2.0
npm ERR! Could not resolve dependency:
npm ERR! peer vite@"^5.0.0" from vitest@3.2.6
"""
        different_range = """
npm ERR! code ERESOLVE
npm ERR! Found: vite@4.3.9
npm ERR! Could not resolve dependency:
npm ERR! peer vite@"^6.0.0" from vitest@3.2.6
"""
        for observed in (different_found, different_range):
            self.assertEqual(
                "",
                matching_dependency_failure_signature(
                    expected_summary="resolver failed",
                    expected_output=base,
                    observed_summary="resolver failed",
                    observed_output=observed,
                ),
            )

    def test_missing_version_predicate_matches_yarn_noise_but_not_other_range(self) -> None:
        full = (
            'yarn install v1.22.22\n'
            'warning Resolution field "x@1" is incompatible with requested version "x@2"\n'
            "error Couldn't find any versions for \"demo-pkg\" that matches \"^3.0.0\"\\n"
        )
        candidate = (
            'yarn install v1.22.22\n'
            'different progress line\n'
            "error Couldn't find any versions for \"demo-pkg\" that matches \"^3.0.0\"\\n"
        )
        other = (
            "error Couldn't find any versions for \"demo-pkg\" that matches \"^4.0.0\"\\n"
        )
        self.assertTrue(
            matching_dependency_failure_signature(
                expected_summary="yarn resolver failed",
                expected_output=full,
                observed_summary="yarn resolver failed",
                observed_output=candidate,
            ).startswith("resolver-predicate-v2:")
        )
        self.assertEqual(
            "",
            matching_dependency_failure_signature(
                expected_summary="yarn resolver failed",
                expected_output=full,
                observed_summary="yarn resolver failed",
                observed_output=other,
            ),
        )

    def test_resolution_field_warning_is_never_a_structured_fatal_predicate(self) -> None:
        self.assertEqual(
            (),
            dependency_failure_predicates(
                summary="yarn install failed with opaque exit",
                output=(
                    'warning Resolution field "es5-ext@0.10.50" is incompatible '
                    'with requested version "es5-ext@^0.10.62"'
                ),
            ),
        )

    def test_opaque_failures_keep_strict_legacy_equality(self) -> None:
        same = matching_dependency_failure_signature(
            expected_summary="opaque resolver crash",
            expected_output="dependency-flow-baseline-verify-abc/repo   XYZ-42",
            observed_summary="opaque resolver crash",
            observed_output="dependency-flow-baseline-verify-def/repo\nXYZ-42",
        )
        self.assertTrue(same)
        self.assertFalse(same.startswith("resolver-predicate-v2:"))
        self.assertEqual(
            "",
            matching_dependency_failure_signature(
                expected_summary="opaque resolver crash",
                expected_output="XYZ-42 alpha",
                observed_summary="opaque resolver crash",
                observed_output="XYZ-42 beta",
            ),
        )

    def test_shared_fatal_fact_can_match_when_full_output_contains_extra_fact(self) -> None:
        full = (
            "npm ERR! No matching version found for alpha@9.9.9.\n"
            "npm ERR! No matching version found for beta@8.8.8.\n"
        )
        candidate = "npm ERR! No matching version found for beta@8.8.8."
        match = matching_dependency_failure_signature(
            expected_summary="resolver failed",
            expected_output=full,
            observed_summary="resolver failed",
            observed_output=candidate,
        )
        self.assertTrue(match.startswith("resolver-predicate-v2:"), match)


if __name__ == "__main__":
    unittest.main()
