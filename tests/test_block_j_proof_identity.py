from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dependency_live_roadmap_generator as roadmap
import constraint_cache
from baseline_constraint_verifier import AssignmentMaterializationError, _apply_assignment
from verification_proof import build_verification_proof_identity, is_fixed_manifest_spec


class BlockJProofIdentityTests(unittest.TestCase):
    def test_source_classifier_is_fail_closed_but_keeps_registry_ranges_and_tags(self) -> None:
        for spec in (
            "workspace:*",
            "file:../external_lib",
            "link:../external_lib",
            "portal:../external_lib",
            "git://example.invalid/external_lib.git",
            "git+ssh://example.invalid/external_lib.git",
            "github:example/external_lib",
            "example/external_lib",
            "https://example.invalid/external_lib.tgz",
            "npm:external_lib@^4.0.0",
            "patch:external_lib@1.0.0#./fix.patch",
            "catalog:external_lib",
            "future:source-syntax",
        ):
            self.assertTrue(is_fixed_manifest_spec(spec), spec)
            self.assertTrue(roadmap.is_non_registry_spec(spec), spec)

        for spec in ("1.2.3", "^1.2.3", "~1.2.3", ">=1 <3", "*", "latest", "next", "beta"):
            self.assertFalse(is_fixed_manifest_spec(spec), spec)
            self.assertFalse(roadmap.is_non_registry_spec(spec), spec)

    def test_heterogeneous_duplicate_source_declaration_fails_before_aggregation(self) -> None:
        rows = [
            SimpleNamespace(name="external_lib", kind="dev", requested_spec="^1.0.0"),
            SimpleNamespace(name="external_lib", kind="peer", requested_spec="workspace:*"),
        ]
        with self.assertRaisesRegex(
            roadmap.BaselineConstraintVerificationError,
            "HETEROGENEOUS_DIRECT_DEPENDENCY_DECLARATION",
        ):
            roadmap._aggregate_duplicate_package_row(rows)

    def test_verifier_never_rewrites_mixed_fixed_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            manifest = {
                "dependencies": {"external_lib": "^1.0.0"},
                "peerDependencies": {"external_lib": "workspace:*"},
            }
            package_json = project / "package.json"
            package_json.write_text(json.dumps(manifest), encoding="utf-8")
            before = package_json.read_text(encoding="utf-8")
            with self.assertRaisesRegex(
                AssignmentMaterializationError,
                "ASSIGNMENT_HETEROGENEOUS_SOURCE_CONFLICT",
            ):
                _apply_assignment(project, {"external_lib": "1.2.0"})
            self.assertEqual(before, package_json.read_text(encoding="utf-8"))

    def test_verifier_rejects_alias_target_even_without_duplicate_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(
                json.dumps({"dependencies": {"external_lib": "npm:external_lib_compat@^4.0.0"}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                AssignmentMaterializationError,
                "ASSIGNMENT_TARGETS_FIXED_INPUT",
            ):
                _apply_assignment(project, {"external_lib": "7.0.0"})

    def _identity(self, project: Path):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(project)}
        return build_verification_proof_identity(
            project,
            assignment={},
            remove_packages=(),
            manager="npm",
            manager_executable=sys.executable,
            registry="https://registry.example.invalid/npm",
            project_checks="off",
            commands=(),
            environment=env,
        )

    def test_external_file_content_change_invalidates_resolver_key_without_root_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            external = root / "external"
            project.mkdir()
            external.mkdir()
            (project / "package.json").write_text(
                json.dumps({"dependencies": {"external_lib": "file:../external"}}),
                encoding="utf-8",
            )
            external_manifest = external / "package.json"
            external_manifest.write_text(
                json.dumps({
                    "name": "external_lib",
                    "version": "3.0.0",
                    "peerDependencies": {"managed_pkg": "^1"},
                }),
                encoding="utf-8",
            )
            first = self._identity(project)
            source_before = first.source_snapshot_key
            external_manifest.write_text(
                json.dumps({
                    "name": "external_lib",
                    "version": "3.0.0",
                    "peerDependencies": {"managed_pkg": "^2"},
                }),
                encoding="utf-8",
            )
            second = self._identity(project)
            self.assertEqual(source_before, second.source_snapshot_key)
            self.assertNotEqual(first.resolver_input_key, second.resolver_input_key)

    def test_persistent_constraint_context_changes_with_external_fixed_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            external = root / "external"
            project.mkdir()
            external.mkdir()
            (project / "package.json").write_text(
                json.dumps({"dependencies": {"external_lib": "file:../external"}}),
                encoding="utf-8",
            )
            target = external / "package.json"
            target.write_text(
                json.dumps({"name": "external_lib", "version": "3.0.0", "peerDependencies": {"managed_pkg": "^1"}}),
                encoding="utf-8",
            )
            with mock.patch("verification_proof._version_identity", return_value="manager-v1"), \
                    mock.patch("verification_proof._node_identity", return_value="node-v1"):
                first = constraint_cache.resolver_environment_fingerprint(
                    project, registry="https://registry.example.invalid/npm"
                )
                target.write_text(
                    json.dumps({"name": "external_lib", "version": "3.0.0", "peerDependencies": {"managed_pkg": "^2"}}),
                    encoding="utf-8",
                )
                second = constraint_cache.resolver_environment_fingerprint(
                    project, registry="https://registry.example.invalid/npm"
                )
            self.assertNotEqual(first, second)

    def test_osv_transport_failure_is_unknown_not_empty_vulnerability_evidence(self) -> None:
        client = roadmap.LiveDataClient(
            "https://registry.example.invalid/npm",
            timeout=1,
            batch_size=10,
            sleep_sec=0,
        )
        client.session.post = mock.Mock(side_effect=RuntimeError("offline"))
        with self.assertRaisesRegex(
            roadmap.VulnerabilityEvidenceUnavailable,
            "OSV_QUERY_UNAVAILABLE",
        ):
            client.query_osv_versions("external_lib", ["1.0.0"])
        self.assertNotIn(("external_lib", "1.0.0"), client.osv_cache)

    def test_fixed_noop_path_contains_real_resolver_proof_gate(self) -> None:
        source = Path(roadmap.__file__).read_text(encoding="utf-8")
        start = source.index("if not changed and not removals:")
        end = source.index("cache_key =", start)
        section = source[start:end]
        self.assertIn("if fixed_input_names:", section)
        self.assertIn("noop_result = verify_assignment(", section)
        self.assertIn("run_project_checks=False", section)
        self.assertIn("BASELINE_NOOP_RESOLVER_INVALID", section)

    def test_desktop_does_not_retry_completed_proof_failures(self) -> None:
        main = (Path(__file__).resolve().parents[1] / "desktop" / "electron" / "main.ts").read_text(
            encoding="utf-8"
        )
        for marker in (
            "PROVEN_ASSIGNMENT_REOPENED",
            "PROVEN_ASSIGNMENT_MUTATED",
            "PROVEN_DEPENDENCY_ENVELOPE_INVALID",
            "FINAL_BASELINE_COMPATIBILITY_INVALID",
            "OBSERVED_RESOLVED_ASSIGNMENT_[A-Z0-9_]+",
            "BASELINE_NOOP_RESOLVER_INVALID",
        ):
            self.assertIn(marker, main)


if __name__ == "__main__":
    unittest.main()
