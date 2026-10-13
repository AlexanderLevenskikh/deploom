"""Completed-request reuse is advisory; existing source guards grant authority."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import source_snapshot as source

class CompletedSourceReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"app";self.root.mkdir()
        (self.root/"package.json").write_text('{"name":"fixture"}')
        (self.root/"input.txt").write_text("sealed")
        self.addCleanup(source.clear_source_snapshot_epochs)
    def scope(self):return source.verification_source_snapshot_scope(self.root,reuse_completed=True)

    def test_unchanged_requests_capture_once_and_remain_context_local(self):
        real=source.capture_source_snapshot
        with patch.object(source,"capture_source_snapshot",wraps=real) as capture:
            with self.scope() as first:source.validate_source_snapshot(first)
            self.assertIsNone(source.active_source_snapshot(self.root))
            with self.scope() as second:
                self.assertIs(first,second);source.validate_source_snapshot(second)
            self.assertEqual(1,capture.call_count)
        source.clear_source_snapshot_epochs();self.assertFalse(first.container.exists())

    @unittest.skipUnless(os.name=="nt","proven Windows notification continuity")
    def test_quiet_completed_reuse_defers_live_manifest_without_rehashing_sealed_bytes(self):
        # v0.2.233 postpones the ORIGINAL-tree continuity pass until the next
        # consumer. The sealed-tree watcher can still validate by notification,
        # but an unarmed live-tree watcher requires one authoritative hash pass.
        real=source.build_source_tree_manifest
        with patch.object(source,"build_source_tree_manifest",wraps=real) as manifest:
            with self.scope() as first:
                source.validate_source_snapshot(first)
                after_sealed_validation=manifest.call_count
            before=manifest.call_count
            self.assertEqual(after_sealed_validation, before,
                             "completed scope must not eagerly hash the live tree")
            with self.scope() as second:
                self.assertIs(first,second);source.validate_source_snapshot(second)
            deferred_calls=manifest.call_args_list[before:]
            self.assertEqual(1,len(deferred_calls),
                             "quiet reuse should need only the deferred live-source pass")
            self.assertEqual("hot SourceSnapshot live-source continuity",
                             deferred_calls[0].kwargs.get("progress_label"),
                             "unexpected extra sealed-source hashing during reuse")
            self.assertEqual(self.root.resolve(),Path(deferred_calls[0].args[0]).resolve())
        source.clear_source_snapshot_epochs()
        self.assertFalse(any(entry[0] is first for entry in source._ACTIVE_WATCHERS.values()))

    def test_tool_build_change_cannot_reuse_another_producers_subject(self):
        with patch.object(source,"tool_build_id",return_value="a"*64):
            with self.scope() as first:pass
        with patch.object(source,"tool_build_id",return_value="b"*64):
            with self.scope() as second:
                self.assertIsNot(first,second)
                self.assertNotEqual(first.key,second.key)

    def test_live_source_mutation_recaptures_instead_of_reusing_other_bytes(self):
        with self.scope() as first:pass
        (self.root/"input.txt").write_text("changed")
        with self.scope() as second:
            self.assertNotEqual(first.key,second.key)
            self.assertEqual("changed",(second.project_path/"input.txt").read_text())
        self.assertFalse(first.container.exists())

    def test_tampered_sealed_bytes_never_authorize_reuse(self):
        with self.scope() as first:source.validate_source_snapshot(first)
        victim=first.project_path/"input.txt";victim.chmod(victim.stat().st_mode|stat.S_IWRITE);victim.write_text("damage")
        with self.scope() as second:
            self.assertIsNot(first,second)
            self.assertEqual("sealed",(second.project_path/"input.txt").read_text())
        self.assertFalse(first.container.exists())

    def test_concurrent_leases_do_not_share_owned_source_copy(self):
        with self.scope() as first:pass
        with self.scope() as leased:
            self.assertIs(first,leased)
            def worker():
                with self.scope() as other:
                    source.validate_source_snapshot(other)
                    return other.container
            with ThreadPoolExecutor(max_workers=1) as executor:
                other_container=executor.submit(worker).result()
            self.assertNotEqual(leased.container,other_container)
            self.assertTrue(leased.root.is_dir());source.validate_source_snapshot(leased)

    def test_failed_scope_does_not_retain_owned_snapshot(self):
        with self.assertRaisesRegex(RuntimeError,"fixture"):
            with self.scope() as snapshot:raise RuntimeError("fixture")
        self.assertFalse(snapshot.container.exists())
        self.assertFalse(source._COMPLETED_REQUEST_SNAPSHOTS)

    def test_excluded_noise_does_not_force_a_recapture(self):
        with self.scope() as first:pass
        noise=self.root/"node_modules";noise.mkdir();(noise/"noise.txt").write_text("noise")
        with self.scope() as second:self.assertIs(first,second)

    def test_idle_pool_is_bounded_and_cleanup_removes_every_owned_tree(self):
        containers=[]
        for index in range(source._COMPLETED_REQUEST_LIMIT+2):
            app=self.root.parent/str(index);app.mkdir();(app/"package.json").write_text('{"name":"fixture"}')
            with source.verification_source_snapshot_scope(app,reuse_completed=True) as snapshot:containers.append(snapshot.container)
            self.assertLessEqual(len(source._COMPLETED_REQUEST_SNAPSHOTS),source._COMPLETED_REQUEST_LIMIT)
        self.assertFalse(containers[0].exists())
        source.clear_source_snapshot_epochs();self.assertFalse(any(path.exists() for path in containers))

if __name__=="__main__":unittest.main()
