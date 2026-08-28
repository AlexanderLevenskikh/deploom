from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import block_v_prepared_artifact as artifact_store
import source_snapshot
import substrate_identity
from baseline_constraint_verifier import project_proof_cache_reusable, reap_orphan_verification_trials
from constraint_cache import dependency_failure_predicates, dependency_failure_signature
from dependency_compatibility_evidence import (
    CompatibilityEvidence,
    external_evidence_identity_matches,
)
from prepared_workspace_fastpath import _DirectoryWatcher
from verification_process_supervisor import run_supervised


ROOT = Path(__file__).resolve().parents[1]
LOCAL_TEMP = None


class BlockLambdaReleaseClosureTests(unittest.TestCase):
    def tearDown(self) -> None:
        source_snapshot.clear_source_snapshot_epochs()

    def test_active_source_snapshot_rejects_same_size_mutation_and_stale_locator(self) -> None:
        with tempfile.TemporaryDirectory(dir=LOCAL_TEMP) as raw:
            project = Path(raw) / "project"
            project.mkdir()
            (project / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
            subject = project / "subject.txt"
            subject.write_text("aaaa", encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "init", "-b", "master"], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "-C", str(project), "config", "user.email", "lambda@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.name", "Lambda Test"], check=True)
            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-m", "fixture"], check=True, stdout=subprocess.PIPE)
            snapshot = source_snapshot.activate_source_snapshot_epoch(project, replace=True)
            before = subject.stat()
            sealed = snapshot.project_path / "subject.txt"
            sealed.write_text("bbbb", encoding="utf-8")
            os.utime(sealed, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(source_snapshot.SourceCaptureError, "SOURCE_SNAPSHOT_CONTENT_MISMATCH"):
                source_snapshot.source_snapshot_fingerprint(project)
            self.assertIsNone(source_snapshot.active_source_snapshot(project))


    def test_materialization_rejects_snapshot_mutation_after_prevalidation(self) -> None:
        with tempfile.TemporaryDirectory(dir=LOCAL_TEMP) as raw:
            project = Path(raw) / "project"
            project.mkdir()
            (project / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
            (project / "subject.txt").write_text("aaaa", encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "init", "-b", "master"], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "-C", str(project), "config", "user.email", "lambda@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.name", "Lambda Test"], check=True)
            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-m", "fixture"], check=True, stdout=subprocess.PIPE)
            snapshot = source_snapshot.activate_source_snapshot_epoch(project, replace=True)
            target = Path(raw) / "materialized"
            def mutate_then_materialize(_source, destination, **_kwargs):
                (snapshot.project_path / "subject.txt").write_text("bbbb", encoding="utf-8")
                destination.mkdir()
                (destination / "subject.txt").write_text("bbbb", encoding="utf-8")
                return "test-mutated-copy"

            from unittest import mock
            with mock.patch(
                "source_snapshot.materialize_private_tree",
                side_effect=mutate_then_materialize,
            ):
                with self.assertRaisesRegex(
                    source_snapshot.SourceCaptureError,
                    "SOURCE_MATERIALIZED_CONTENT_MISMATCH",
                ):
                    source_snapshot.materialize_source_for_verification(
                        project, target, timeout_seconds=30
                    )

    def test_source_capture_rejects_temp_container_inside_subject_before_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir=LOCAL_TEMP) as raw:
            project = Path(raw) / "project"
            project.mkdir()
            (project / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "init", "-b", "master"], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "-C", str(project), "config", "user.email", "lambda@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.name", "Lambda Test"], check=True)
            subprocess.run(["git", "-C", str(project), "add", "."], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-m", "fixture"], check=True, stdout=subprocess.PIPE)
            inside = project / "dependency-flow-source-snapshot-injected"

            def allocate(*_args, **_kwargs):
                inside.mkdir()
                return str(inside)

            from unittest import mock
            with mock.patch("source_snapshot.tempfile.mkdtemp", side_effect=allocate):
                with self.assertRaisesRegex(
                    source_snapshot.SourceCaptureError, "SOURCE_SNAPSHOT_TEMP_INSIDE_SUBJECT"
                ):
                    source_snapshot.capture_source_snapshot(project, timeout_seconds=30)

    def _artifact(self):
        temporary = tempfile.TemporaryDirectory(dir=LOCAL_TEMP)
        root = Path(temporary.name)
        old = os.environ.get("DEPLOOM_VERIFICATION_ROOT")
        os.environ["DEPLOOM_VERIFICATION_ROOT"] = str(root / "substrate")
        artifact_store.configure_prepared_artifact_store(root / "proofs")
        source = root / "source"
        source.mkdir()
        trees = artifact_store.prepared_snapshot_storage_root()
        assert trees is not None
        workspace = Path(tempfile.mkdtemp(prefix="artifact-", dir=trees)) / "workspace"
        (workspace / "project").mkdir(parents=True)
        (workspace / "project" / "package.json").write_text("{}", encoding="utf-8")
        return temporary, old, source, workspace

    def _restore_root(self, old):
        if old is None:
            os.environ.pop("DEPLOOM_VERIFICATION_ROOT", None)
        else:
            os.environ["DEPLOOM_VERIFICATION_ROOT"] = old

    def test_same_process_artifact_cache_does_not_survive_mutation_or_index_delete(self) -> None:
        temporary, old, source, workspace = self._artifact()
        try:
            key = "d" * 32
            self.assertTrue(artifact_store.publish_prepared_artifact_record(
                key=key, workspace_root=workspace, project_relative=Path("project"),
                source_project=source, storage_mode="test",
                observed_resolved_versions={}, observed_resolved_hash="e" * 64,
            ))
            self.assertIsNotNone(artifact_store.load_prepared_artifact_record(key, source))
            subject = workspace / "project" / "package.json"
            stamp = subject.stat()
            subject.write_text("[]", encoding="utf-8")
            os.utime(subject, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertIsNone(artifact_store.load_prepared_artifact_record(key, source))

            subject.write_text("{}", encoding="utf-8")
            self.assertTrue(artifact_store.publish_prepared_artifact_record(
                key=key, workspace_root=workspace, project_relative=Path("project"),
                source_project=source, storage_mode="test",
                observed_resolved_versions={}, observed_resolved_hash="e" * 64,
            ))
            self.assertIsNotNone(artifact_store.load_prepared_artifact_record(key, source))
            index = artifact_store.configured_prepared_artifact_root() / "index" / f"{key}.json"
            index.unlink()
            self.assertIsNone(artifact_store.load_prepared_artifact_record(key, source))
        finally:
            self._restore_root(old)
            temporary.cleanup()


    def test_cross_process_artifact_invalidation_is_observed(self) -> None:
        temporary, old, source, workspace = self._artifact()
        try:
            key = "c" * 32
            self.assertTrue(artifact_store.publish_prepared_artifact_record(
                key=key, workspace_root=workspace, project_relative=Path("project"),
                source_project=source, storage_mode="test",
                observed_resolved_versions={}, observed_resolved_hash="b" * 64,
            ))
            self.assertIsNotNone(artifact_store.load_prepared_artifact_record(key, source))
            child = (
                "import block_v_prepared_artifact as s;"
                f"s.configure_prepared_artifact_store({str(Path(temporary.name) / 'proofs')!r});"
                f"raise SystemExit(0 if s.invalidate_prepared_artifact_record({key!r}) else 2)"
            )
            subprocess.run([sys.executable, "-c", child], cwd=ROOT, check=True, env=os.environ.copy())
            self.assertIsNone(artifact_store.load_prepared_artifact_record(key, source))
        finally:
            self._restore_root(old)
            temporary.cleanup()

    def test_prepared_artifact_hardlink_topology_is_rejected(self) -> None:
        temporary, old, source, workspace = self._artifact()
        try:
            subject = workspace / "project" / "package.json"
            alias = Path(temporary.name) / "external-alias.json"
            try:
                os.link(subject, alias)
            except OSError as exc:
                self.skipTest(f"hardlink unavailable: {exc}")
            self.assertFalse(artifact_store.publish_prepared_artifact_record(
                key="f" * 32, workspace_root=workspace, project_relative=Path("project"),
                source_project=source, storage_mode="test",
                observed_resolved_versions={}, observed_resolved_hash="a" * 64,
            ))
        finally:
            self._restore_root(old)
            temporary.cleanup()

    def test_external_evidence_requires_triple_snapshot_identity(self) -> None:
        evidence = CompatibilityEvidence(
            project="Demo", project_path=ROOT, branch_ref="main", target_mode="yellow",
            commands=("npm test",), actions=(), exact_assignment=(("a", "2.0.0"),),
            source_snapshot_key="snapshot-s1", envelope_source_snapshot_key="snapshot-s1",
        )
        self.assertTrue(external_evidence_identity_matches(evidence, "snapshot-s1"))
        self.assertFalse(external_evidence_identity_matches(evidence, "snapshot-s2"))
        self.assertFalse(external_evidence_identity_matches(
            dataclasses.replace(evidence, envelope_source_snapshot_key="snapshot-s2"),
            "snapshot-s1",
        ))

    def test_failure_signature_ignores_trial_timestamp_log_and_pid_but_not_semantics(self) -> None:
        first = "2026-08-27T10:11:12Z pid=123 C:/tmp/dependency-flow-baseline-verify-a/_logs/2026-debug.log No matching version found for demo@^2"
        second = "2026-08-28T11:12:13Z pid=999 D:/x/dependency-flow-baseline-verify-b/_logs/other.log No matching version found for demo@^2"
        changed = second.replace("demo@^2", "demo@^3")
        self.assertEqual(
            dependency_failure_signature(summary="resolver failed", output=first),
            dependency_failure_signature(summary="resolver failed", output=second),
        )
        self.assertNotEqual(
            dependency_failure_signature(summary="resolver failed", output=first),
            dependency_failure_signature(summary="resolver failed", output=changed),
        )

    def test_yarn_classic_predicates_preserve_semantic_literals(self) -> None:
        output = """Couldn't find package "left-pad@^9.0.0" required by "demo@1.2.3"
error demo@1.2.3: The engine "node" is incompatible with this module. Expected version ">=99"."""
        facts = dependency_failure_predicates(summary="yarn failed", output=output)
        self.assertIn("yarn1-required-package:spec=left-pad@^9.0.0;consumer=demo@1.2.3", facts)
        self.assertIn("yarn1-engine-incompatible:engine=node;expected=>=99", facts)

    def test_diagnostic_minimization_inconclusive_cannot_abort_exact_search(self) -> None:
        generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertNotIn("BASELINE_CONSTRAINT_MINIMIZATION_INCONCLUSIVE", generator)
        self.assertGreaterEqual(generator.count('"verify.diagnostic"'), 4)
        self.assertGreaterEqual(generator.count("abortAllowed=False"), 4)
        self.assertIn("keeping the certified exact exclusion and continuing", generator)

    def test_external_command_project_proof_is_not_durable_reusable(self) -> None:
        self.assertFalse(project_proof_cache_reusable(("npm test",)))
        self.assertFalse(project_proof_cache_reusable(("python verify.py",)))
        self.assertTrue(project_proof_cache_reusable(()))

    def test_tool_build_component_digest_changes_identity(self) -> None:
        from unittest import mock
        substrate_identity.tool_build_components.cache_clear()
        substrate_identity.tool_build_id.cache_clear()
        with mock.patch.object(substrate_identity, "_file_digest", return_value="1" * 64):
            substrate_identity.tool_build_components.cache_clear()
            substrate_identity.tool_build_id.cache_clear()
            first = substrate_identity.tool_build_id()
        with mock.patch.object(substrate_identity, "_file_digest", return_value="2" * 64):
            substrate_identity.tool_build_components.cache_clear()
            substrate_identity.tool_build_id.cache_clear()
            second = substrate_identity.tool_build_id()
        self.assertNotEqual(first, second)
        substrate_identity.tool_build_components.cache_clear()
        substrate_identity.tool_build_id.cache_clear()

    def test_release_package_depends_on_exact_sha_windows_validation(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("validate-windows:", workflow)
        self.assertIn("needs: [validate, validate-windows]", workflow)
        self.assertIn("test_block_lambda_release_closure", workflow)

    def test_tool_build_override_cannot_replace_computed_identity(self) -> None:
        substrate_identity.tool_build_id.cache_clear()
        expected = substrate_identity.tool_build_id()
        old = os.environ.get("DEPLOOM_TOOL_BUILD_ID")
        os.environ["DEPLOOM_TOOL_BUILD_ID"] = "0" * 64
        try:
            substrate_identity.tool_build_id.cache_clear()
            self.assertEqual(expected, substrate_identity.tool_build_id())
        finally:
            if old is None:
                os.environ.pop("DEPLOOM_TOOL_BUILD_ID", None)
            else:
                os.environ["DEPLOOM_TOOL_BUILD_ID"] = old
            substrate_identity.tool_build_id.cache_clear()

    def test_orphan_trial_reaper_only_reclaims_old_owned_namespace(self) -> None:
        with tempfile.TemporaryDirectory(dir=LOCAL_TEMP) as raw:
            parent = Path(raw)
            old_trial = parent / "dependency-flow-baseline-verify-old"
            unrelated = parent / "user-data"
            old_trial.mkdir()
            unrelated.mkdir()
            old_time = time.time() - 48 * 60 * 60
            os.utime(old_trial, (old_time, old_time))
            candidates, reclaimed = reap_orphan_verification_trials(parent, max_age_seconds=3600)
            self.assertEqual((1, 1), (candidates, reclaimed))
            self.assertTrue(unrelated.is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows watcher arming boundary")
    def test_watcher_start_boundary_is_already_armed(self) -> None:
        with tempfile.TemporaryDirectory(dir=LOCAL_TEMP) as raw:
            root = Path(raw)
            watcher = _DirectoryWatcher(root)
            self.assertTrue(watcher.start(), watcher.errors)
            (root / "immediate.txt").write_text("armed", encoding="utf-8")
            time.sleep(0.2)
            watcher.stop()
            self.assertFalse(watcher.errors)
            self.assertTrue(any(name.endswith("immediate.txt") for _action, name in watcher.events))

    @unittest.skipUnless(os.name == "nt", "Windows Job Object physical check")
    def test_windows_process_is_attached_before_execution(self) -> None:
        result = run_supervised([sys.executable, "-c", "print('ok')"], ROOT, timeout_seconds=10)
        self.assertEqual(0, result.returncode)
        self.assertTrue(result.supervision.attach_before_execution)


if __name__ == "__main__":
    unittest.main()
