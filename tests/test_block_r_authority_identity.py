from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import constraint_cache
from constraint_cache import LearnedConstraintProof, load_verified_nogoods, persist_verified_nogood
from verification_proof import (
    VerificationProofStore,
    bind_resolved_state_identity,
    build_project_trial_key,
    build_resolver_context_key,
    build_resolver_trial_key,
    build_verification_proof_identity,
)


class BlockRAuthorityIdentityTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "project"
        home = root / "home"
        project.mkdir()
        home.mkdir()
        (project / "package.json").write_text(
            json.dumps({"name": "demo", "dependencies": {"a": "^1.0.0"}}),
            encoding="utf-8",
        )
        (project / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": {}}),
            encoding="utf-8",
        )
        return project

    def _context(self, project: Path, **extra_env: str) -> str:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(project.parent / "home"),
            **extra_env,
        }
        return build_resolver_context_key(
            project,
            manager="npm",
            manager_executable=sys.executable,
            registry="https://registry.example.invalid/npm",
            environment=env,
        )

    def test_context_changes_when_only_resolver_affecting_environment_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            a = self._context(project, npm_config_legacy_peer_deps="false")
            b = self._context(project, npm_config_legacy_peer_deps="true")
            self.assertEqual(64, len(a))
            self.assertNotEqual(a, b)

    def test_context_changes_when_user_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            npmrc = project.parent / "home" / ".npmrc"
            npmrc.write_text("legacy-peer-deps=false\n", encoding="utf-8")
            first = self._context(project)
            npmrc.write_text("legacy-peer-deps=true\n", encoding="utf-8")
            second = self._context(project)
            self.assertNotEqual(first, second)

    def test_persistent_nogood_from_context_a_is_not_authority_in_context_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._project(root)
            cache = root / "constraints.json"
            context_a = self._context(project, npm_config_legacy_peer_deps="false")
            context_b = self._context(project, npm_config_legacy_peer_deps="true")
            self.assertTrue(persist_verified_nogood(
                cache,
                LearnedConstraintProof(
                    project_path=str(project.resolve()),
                    environment_fingerprint=context_a,
                    literals={"a": "2.0.0"},
                    failure_signature="sig",
                    verified_count=2,
                ),
            ))
            self.assertEqual(
                [{"a": "2.0.0"}],
                load_verified_nogoods(
                    cache, project_path=project, environment_fingerprint=context_a
                ),
            )
            self.assertEqual(
                [],
                load_verified_nogoods(
                    cache, project_path=project, environment_fingerprint=context_b
                ),
            )

    def test_legacy_schema_v1_cache_is_never_promoted_to_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._project(root)
            cache = root / "constraints.json"
            context = self._context(project)
            cache.write_text(json.dumps({
                "schemaVersion": 1,
                "entries": [{
                    "projectPath": str(project.resolve()),
                    "environmentFingerprint": context,
                    "literals": {"a": "2.0.0"},
                    "failureSignature": "old-sig",
                    "source": "package-manager-resolver",
                    "verifiedCount": 2,
                }],
            }), encoding="utf-8")
            self.assertEqual([], load_verified_nogoods(
                cache, project_path=project, environment_fingerprint=context
            ))

    def test_tampered_v2_payload_fails_entry_key_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._project(root)
            cache = root / "constraints.json"
            context = self._context(project)
            persist_verified_nogood(
                cache,
                LearnedConstraintProof(
                    project_path=str(project.resolve()),
                    environment_fingerprint=context,
                    literals={"a": "2.0.0"},
                    failure_signature="sig",
                    verified_count=2,
                ),
            )
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["entries"][0]["literals"] = {"a": "9.9.9"}
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual([], load_verified_nogoods(
                cache, project_path=project, environment_fingerprint=context
            ))

    def test_corrupt_v2_record_is_dropped_on_next_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._project(root)
            cache = root / "constraints.json"
            context = self._context(project)
            first = LearnedConstraintProof(
                project_path=str(project.resolve()),
                environment_fingerprint=context,
                literals={"a": "2.0.0"},
                failure_signature="sig-a",
                verified_count=2,
            )
            self.assertTrue(persist_verified_nogood(cache, first))
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["entries"][0]["verifiedCount"] = "corrupt"
            cache.write_text(json.dumps(payload), encoding="utf-8")

            second = LearnedConstraintProof(
                project_path=str(project.resolve()),
                environment_fingerprint=context,
                literals={"b": "3.0.0"},
                failure_signature="sig-b",
                verified_count=2,
            )
            self.assertTrue(persist_verified_nogood(cache, second))
            self.assertEqual(
                [{"b": "3.0.0"}],
                load_verified_nogoods(
                    cache, project_path=project, environment_fingerprint=context
                ),
            )

    def test_short_display_hash_cannot_be_persisted_as_context_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._project(root)
            with self.assertRaisesRegex(ValueError, "RESOLVER_CONTEXT_KEY_INVALID"):
                persist_verified_nogood(
                    root / "constraints.json",
                    LearnedConstraintProof(
                        project_path=str(project.resolve()),
                        environment_fingerprint="deadbeefdeadbeef",
                        literals={"a": "2.0.0"},
                        failure_signature="sig",
                        verified_count=2,
                    ),
                )

    def test_trial_keys_are_full_and_bind_removals_commands_predicate_and_state(self) -> None:
        context = "a" * 64
        resolver_ab = build_resolver_trial_key(
            resolver_context_key=context,
            assignment={"a": "2", "b": "3"},
            remove_packages=(),
        )
        resolver_abc = build_resolver_trial_key(
            resolver_context_key=context,
            assignment={"a": "2", "b": "3", "c": "4"},
            remove_packages=(),
        )
        resolver_removed = build_resolver_trial_key(
            resolver_context_key=context,
            assignment={"a": "2", "b": "3"},
            remove_packages=("types",),
        )
        self.assertEqual(64, len(resolver_ab))
        self.assertNotEqual(resolver_ab, resolver_abc)
        self.assertNotEqual(resolver_ab, resolver_removed)

        project_a = build_project_trial_key(
            resolver_trial_key=resolver_ab,
            resolved_state_key="1" * 64,
            source_snapshot_key="source-a",
            project_checks="adaptive",
            commands=("yarn lint:types",),
            predicate_identity="sig-a",
        )
        project_b = build_project_trial_key(
            resolver_trial_key=resolver_ab,
            resolved_state_key="2" * 64,
            source_snapshot_key="source-a",
            project_checks="adaptive",
            commands=("yarn lint:types",),
            predicate_identity="sig-a",
        )
        project_command = build_project_trial_key(
            resolver_trial_key=resolver_ab,
            resolved_state_key="1" * 64,
            source_snapshot_key="source-a",
            project_checks="adaptive",
            commands=("yarn build",),
            predicate_identity="sig-a",
        )
        project_predicate = build_project_trial_key(
            resolver_trial_key=resolver_ab,
            resolved_state_key="1" * 64,
            source_snapshot_key="source-a",
            project_checks="adaptive",
            commands=("yarn lint:types",),
            predicate_identity="sig-b",
        )
        self.assertEqual(64, len(project_a))
        self.assertNotEqual(project_a, project_b)
        self.assertNotEqual(project_a, project_command)
        self.assertNotEqual(project_a, project_predicate)

    def test_proof_store_rejects_identity_payload_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._project(root)
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(root / "home"),
            }
            identity = build_verification_proof_identity(
                project,
                assignment={"a": "1.0.0"},
                remove_packages=(),
                manager="npm",
                manager_executable=sys.executable,
                registry="https://registry.example.invalid/npm",
                project_checks="off",
                commands=(),
                environment=env,
            )
            identity = bind_resolved_state_identity(
                identity,
                "1" * 64,
                project_checks="off",
                commands=(),
            )
            observed = {"a": "1.0.0"}
            observed_hash = __import__("hashlib").sha256(
                json.dumps(
                    observed, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            metadata = {
                "observedResolvedVersions": observed,
                "observedResolvedHash": observed_hash,
                "resolvedStateKey": "1" * 64,
                "resolvedStateResolverInputKey": identity.resolver_input_key,
                "resolvedPackageManager": "npm",
                "resolvedLockfilePath": "package-lock.json",
                "resolvedLockfileHash": "2" * 64,
                "resolvedStateArtifact": "state.json",
                "resolvedStateObservedHash": observed_hash,
            }
            store = VerificationProofStore(root / "proofs")
            self.assertTrue(store.publish_pass(
                "resolver", identity.resolver_input_key, identity,
                metadata=metadata,
            ))
            path = root / "proofs" / "resolver" / f"{identity.resolver_input_key}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["identity"]["resolverInputKey"] = "f" * 32
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(store.lookup_pass("resolver", identity.resolver_input_key))

    def test_roadmap_has_no_known_weak_authority_cache_markers(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertNotIn("project_environment_fingerprint", source)
        self.assertNotIn("trial_proof_cache.setdefault(trial_fingerprint", source)
        self.assertNotIn("adaptive_control_cache", source)
        self.assertIn("build_resolver_context_key(", source)
        self.assertIn("build_resolver_trial_key(", source)
        self.assertIn("build_project_trial_key(", source)


if __name__ == "__main__":
    unittest.main()
