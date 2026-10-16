"""Physical continuity and copy-volume checks for guarded Git object reuse."""
import dataclasses
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import artifact_integrity as integrity
import prepared_workspace_fastpath as fast
from verification_source_closure import validate_materialized_source_closure


@unittest.skipUnless(os.name == "nt", "Windows guarded Git object lower")
class GuardedGitObjectsTests(unittest.TestCase):
    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def fixture(self, root):
        prepared = root / "prepared"
        package = prepared / "node_modules" / "demo"
        package.mkdir(parents=True)
        (prepared / "package.json").write_text('{"name":"git-lower-demo","private":true}')
        (prepared / "source.txt").write_text("sealed-source")
        (package / "package.json").write_text('{"name":"demo","version":"1.0.0"}')
        self.git(prepared, "init", "-q")
        self.git(prepared, "add", "package.json", "source.txt")
        self.git(prepared, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                 "commit", "-qm", "fixture")
        self.git(prepared, "tag", "v1.0.0")
        # Many immutable history payloads make accidental copying/hashing visible.
        pack = prepared / ".git" / "objects" / "pack"
        for index in range(128):
            (pack / f"fixture-{index}.payload").write_bytes(bytes([index]) * 32768)
        seal = integrity.build_artifact_tree_integrity(prepared, source_only=True)
        snapshot = dataclasses.make_dataclass("SourceSeal",
            ["source_closure_content_key", "source_closure_entries"])(
                seal.source_content_key, seal.source_entries)
        return prepared.resolve(), snapshot

    def clone(self, prepared, target):
        project = fast.try_materialize_guarded_clone(source_project=prepared,
            prepared_workspace_root=prepared, project_relative=Path("."), target=target,
            dependency_roots=fast.dependency_root_manifest(prepared))
        self.addCleanup(fast.cleanup_guarded_clone, target)
        self.assertIsNotNone(project)
        return project

    def test_history_and_dependencies_are_shared_but_index_and_source_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, snapshot = self.fixture(Path(tmp))
            target = Path(tmp) / "trial"
            with patch.object(fast, "_run_overlay_robocopy", wraps=fast._run_overlay_robocopy) as copy:
                project = self.clone(prepared, target)
            self.assertIn(prepared / ".git" / "objects", copy.call_args.args[2])
            self.assertIn(prepared / "node_modules", copy.call_args.args[2])
            self.assertTrue((project / ".git/objects").samefile(prepared / ".git/objects"))
            self.assertTrue((project / "node_modules/demo").samefile(prepared / "node_modules/demo"))
            self.assertFalse((project / ".git/index").samefile(prepared / ".git/index"))
            original_index = hashlib.sha256((prepared / ".git/index").read_bytes()).hexdigest()
            with patch.object(integrity, "_hash_file", wraps=integrity._hash_file) as hashes:
                validate_materialized_source_closure(snapshot, target)
            self.assertFalse(any(".git/objects/" in str(call.args[0]).replace("\\", "/")
                                 for call in hashes.call_args_list))
            self.assertEqual(self.git(prepared, "rev-parse", "HEAD"), self.git(project, "rev-parse", "HEAD"))
            self.assertEqual("v1.0.0", self.git(project, "describe", "--tags", "--always"))
            (project / "source.txt").write_text("private repair")
            self.git(project, "update-index", "--assume-unchanged", "source.txt")
            self.assertEqual(original_index, hashlib.sha256((prepared / ".git/index").read_bytes()).hexdigest())
            self.assertEqual("sealed-source", (prepared / "source.txt").read_text())
            result = fast.stop_guarded_clone(target)
            self.assertEqual((), result.errors)
            self.assertEqual((), result.notification_only)
            fast.cleanup_guarded_clone(target)
            self.assertTrue((prepared / ".git/objects/pack/fixture-0.payload").is_file())

    def test_shared_git_write_rejects_continuity(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, snapshot = self.fixture(Path(tmp))
            target = Path(tmp) / "trial"
            project = self.clone(prepared, target)
            validate_materialized_source_closure(snapshot, target)
            (project / ".git/objects/pack/fixture-0.payload").write_bytes(b"changed")
            result = fast.stop_guarded_clone(target)
            self.assertTrue(result.notification_only or result.errors)
            fast.cleanup_guarded_clone(target)

    def test_private_source_change_cannot_reuse_shared_history_to_claim_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, snapshot = self.fixture(Path(tmp))
            target = Path(tmp) / "trial"
            project = self.clone(prepared, target)
            (project / "source.txt").write_text("changed")
            with self.assertRaisesRegex(integrity.ArtifactIntegrityError, "MATERIALIZED_MISMATCH"):
                validate_materialized_source_closure(snapshot, target)
            fast.cleanup_guarded_clone(target)

    def test_replaced_history_mapping_rejects_even_if_original_lower_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, _snapshot = self.fixture(Path(tmp))
            target = Path(tmp) / "trial"
            project = self.clone(prepared, target)
            outside = Path(tmp) / "unrelated"; outside.mkdir()
            fast._remove_junction(project / ".git/objects")
            self.assertTrue(fast._create_junction(project / ".git/objects", outside))
            result = fast.stop_guarded_clone(target)
            self.assertTrue(result.errors)
            fast.cleanup_guarded_clone(target)

    def test_real_verifier_runs_git_commands_with_shared_history_and_keeps_source_clean(self):
        import json
        import shutil
        import baseline_constraint_verifier as verifier
        import source_snapshot as source
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve(); repo = base / "repo"
            app = repo / "packages/app"; lib = repo / "packages/lib"
            app.mkdir(parents=True); lib.mkdir(parents=True)
            (repo / "package.json").write_text(json.dumps({"name":"git-proof-fixture","private":True,"workspaces":["packages/*"]}))
            (repo / ".gitignore").write_text("node_modules/\n")
            (app / "package.json").write_text(json.dumps({"name":"fixture-app","version":"1.0.0","dependencies":{"fixture-lib":"1.0.0"}}))
            (lib / "package.json").write_text(json.dumps({"name":"fixture-lib","version":"1.0.0","main":"index.cjs"}))
            (lib / "index.cjs").write_text("module.exports=42;")
            (app / "check.cjs").write_text(
                "const cp=require('child_process');"
                "if(require('fixture-lib')!==42)throw Error('dependency');"
                "if(cp.execFileSync('git',['describe','--tags','--always'],{encoding:'utf8'}).trim()!=='v1.0.0')throw Error('history');"
                "console.log('GIT_HISTORY_AND_DEPENDENCY_CONFIRMED');")
            argv, _ = verifier._command_prefix(shutil.which("npm"), ["install","--ignore-scripts","--no-audit","--no-fund"])
            installed = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=120)
            self.assertEqual(0, installed.returncode, installed.stdout + installed.stderr)
            for args in (("init","-q"),("add","."),("-c","user.name=Fixture","-c","user.email=fixture@example.com","commit","-qm","fixture"),("tag","v1.0.0")):
                self.git(repo, *args)
            original_head = self.git(repo, "rev-parse", "HEAD")
            config = verifier.BaselineVerifyConfig(project_checks="strict", commands=("node check.cjs",),
                timeout_seconds=120, attempt_timeout_seconds=360, proof_cache_dir=str(base / "proofs"))
            observed = []; real_run = verifier._run
            def run(argv, cwd, **kwargs):
                if kwargs.get("progress_label") == "node check.cjs":
                    command_root = Path(cwd).resolve().parents[1]
                    observed.append(fast.guarded_clone_git_objects(command_root))
                return real_run(argv, cwd, **kwargs)
            self.addCleanup(source.clear_source_snapshot_epochs)
            with patch.dict(os.environ, {"DEPLOOM_VERIFICATION_ROOT":str(base / "storage")}), patch.object(verifier, "_run", run):
                result = verifier.verify_assignment(app, {}, config=config, run_project_checks=True)
            self.assertTrue(result.ok, result.summary + "\n" + result.output)
            self.assertEqual([(".git/objects",)], observed)
            self.assertEqual(original_head, self.git(repo, "rev-parse", "HEAD"))
            self.assertEqual("", self.git(repo, "status", "--porcelain"))

    def test_git_watcher_failure_falls_back_without_sharing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, _snapshot = self.fixture(Path(tmp))
            target = Path(tmp) / "trial"
            start = fast._DirectoryWatcher.start
            def arm(watcher):
                return False if watcher.root == prepared / ".git/objects" else start(watcher)
            with patch.object(fast._DirectoryWatcher, "start", arm):
                project = fast.try_materialize_guarded_clone(source_project=prepared,
                    prepared_workspace_root=prepared, project_relative=Path("."), target=target,
                    dependency_roots=fast.dependency_root_manifest(prepared))
            self.assertIsNone(project)
            self.assertFalse(target.exists())
            self.assertTrue((prepared / ".git/objects/pack/fixture-0.payload").is_file())


if __name__ == "__main__":
    unittest.main()
