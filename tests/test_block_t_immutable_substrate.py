from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import prepared_workspace_fastpath as fastpath
import baseline_constraint_verifier as baseline_verifier
import verification_proof as proof
from proven_dependency_state import (
    build_proven_dependency_envelope,
    proof_envelope_key,
    validate_proven_dependency_envelope,
)


class BlockTFixedSourceIdentityTests(unittest.TestCase):
    def _npm_project(
        self,
        root: Path,
        *,
        spec: str,
        resolved: str = "",
        integrity: str = "",
    ) -> Path:
        project = root / "project"
        project.mkdir()
        (project / "package.json").write_text(
            json.dumps({
                "packageManager": "npm@11.0.0",
                "dependencies": {"external_lib": spec},
            }),
            encoding="utf-8",
        )
        entry = {"version": "1.0.0"}
        if resolved:
            entry["resolved"] = resolved
        if integrity:
            entry["integrity"] = integrity
        (project / "package-lock.json").write_text(
            json.dumps({
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"external_lib": spec}},
                    "node_modules/external_lib": entry,
                },
            }),
            encoding="utf-8",
        )
        return project

    def test_same_git_ref_with_different_resolved_commit_changes_authority_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._npm_project(
                Path(tmp),
                spec="git+https://example.invalid/external_lib.git#main",
                resolved="git+https://example.invalid/external_lib.git#" + "a" * 40,
            )
            first = proof.fixed_resolver_input_fingerprint(project, manager="npm")
            lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
            lock["packages"]["node_modules/external_lib"]["resolved"] = (
                "git+https://example.invalid/external_lib.git#" + "b" * 40
            )
            (project / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
            second = proof.fixed_resolver_input_fingerprint(project, manager="npm")
            self.assertNotEqual(first, second)

    def test_same_http_url_with_different_integrity_changes_authority_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._npm_project(
                Path(tmp),
                spec="https://example.invalid/external_lib.tgz",
                resolved="https://example.invalid/external_lib.tgz",
                integrity="sha512-AAAA",
            )
            first = proof.fixed_resolver_input_fingerprint(project, manager="npm")
            lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
            lock["packages"]["node_modules/external_lib"]["integrity"] = "sha512-BBBB"
            (project / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
            second = proof.fixed_resolver_input_fingerprint(project, manager="npm")
            self.assertNotEqual(first, second)

    def test_moving_git_ref_without_resolved_commit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._npm_project(
                Path(tmp),
                spec="git+https://example.invalid/external_lib.git#main",
                resolved="git+https://example.invalid/external_lib.git#main",
            )
            with self.assertRaisesRegex(
                proof.SourceIdentityUnavailable,
                "FIXED_SOURCE_IMMUTABLE_IDENTITY_UNAVAILABLE",
            ):
                proof.fixed_resolver_input_fingerprint(project, manager="npm")

    def test_npm_git_lock_version_commit_is_accepted_when_resolved_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._npm_project(
                Path(tmp),
                spec="git+https://example.invalid/external_lib.git#main",
            )
            lock = json.loads((project / "package-lock.json").read_text(encoding="utf-8"))
            lock["packages"]["node_modules/external_lib"]["version"] = (
                "git+https://example.invalid/external_lib.git#" + "e" * 40
            )
            (project / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
            key = proof.fixed_resolver_input_fingerprint(project, manager="npm")
            self.assertEqual(64, len(key))

    def test_http_tarball_without_integrity_or_digest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._npm_project(
                Path(tmp),
                spec="https://example.invalid/external_lib.tgz",
                resolved="https://example.invalid/external_lib.tgz",
            )
            with self.assertRaisesRegex(
                proof.SourceIdentityUnavailable,
                "FIXED_SOURCE_IMMUTABLE_IDENTITY_UNAVAILABLE",
            ):
                proof.fixed_resolver_input_fingerprint(project, manager="npm")

    def test_raw_http_manifest_fragment_is_not_promoted_to_content_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._npm_project(
                Path(tmp),
                spec="https://example.invalid/external_lib.tgz#" + "f" * 40,
                resolved="https://example.invalid/external_lib.tgz",
            )
            with self.assertRaisesRegex(
                proof.SourceIdentityUnavailable,
                "FIXED_SOURCE_IMMUTABLE_IDENTITY_UNAVAILABLE",
            ):
                proof.fixed_resolver_input_fingerprint(project, manager="npm")

    def test_pinned_manifest_git_commit_is_immutable_without_moving_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._npm_project(
                Path(tmp),
                spec="git+https://example.invalid/external_lib.git#" + "c" * 40,
            )
            key = proof.fixed_resolver_input_fingerprint(project, manager="npm")
            self.assertEqual(64, len(key))

    def test_yarn_integrity_closes_remote_tarball_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            spec = "https://example.invalid/external_lib.tgz"
            (project / "package.json").write_text(
                json.dumps({
                    "packageManager": "yarn@1.22.22",
                    "dependencies": {"external_lib": spec},
                }),
                encoding="utf-8",
            )
            (project / "yarn.lock").write_text(
                f'"external_lib@{spec}":\n'
                '  version "1.0.0"\n'
                f'  resolved "{spec}"\n'
                '  integrity sha512-AAAA\n',
                encoding="utf-8",
            )
            first = proof.fixed_resolver_input_fingerprint(project, manager="yarn")
            text = (project / "yarn.lock").read_text(encoding="utf-8").replace(
                "sha512-AAAA", "sha512-BBBB"
            )
            (project / "yarn.lock").write_text(text, encoding="utf-8")
            second = proof.fixed_resolver_input_fingerprint(project, manager="yarn")
            self.assertNotEqual(first, second)

    def test_yarn_git_semver_ref_uses_resolved_commit_not_moving_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            spec = "git+ssh://git@example.invalid:team/fixture-lib.git#semver:2.132.0"
            (project / "package.json").write_text(
                json.dumps({
                    "packageManager": "yarn@1.22.22",
                    "dependencies": {"fixture-lib": spec},
                }),
                encoding="utf-8",
            )
            first_commit = "f" * 40
            (project / "yarn.lock").write_text(
                f'"fixture-lib@{spec}":\n'
                '  version "2.132.0"\n'
                f'  resolved "git+ssh://git@example.invalid:team/fixture-lib.git#{first_commit}"\n',
                encoding="utf-8",
            )
            first = proof.fixed_resolver_input_fingerprint(project, manager="yarn")
            remote_first = proof.remote_fixed_resolver_input_fingerprint(project, manager="yarn")

            second_commit = "e" * 40
            text = (project / "yarn.lock").read_text(encoding="utf-8").replace(
                first_commit, second_commit
            )
            (project / "yarn.lock").write_text(text, encoding="utf-8")
            second = proof.fixed_resolver_input_fingerprint(project, manager="yarn")
            remote_second = proof.remote_fixed_resolver_input_fingerprint(project, manager="yarn")
            self.assertNotEqual(first, second)
            self.assertNotEqual(remote_first, remote_second)

    def test_moving_git_resolution_control_fails_closed_without_safe_lock_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(
                json.dumps({
                    "packageManager": "npm@11.0.0",
                    "dependencies": {"a": "1.0.0"},
                    "overrides": {"nested": "git+https://example.invalid/nested.git#main"},
                }),
                encoding="utf-8",
            )
            (project / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {"": {}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                proof.SourceIdentityUnavailable,
                "FIXED_SOURCE_IMMUTABLE_IDENTITY_UNAVAILABLE",
            ):
                proof.fixed_resolver_input_fingerprint(project, manager="npm")

    def test_manifest_pinned_git_resolution_control_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(
                json.dumps({
                    "packageManager": "npm@11.0.0",
                    "dependencies": {"a": "1.0.0"},
                    "overrides": {
                        "nested": "git+https://example.invalid/nested.git#" + "a" * 40
                    },
                }),
                encoding="utf-8",
            )
            (project / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {"": {}}}),
                encoding="utf-8",
            )
            key = proof.fixed_resolver_input_fingerprint(project, manager="npm")
            self.assertEqual(64, len(key))


class BlockTFastpathFallbackReentryTests(unittest.TestCase):
    def test_released_failure_branch_no_longer_calls_public_wrapper_with_internal_kwargs(self) -> None:
        source = Path(baseline_verifier.__file__).read_text(encoding="utf-8")
        internal_start = source.index("def verify_assignment(")
        # Block Y preserves the released physical implementation behind
        # an explicit internal alias. Fastpath fallback must still avoid
        # re-entering the public cache-aware wrapper with internal kwargs.
        alias = source.index("_verify_assignment_uncached_impl = verify_assignment", internal_start)
        internal = source[internal_start:alias]
        self.assertIn("_retry_assignment_without_prepared_fastpath(", internal)
        self.assertNotIn("return verify_assignment(", internal)

        public_start = source.index("def verify_assignment(", alias)
        public_signature = source[public_start:source.index(") -> BaselineVerifyResult:", public_start)]
        self.assertNotIn("proof_identity", public_signature)
        self.assertNotIn("_allow_prepared_fastpath", public_signature)

    def test_forced_fastpath_rejection_retry_preserves_identity_and_disables_fastpath(self) -> None:
        sentinel = baseline_verifier.BaselineVerifyResult(
            True, "passed", "synthetic full-copy fallback"
        )
        identity = mock.Mock()
        identity.resolver_input_key = "a" * 32
        config = baseline_verifier.BaselineVerifyConfig(
            commands=("yarn build",),
            proof_cache_dir="synthetic-proof-cache",
        )
        with mock.patch.object(
            baseline_verifier,
            "_verify_assignment_uncached",
            return_value=sentinel,
        ) as internal:
            result = baseline_verifier._retry_assignment_without_prepared_fastpath(
                Path("."),
                {"demo": "2.0.0"},
                config=config,
                run_project_checks=True,
                remove_packages=(),
                progress=None,
                progress_label="forced-fastpath-rejection",
                proof_identity=identity,
            )

        self.assertIs(sentinel, result)
        internal.assert_called_once()
        kwargs = internal.call_args.kwargs
        self.assertIs(identity, kwargs["proof_identity"])
        self.assertFalse(kwargs["_allow_prepared_fastpath"])
        self.assertEqual(
            identity.resolver_input_key,
            kwargs["config"].reuse_resolver_proof_key,
        )


class BlockTVerificationDriftGateTests(unittest.TestCase):
    def test_real_resolver_and_lifecycle_recheck_fixed_source_identity(self) -> None:
        source = Path(baseline_verifier.__file__).read_text(encoding="utf-8")
        start = source.index("def verify_assignment(")
        section = source[start:]
        self.assertIn("def fixed_source_identity_gate(stage: str)", section)
        self.assertIn("remote_fixed_resolver_input_fingerprint(", section)
        self.assertIn("current_source_fixed_key", section)
        self.assertEqual(1, section.count('fixed_source_identity_gate("resolver")'))
        self.assertEqual(1, section.count('fixed_source_identity_gate("lifecycle")'))
        self.assertIn("FIXED_SOURCE_IDENTITY_DRIFT_DURING_", section)
        self.assertIn("verify.fixed-source.identity-drift", section)


class BlockTProofEnvelopeTests(unittest.TestCase):
    def test_pass_proof_identity_requires_fixed_source_authority_key(self) -> None:
        source = Path(proof.__file__).read_text(encoding="utf-8")
        validator = source[
            source.index("def _proof_record_identity_valid("):
            source.index("def _proof_record_metadata_valid(")
        ]
        self.assertIn('identity.get("fixedResolverInputsKey")', validator)
        self.assertIn("lengths=(64,)", validator)

    def _envelope(self, fixed_key: str) -> dict:
        return build_proven_dependency_envelope(
            project="Demo",
            mode="yellow",
            proof_schema="baseline-proof-v5-resolved-state",
            source_head="abc123",
            source_snapshot_key="source-key",
            assignment_key="assignment-key",
            resolver_input_key="resolver-key",
            fixed_resolver_inputs_key=fixed_key,
            preparation_proof_key="preparation-key",
            project_proof_key="project-key",
            observed_resolved_hash="a" * 64,
            resolved_state_key="b" * 64,
            resolved_lockfile_path="package-lock.json",
            resolved_lockfile_hash="c" * 64,
            assignment={"a": "2.0.0"},
            removals=(),
            verification_commands=("npm test",),
            project_checks="adaptive",
            resolver_proof_status="passed",
            preparation_proof_status="passed",
            project_proof_status="passed",
        )

    def test_envelope_carries_and_hashes_fixed_source_authority_key(self) -> None:
        first = self._envelope("d" * 64)
        self.assertEqual("d" * 64, first["fixedResolverInputsKey"])
        second = dict(first)
        second["fixedResolverInputsKey"] = "e" * 64
        self.assertNotEqual(first["envelopeKey"], proof_envelope_key(second))

    def test_envelope_rejects_missing_or_malformed_fixed_source_key(self) -> None:
        envelope = self._envelope("d" * 64)
        envelope["fixedResolverInputsKey"] = ""
        envelope["envelopeKey"] = proof_envelope_key(envelope)
        valid, reason = validate_proven_dependency_envelope(envelope)
        self.assertFalse(valid)
        self.assertIn("fixedResolverInputsKey", reason)


class BlockTDesktopHandoffTests(unittest.TestCase):
    def test_desktop_proof_envelope_v5_and_materialization_v4(self) -> None:
        root = Path(__file__).resolve().parents[1]
        migration = (root / "desktop" / "electron" / "migration-progress.ts").read_text(encoding="utf-8")
        materialization = (root / "desktop" / "electron" / "materialization-proof.ts").read_text(encoding="utf-8")
        main = (root / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("schemaVersion: 5", migration)
        self.assertIn("toolBuildId: string", migration)
        self.assertIn("fixedResolverInputsKey: string", migration)
        self.assertIn("proof envelope fixed source identity missing", migration)
        self.assertIn("schemaVersion: 4", materialization)
        self.assertIn("fixedResolverInputsKey: string", materialization)
        self.assertIn("fixed source proof identity mismatch", materialization)
        self.assertIn("fixedResolverInputsKey: proofEnvelope.fixedResolverInputsKey", main)


class BlockTSnapshotLeaseTests(unittest.TestCase):
    def test_production_wiring_holds_lease_through_guarded_clone_lifetime(self) -> None:
        source = Path(fastpath.__file__).read_text(encoding="utf-8")
        start = source.index("def try_materialize_guarded_clone(")
        end = source.index("def guarded_clone_is_active(", start)
        section = source[start:end]
        self.assertIn("_try_acquire_snapshot_lease(", section)
        self.assertIn("lease=lease", section)
        self.assertIn("lease_transferred = True", section)
        self.assertLess(
            section.index("_try_acquire_snapshot_lease("),
            section.index("_populate_node_modules_shell("),
        )

        cleanup_start = source.index("def cleanup_guarded_clone(")
        cleanup = source[cleanup_start:]
        self.assertIn("state.guard.stop()", cleanup)
        self.assertIn("state.lease.close()", cleanup)
        self.assertGreater(
            cleanup.index("state.lease.close()"),
            cleanup.index("state.guard.stop()"),
        )

    def test_private_copy_path_is_serialized_by_same_snapshot_lease(self) -> None:
        source = Path(baseline_verifier.__file__).read_text(encoding="utf-8")
        start = source.index("def _materialize_prepared_workspace_snapshot(")
        end = source.index("def _package_manager_cache_environment(", start)
        section = source[start:end]
        self.assertIn("acquire_snapshot_copy_lease(", section)
        self.assertIn("copy_lease.close()", section)
        self.assertLess(
            section.index("acquire_snapshot_copy_lease("),
            section.index("_copy_tree_snapshot("),
        )
        self.assertIn("_retire_prepared_workspace_snapshot", source)
        self.assertIn("try_acquire_snapshot_cleanup_lease", source)

    @unittest.skipUnless(os.name == "nt", "Windows handle lease semantics")
    def test_private_copy_waits_for_live_fastpath_owner_then_acquires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot"
            snapshot.mkdir()
            first = fastpath._try_acquire_snapshot_lease(snapshot)
            self.assertIsNotNone(first)
            assert first is not None
            observed: list[object] = []

            def acquire_copy() -> None:
                lease = fastpath.acquire_snapshot_copy_lease(snapshot, timeout_seconds=5)
                observed.append(lease)
                if lease is not None:
                    lease.close()

            thread = threading.Thread(target=acquire_copy)
            thread.start()
            threading.Event().wait(0.25)
            self.assertTrue(thread.is_alive(), "private copy must wait, not race shared bytes")
            first.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(1, len(observed))
            self.assertIsNotNone(observed[0])

    @unittest.skipUnless(os.name == "nt", "Windows handle lease semantics")
    def test_cache_eviction_never_deletes_live_consumer_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            workspace = stage / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "sentinel.txt").write_text("sealed", encoding="utf-8")
            snapshot = baseline_verifier.PreparedWorkspaceSnapshot(
                key="k",
                workspace_root=workspace,
                project_relative=Path("."),
                source_project=root,
                storage_mode="test",
                observed_resolved_versions={},
                observed_resolved_hash="",
                dependency_integrity={},
            )
            owner = fastpath._try_acquire_snapshot_lease(workspace)
            self.assertIsNotNone(owner)
            assert owner is not None
            try:
                self.assertFalse(baseline_verifier._retire_prepared_workspace_snapshot(snapshot))
                self.assertTrue(workspace.is_dir())
            finally:
                owner.close()
            self.assertTrue(baseline_verifier._retire_prepared_workspace_snapshot(snapshot))
            self.assertFalse(stage.exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle lease semantics")
    def test_live_handle_blocks_second_consumer_and_stale_file_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot"
            snapshot.mkdir()
            first = fastpath._try_acquire_snapshot_lease(snapshot)
            self.assertIsNotNone(first)
            assert first is not None

            second = fastpath._try_acquire_snapshot_lease(snapshot)
            self.assertIsNone(second)
            lease_path = first.path
            first.close()

            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_text("stale-name-only", encoding="utf-8")
            third = fastpath._try_acquire_snapshot_lease(snapshot)
            self.assertIsNotNone(third)
            assert third is not None
            third.close()

    @unittest.skipUnless(os.name == "nt", "Windows handle lease semantics")
    def test_two_threads_cannot_hold_same_snapshot_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot"
            snapshot.mkdir()
            first = fastpath._try_acquire_snapshot_lease(snapshot)
            self.assertIsNotNone(first)
            assert first is not None
            observed: list[object] = []

            thread = threading.Thread(
                target=lambda: observed.append(fastpath._try_acquire_snapshot_lease(snapshot))
            )
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual([None], observed)
            first.close()


if __name__ == "__main__":
    unittest.main()
