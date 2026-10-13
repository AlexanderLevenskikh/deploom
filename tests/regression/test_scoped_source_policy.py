"""Real isolated copy: working project scope, ignored outputs and pinned validation."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import iterative_delivery as delivery
from scoped_source_policy import MODE_ENV, policy_for_project
import source_snapshot as snapshots
from source_snapshot import capture_durable_source_snapshot, open_source_snapshot, materialize_source_for_verification


class ScopedSourcePolicyTests(unittest.TestCase):
    def test_project_scope_omits_ignored_outputs_and_backend_preserves_tracked_and_checks_bytes(self):
        with tempfile.TemporaryDirectory(prefix="deploom-scoped-source-") as raw:
            base = Path(raw); repo = base / "repo"; front = repo / "front"; front.mkdir(parents=True)
            (front / "package.json").write_text('{"name":"fixture","version":"1.0.0"}',encoding="utf-8")
            (front / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}',encoding="utf-8")
            (front / "source.js").write_text("module.exports=1;",encoding="utf-8")
            (repo / ".gitignore").write_text("bin/\nobj/\nignored-input.txt\n",encoding="utf-8")
            (repo / "backend").mkdir(); (repo / "backend/server.sql").write_text("backend baseline",encoding="utf-8")
            (front / "bin").mkdir(); (front / "bin/explicit-source.txt").write_text("tracked despite ignore",encoding="utf-8")
            delivery.git(repo,"init","-b","fixture");delivery.git(repo,"config","user.email","fixture@example.invalid")
            delivery.git(repo,"config","user.name","Fixture");delivery.git(repo,"add",".")
            delivery.git(repo,"add","-f","front/bin/explicit-source.txt");delivery.git(repo,"commit","-m","fixture")
            (front / "bin/huge-build.dll").write_bytes(b"generated"*131072)
            (front / "ignored-input.txt").write_text("ignored",encoding="utf-8")
            with patch.dict(os.environ,{MODE_ENV:"project-gitignore"}):
                snapshot = capture_durable_source_snapshot(front,base/"snapshot",timeout_seconds=0)
            self.assertTrue((snapshot.root / "backend/server.sql").is_file())
            self.assertTrue((snapshot.project_path / "bin/huge-build.dll").is_file())
            self.assertTrue((snapshot.project_path / "ignored-input.txt").is_file())
            self.assertEqual((snapshot.project_path / "bin/explicit-source.txt").read_text(),"tracked despite ignore")
            self.assertTrue((snapshot.root / ".gitignore").is_file())
            # Stored scope must survive lossless cold storage and restoration.
            import snapshot_compaction as cold
            packed = base / "workspace/.dependency-roadmap/iterative/demo/sources/C0"
            packed.parent.mkdir(parents=True)
            import shutil
            shutil.copytree(base / "snapshot", packed)
            cold.compact(packed)
            self.assertFalse((packed / "tree").exists())
            restored = open_source_snapshot(packed, expected_key=snapshot.key, timeout_seconds=0)
            self.assertTrue((restored.root / "backend/server.sql").is_file())
            self.assertEqual(restored.input_policy, snapshot.input_policy)
            self.assertIn("backend/server.sql",delivery.delivery_files(snapshot.root))
            reopened = open_source_snapshot(base/"snapshot",expected_key=snapshot.key,timeout_seconds=0)
            with patch.dict(snapshots._ACTIVE, {snapshots._active_key(reopened.project_path): reopened}):
                materialized, _, _ = materialize_source_for_verification(reopened.project_path,base/"trial",timeout_seconds=0)
            self.assertTrue((materialized / "source.js").is_file())
            self.assertTrue((base / "trial/backend/server.sql").is_file())

    def test_workspace_scope_keeps_root_lock_and_local_yarn_without_ignored_cache(self):
        with tempfile.TemporaryDirectory(prefix="deploom-workspace-scope-") as raw:
            root=Path(raw); app=root/"packages/app";lib=root/"packages/lib"
            app.mkdir(parents=True);lib.mkdir(parents=True)
            (root/"package.json").write_text(json.dumps({"name":"root","private":True,"workspaces":["./packages/*/"]}),encoding="utf-8")
            (root/"yarn.lock").write_text("# yarn lockfile v1\n",encoding="utf-8")
            (root/".gitignore").write_text(".yarn/\nbin/\n",encoding="utf-8")
            (root/".yarnrc.yml").write_text("yarnPath: .yarn/releases/yarn.cjs\n",encoding="utf-8")
            for p in [app,lib]: (p/"package.json").write_text(json.dumps({"name":p.name}),encoding="utf-8")
            (root/".yarn/releases").mkdir(parents=True);(root/".yarn/cache").mkdir()
            (root/".yarn/releases/yarn.cjs").write_text("local yarn",encoding="utf-8")
            (root/".yarn/cache/large.zip").write_bytes(b"cache")
            delivery.git(root,"init","-b","fixture")
            with patch.dict(os.environ,{MODE_ENV:"project-gitignore"}): policy=policy_for_project(app,root)
            self.assertEqual(policy.scope_roots,())
            self.assertNotIn(".yarn/cache",policy.excluded_relative_paths)
            self.assertNotIn(".yarn/releases/yarn.cjs",policy.excluded_relative_paths)
            self.assertNotIn("yarn.lock",policy.excluded_relative_paths)

if __name__ == "__main__": unittest.main()
