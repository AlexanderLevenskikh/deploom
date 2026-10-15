"""Real filesystem/Windows lease boundaries around a durable artifact publisher."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import baseline_constraint_verifier as verifier
import block_v_prepared_artifact as store
from prepared_workspace_fastpath import try_acquire_snapshot_cleanup_lease

class PreparedPublicationOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'DEPLOOM_VERIFICATION_ROOT': str(self.root / 'verification')})
        self.environment.start(); self.addCleanup(self.environment.stop)
        previous_store = store._CONFIGURED_ROOT
        self.addCleanup(setattr, store, "_CONFIGURED_ROOT", previous_store)
        with patch.object(store, 'schedule_prepared_artifact_maintenance', return_value=True):
            store.configure_prepared_artifact_store(self.root / 'proofs')
        self.source = self.root / 'preserved-source'; self.source.mkdir()
        (self.source / 'keep.txt').write_text('verified original', encoding='utf-8')
        self.workspace = self.root / 'trial'; self.workspace.mkdir()
        (self.workspace / 'package.json').write_text('{"name":"fixture","private":true}', encoding='utf-8')
        (self.workspace / 'input.txt').write_text('prepared input', encoding='utf-8')
        self.key = 'a' * 64
        self.slot = verifier._prepared_snapshot_slot(self.key, self.source)
        verifier._PREPARED_SNAPSHOTS.pop(self.slot, None)
        self.addCleanup(verifier._PREPARED_SNAPSHOTS.pop, self.slot, None)

    def publish(self):
        return verifier._publish_prepared_workspace_snapshot(self.workspace, self.workspace, key=self.key, observed_versions={}, observed_hash='b'*64, source_project=self.source, progress=lambda message: None)

    def test_rejected_integrity_never_exposes_cache_or_repeats_source_scan(self):
        def reject(**values):
            self.assertNotIn(self.slot, verifier._PREPARED_SNAPSHOTS)
            values['source_closure_receipt']['publicationError'] = 'PREPARED_ARTIFACT_FILE_UNREADABLE: synthetic disappearance'
            return False
        with patch.object(verifier, 'publish_prepared_artifact_record', side_effect=reject), patch('artifact_integrity.build_artifact_tree_integrity', side_effect=AssertionError('known corrupt tree must not be scanned again')):
            with self.assertRaisesRegex(RuntimeError, 'PUBLICATION_REJECTED.*FILE_UNREADABLE'):
                self.publish()
        self.assertNotIn(self.slot, verifier._PREPARED_SNAPSHOTS)
        self.assertEqual('verified original', (self.source / 'keep.txt').read_text(encoding='utf-8'))

    @unittest.skipUnless(os.name == 'nt', 'Windows snapshot leases')
    def test_real_windows_publisher_blocks_retirement_until_seal_and_return(self):
        def seal(**values):
            self.assertNotIn(self.slot, verifier._PREPARED_SNAPSHOTS)
            self.assertIsNone(try_acquire_snapshot_cleanup_lease(values['workspace_root']))
            result = store.publish_prepared_artifact_record(**values)
            self.assertTrue(result)
            self.assertFalse(store.invalidate_prepared_artifact_record(self.key, remove_tree=True))
            self.assertTrue(values['workspace_root'].is_dir())
            return result
        with patch.object(verifier, 'publish_prepared_artifact_record', side_effect=seal):
            snapshot = self.publish()
        self.assertTrue(snapshot.durable_publication_confirmed)
        lease = try_acquire_snapshot_cleanup_lease(snapshot.workspace_root)
        self.assertIsNotNone(lease)
        lease.close()
        self.assertEqual('prepared input', (snapshot.workspace_root / 'input.txt').read_text(encoding='utf-8'))
