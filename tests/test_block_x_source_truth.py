from __future__ import annotations

# BLOCK_X_SOURCE_TRUTH_V1

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import source_snapshot
from baseline_constraint_verifier import BaselineVerifyConfig, verify_assignment
from verification_proof import source_snapshot_fingerprint


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def init_repo(root: Path) -> None:
    git(root, "init", "-b", "master")
    git(root, "config", "user.email", "block-x@example.invalid")
    git(root, "config", "user.name", "Block X")


class BlockXSourceTruthTests(unittest.TestCase):
    def tearDown(self) -> None:
        source_snapshot.clear_source_snapshot_epochs()

    def _repo(self, root: Path, *, nested: bool = False) -> Path:
        init_repo(root)
        project = root / "frontend" if nested else root
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(
            json.dumps({"name": "demo", "private": True, "dependencies": {}}),
            encoding="utf-8",
        )
        (project / "src.txt").write_text("committed", encoding="utf-8")
        (root / ".gitignore").write_text("ignored.env\nnode_modules/\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-m", "initial")
        return project

    def test_dirty_untracked_and_ignored_bytes_are_the_verified_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._repo(root)
            (project / "src.txt").write_text("DIRTY", encoding="utf-8")
            (project / "untracked.ts").write_text("UNTRACKED", encoding="utf-8")
            (project / "ignored.env").write_text("SECRET_INPUT", encoding="utf-8")

            snapshot = source_snapshot.activate_source_snapshot_epoch(project, replace=True)
            self.assertEqual("DIRTY", (snapshot.project_path / "src.txt").read_text(encoding="utf-8"))
            self.assertEqual("UNTRACKED", (snapshot.project_path / "untracked.ts").read_text(encoding="utf-8"))
            self.assertEqual("SECRET_INPUT", (snapshot.project_path / "ignored.env").read_text(encoding="utf-8"))
            self.assertEqual(snapshot.key, source_snapshot_fingerprint(project))

    def test_ignored_semantic_content_changes_source_key_between_epochs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._repo(root)
            ignored = project / "ignored.env"
            ignored.write_text("one", encoding="utf-8")
            first = source_snapshot.activate_source_snapshot_epoch(project, replace=True).key
            ignored.write_text("two", encoding="utf-8")
            second = source_snapshot.activate_source_snapshot_epoch(project, replace=True).key
            self.assertNotEqual(first, second)

    def test_live_edit_after_seal_does_not_mutate_active_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._repo(root)
            snapshot = source_snapshot.activate_source_snapshot_epoch(project, replace=True)
            sealed = (snapshot.project_path / "src.txt").read_text(encoding="utf-8")
            key = snapshot.key
            (project / "src.txt").write_text("edited-after-seal", encoding="utf-8")
            self.assertEqual(sealed, (snapshot.project_path / "src.txt").read_text(encoding="utf-8"))
            self.assertEqual(key, source_snapshot.source_snapshot_fingerprint(project))

    def test_detached_head_is_preserved_in_snapshot_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._repo(root)
            first = git(root, "rev-parse", "HEAD").stdout.strip()
            (project / "src.txt").write_text("second", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-m", "second")
            git(root, "checkout", "--detach", first)

            snapshot = source_snapshot.activate_source_snapshot_epoch(project, replace=True)
            observed = git(snapshot.project_path, "rev-parse", "HEAD").stdout.strip()
            branch = git(snapshot.project_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            self.assertEqual(first, snapshot.git_head)
            self.assertEqual(first, observed)
            self.assertEqual("HEAD", branch)

    def test_nested_package_relative_path_survives_capture_and_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            root = Path(tmp)
            project = self._repo(root, nested=True)
            snapshot = source_snapshot.activate_source_snapshot_epoch(project, replace=True)
            self.assertEqual(Path("frontend"), snapshot.project_relative)
            target = Path(out) / "verify"
            materialized, same_snapshot, _method = source_snapshot.materialize_source_for_verification(
                project, target, timeout_seconds=60
            )
            self.assertEqual(snapshot.key, same_snapshot.key)
            self.assertTrue((materialized / "package.json").is_file())
            self.assertEqual(snapshot.key, source_snapshot.source_snapshot_fingerprint(project))

    def test_manifest_sidecar_hashes_secret_without_storing_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._repo(root)
            secret = "DO_NOT_LEAK_THIS_SECRET_49382"
            (project / "ignored.env").write_text(secret, encoding="utf-8")
            snapshot = source_snapshot.activate_source_snapshot_epoch(project, replace=True)
            sidecar = (snapshot.container / "manifest.json").read_text(encoding="utf-8")
            self.assertIn("ignored.env", sidecar)
            self.assertNotIn(secret, sidecar)

    def test_git_gc_after_seal_does_not_break_snapshot_git_object_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._repo(root)
            snapshot = source_snapshot.activate_source_snapshot_epoch(project, replace=True)
            expected = snapshot.git_head
            git(root, "gc", "--prune=now")
            self.assertEqual(expected, git(snapshot.project_path, "rev-parse", "HEAD").stdout.strip())
            self.assertEqual(0, git(snapshot.project_path, "fsck", "--no-progress", check=False).returncode)

    def test_uninitialized_submodule_fails_closed(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as subtmp:
            root = Path(tmp)
            project = self._repo(root)
            sub = Path(subtmp)
            init_repo(sub)
            (sub / "sub.txt").write_text("submodule", encoding="utf-8")
            git(sub, "add", ".")
            git(sub, "commit", "-m", "sub")
            added = subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "-C", str(root), "submodule", "add", str(sub), "vendor/sub"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if added.returncode != 0:
                self.skipTest(f"local submodule add unavailable: {added.stderr}")
            git(root, "commit", "-am", "submodule")
            git(root, "submodule", "deinit", "-f", "--", "vendor/sub")
            with self.assertRaisesRegex(source_snapshot.SourceCaptureError, "SOURCE_SUBMODULE_INCOMPLETE"):
                source_snapshot.activate_source_snapshot_epoch(project, replace=True)

    def test_external_symlink_escape_fails_closed(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink unsupported")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
            root = Path(tmp)
            project = self._repo(root)
            outside = Path(outside_tmp) / "secret.txt"
            outside.write_text("outside", encoding="utf-8")
            link = project / "escape-link"
            try:
                os.symlink(outside, link)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(source_snapshot.SourceCaptureError, "SOURCE_SYMLINK_ESCAPE"):
                source_snapshot.activate_source_snapshot_epoch(project, replace=True)

    def test_absolute_nested_gitdir_and_external_alternates_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
            root = Path(tmp)
            project = self._repo(root)
            nested = project / "nested"
            nested.mkdir()
            (nested / ".git").write_text(
                f"gitdir: {root / '.git'}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                source_snapshot.SourceCaptureError,
                "SOURCE_GIT_ABSOLUTE_INDIRECTION_UNSUPPORTED",
            ):
                source_snapshot.capture_source_snapshot(project, timeout_seconds=60)

            (nested / ".git").unlink()
            alternates = root / ".git" / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(str(Path(outside_tmp).resolve()), encoding="utf-8")
            with self.assertRaisesRegex(
                source_snapshot.SourceCaptureError,
                "SOURCE_GIT_ABSOLUTE_INDIRECTION_UNSUPPORTED",
            ):
                source_snapshot.capture_source_snapshot(project, timeout_seconds=60)

    def test_durable_snapshot_detects_same_size_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state:
            root = Path(tmp)
            project = self._repo(root)
            durable = source_snapshot.capture_durable_source_snapshot(
                project,
                Path(state) / "durable",
                timeout_seconds=60,
            )
            sealed_file = durable.project_path / "src.txt"
            before = sealed_file.stat()
            sealed_file.write_text("tampered!", encoding="utf-8")
            os.utime(sealed_file, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(
                source_snapshot.SourceCaptureError,
                "SOURCE_SNAPSHOT_CONTENT_MISMATCH",
            ):
                source_snapshot.open_source_snapshot(
                    durable.container,
                    expected_key=durable.key,
                    timeout_seconds=60,
                )

    def test_capture_instability_retries_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self._repo(root)
            real = source_snapshot._capture_once
            calls = 0

            def unstable(*args, **kwargs):
                nonlocal calls
                calls += 1
                raise source_snapshot.SourceCaptureError("SOURCE_CAPTURE_UNSTABLE: injected")

            with mock.patch("source_snapshot._capture_once", side_effect=unstable):
                with self.assertRaisesRegex(source_snapshot.SourceCaptureError, "SOURCE_CAPTURE_UNSTABLE"):
                    source_snapshot.capture_source_snapshot(project, timeout_seconds=10)
            self.assertEqual(source_snapshot.SOURCE_CAPTURE_RETRIES, calls)
            self.assertTrue(callable(real))

    def test_source_capture_failure_is_infrastructure_not_nogood(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text('{"name":"demo","private":true}', encoding="utf-8")
            config = BaselineVerifyConfig(enabled=True, commands=())
            with mock.patch(
                "baseline_constraint_verifier.materialize_source_for_verification",
                side_effect=source_snapshot.SourceCaptureError("SOURCE_CAPTURE_UNSTABLE: injected"),
            ):
                result = verify_assignment(project, {}, config=config, run_project_checks=False)
            self.assertFalse(result.ok)
            self.assertEqual("infrastructure", result.kind)
            self.assertFalse(result.hard_failure)

    def test_authoritative_verifier_contains_no_shared_git_clone_path(self) -> None:
        verifier = (Path(__file__).resolve().parents[1] / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        self.assertNotIn('"clone", "--quiet", "--shared"', verifier)
        self.assertIn("materialize_source_for_verification", verifier)

    def test_final_proof_no_longer_rejects_dirty_source(self) -> None:
        generator = (Path(__file__).resolve().parents[1] / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertNotIn("PROVEN_DEPENDENCY_SOURCE_DIRTY", generator)
        self.assertIn("source_snapshot_provenance_head", generator)
        self.assertIn("activate_source_snapshot_epoch", generator)


if __name__ == "__main__":
    unittest.main()
