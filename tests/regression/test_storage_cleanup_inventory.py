import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import storage_cleanup_inventory as s

class StorageInventorySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name)
        self.root = self.base / 'verification'; (self.root / 'trials').mkdir(parents=True)
        self.workspace = self.base / 'workspace'; self.run = self.workspace / '.dependency-roadmap' / 'iterative' / 'demo'
        self.run.mkdir(parents=True)
    def tearDown(self): self.temp.cleanup()
    def trash(self):
        stamp = time.time_ns() - 3 * s.MIN_AGE_SECONDS * 10**9
        p = self.root / 'trials' / f'.deploom-trial-trash-dependency-flow-baseline-verify-fixture-999999-{stamp}'
        p.mkdir(); (p / 'payload').write_bytes(b'garbage'); os.utime(p, (time.time()-3*s.MIN_AGE_SECONDS,)*2)
        return p
    def inspect(self):
        with contextlib.redirect_stdout(io.StringIO()): return s.inspect(self.root, [str(self.workspace)])
    def clean(self, plan):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): result = s.clean(self.root, [str(self.workspace)], plan)
        return result, out.getvalue()
    def archive(self):
        archive = self.run / 'run-archive' / 'saved'; (archive / 'trial' / 'workspace').mkdir(parents=True)
        (archive / 'trial' / 'workspace' / 'payload').write_text('generated trial')
        (archive / 'sources' / 'C6' / 'tree').mkdir(parents=True); (archive / 'sources' / 'C6' / 'keep').write_text('verified')
        (archive / 'sources' / 'C6' / 'manifest.json').write_text(json.dumps({'type':'deploom-source-snapshot','sourceSnapshotKey':'sealed-key'}))
        (archive / 'reports').mkdir(); (archive / 'reports' / 'keep.md').write_text('report')
        (archive / 'checkpoints').mkdir(); (archive / 'checkpoints' / 'C6.json').write_text(json.dumps({'status':'VERIFIED','checkpointId':'C6','sourceSnapshotKey':'sealed-key'}))
        (archive / 'run.json').write_text(json.dumps({'phase':'TERMINAL','runId':'old'}))
        os.utime(archive, (time.time()-3*s.MIN_AGE_SECONDS,)*2)
        return archive
    def test_inspection_does_not_rename_delete_or_write(self):
        garbage=self.trash(); before=list(self.root.rglob('*')); plan=self.inspect()
        self.assertTrue(garbage.exists()); self.assertEqual(before,list(self.root.rglob('*')))
        self.assertEqual(plan['eligible'],1); self.assertEqual(plan['eligibleBytes'],7)
    def test_clean_emits_real_progress_and_preserves_unknown_active_and_current_data(self):
        garbage=self.trash()
        live=self.root/'trials'/'dependency-flow-resolver-seed-live';live.mkdir();(live/'keep').write_text('keep')
        current=self.run/'sources'/'C6';current.mkdir(parents=True);(current/'keep').write_text('verified')
        plan=self.inspect();result,events=self.clean(plan)
        self.assertFalse(garbage.exists());self.assertTrue((live/'keep').exists());self.assertTrue((current/'keep').exists())
        self.assertEqual(result['removed'],1);self.assertEqual(result['reclaimedBytes'],7)
        parsed=[json.loads(line.split(' ',1)[1]) for line in events.splitlines()]
        self.assertEqual(parsed[-1]['percent'],100);self.assertEqual(parsed[-1]['processed'],1)
    def test_changed_tree_after_preview_is_preserved(self):
        garbage=self.trash();plan=self.inspect();(garbage/'new-user-file').write_text('keep')
        os.utime(garbage,(time.time()-3*s.MIN_AGE_SECONDS,)*2)
        result,_=self.clean(plan)
        self.assertTrue((garbage/'payload').exists());self.assertTrue((garbage/'new-user-file').exists())
        self.assertEqual(result['removed'],0);self.assertEqual(result['results'][0]['reason'],'changed-after-preview')
    def test_unknown_legacy_private_roots_are_protected(self):
        p=self.root/'trials'/'dependency-flow-same-run-prepared-legacy';p.mkdir();(p/'keep').write_text('unknown')
        os.utime(p,(time.time()-3*s.MIN_AGE_SECONDS,)*2)
        self.assertEqual(self.inspect()['eligible'],0);self.assertTrue((p/'keep').exists())
    def test_dead_owner_private_root_is_recognized_only_with_evidence(self):
        p=self.root/'trials'/'dependency-flow-resolver-seed-owned';p.mkdir();(p/'payload').write_text('cache')
        (p/'.deploom-trial-owner.json').write_text(json.dumps({'pid':999999}))
        os.utime(p,(time.time()-3*s.MIN_AGE_SECONDS,)*2)
        with patch('verification_storage_maintenance.owner_alive',return_value=False):
            plan=self.inspect();result,_=self.clean(plan)
        self.assertEqual(result['removed'],1);self.assertFalse(p.exists())
    def test_hardlink_protects_original_bytes_and_permissions(self):
        garbage=self.trash();outside=self.base/'source';outside.write_bytes(b'original')
        os.link(outside,garbage/'shared');before=outside.stat().st_mode
        plan=self.inspect();self.assertEqual(plan['eligible'],0)
        self.clean(plan);self.assertTrue((garbage/'shared').exists());self.assertEqual(outside.read_bytes(),b'original');self.assertEqual(outside.stat().st_mode,before)
    def test_symlink_or_junction_is_never_followed(self):
        garbage=self.trash();outside=self.base/'source';outside.mkdir();(outside/'keep').write_text('keep')
        try:os.symlink(outside,garbage/'link',target_is_directory=True)
        except OSError:
            if os.name != 'nt': self.skipTest('symlink unavailable')
            import subprocess
            result = subprocess.run(['cmd','/c','mklink','/J',str(garbage/'link'),str(outside)],capture_output=True)
            if result.returncode: self.skipTest('junction unavailable')
        plan=self.inspect();self.assertEqual(plan['eligible'],0);self.clean(plan);self.assertTrue((outside/'keep').exists())
    def test_archived_trial_cleanup_preserves_checkpoint_sources_and_reports(self):
        archive=self.archive();plan=self.inspect();self.assertEqual(plan['eligible'],1)
        result,_=self.clean(plan);self.assertEqual(result['removed'],1)
        self.assertFalse((archive/'trial'/'workspace').exists());self.assertTrue((archive/'sources'/'C6'/'keep').exists());self.assertTrue((archive/'reports'/'keep.md').exists())
    def test_active_reference_blocks_archive_trial(self):
        archive=self.archive();(self.run/'security.json').write_text(json.dumps({'workspaceRoot':str(archive/'trial'/'workspace')}))
        plan=self.inspect();self.assertEqual(plan['eligible'],0);self.clean(plan);self.assertTrue((archive/'trial'/'workspace').exists())
    def test_new_reference_after_preview_blocks_archive_trial(self):
        archive=self.archive();plan=self.inspect();(self.run/'handoff.json').write_text(json.dumps({'sourceRef':f'run-archive/{archive.name}/trial/workspace'}))
        result,_=self.clean(plan);self.assertEqual(result['removed'],0);self.assertTrue((archive/'trial'/'workspace').exists())
    def test_unfinished_archive_preserves_uncommitted_work(self):
        archive=self.archive();(archive/'run.json').write_text(json.dumps({'runId':'old','phase':'REPAIR_REQUIRED'}))
        self.assertEqual(self.inspect()['eligible'],0)
    def test_detached_prepared_trash_is_separate_from_published_cache(self):
        cache=self.root/'baseline-prepared-artifacts';(cache/'index').mkdir(parents=True);(cache/'trees').mkdir()
        trash=cache/'trash'/'abcdef123456-temp.abcdef123456.999999.1234567890';trash.mkdir(parents=True);(trash/'payload').write_text('cache')
        os.utime(trash,(time.time()-3*s.MIN_AGE_SECONDS,)*2)
        (cache/'trees'/'keep').write_text('published')
        with patch('storage_cleanup_inventory.owner_alive',return_value=False):
            plan=self.inspect();result,_=self.clean(plan)
        self.assertEqual(result['removed'],1);self.assertFalse(trash.exists());self.assertTrue((cache/'trees'/'keep').exists())
    def test_published_record_reference_protects_prepared_trash(self):
        cache=self.root/'baseline-prepared-artifacts';(cache/'index').mkdir(parents=True)
        trash=cache/'trash'/'abcdef123456-temp.abcdef123456.999999.1234567890';trash.mkdir(parents=True);(trash/'payload').write_text('keep')
        os.utime(trash,(time.time()-3*s.MIN_AGE_SECONDS,)*2)
        (cache/'index'/'published.json').write_text(json.dumps({'workspaceRoot':str(trash/'workspace')}))
        with patch('storage_cleanup_inventory.owner_alive',return_value=False):
            self.assertEqual(self.inspect()['eligible'],0)

    def test_saved_archive_candidate_is_preserved(self):
        archive=self.archive();(archive/'trial'/'candidate.json').write_text(json.dumps({'candidateId':'saved-work'}))
        self.assertEqual(self.inspect()['eligible'],0);self.assertTrue((archive/'trial'/'workspace').exists())
    def test_missing_source_preserves_sole_trial_copy(self):
        archive=self.archive();(archive/'sources'/'C6'/'manifest.json').unlink()
        self.assertEqual(self.inspect()['eligible'],0);self.assertTrue((archive/'trial'/'workspace').exists())
    def test_missing_snapshot_key_in_both_records_is_not_preservation_evidence(self):
        archive=self.archive()
        (archive/'checkpoints'/'C6.json').write_text(json.dumps({'status':'VERIFIED','checkpointId':'C6'}))
        (archive/'sources'/'C6'/'manifest.json').write_text(json.dumps({'type':'deploom-source-snapshot'}))
        plan=self.inspect();self.assertEqual(plan['eligible'],0)
        self.clean(plan);self.assertTrue((archive/'trial'/'workspace').exists())

    def test_low_byte_scan_budget_does_not_limit_explicit_inventory_cleanup(self):
        garbage=self.trash();(garbage/'large-payload').write_bytes(b'x'*1024*1024)
        os.utime(garbage,(time.time()-3*s.MIN_AGE_SECONDS,)*2)
        plan=self.inspect();self.assertGreater(plan['eligibleBytes'],512*1024)
        result,_=self.clean(plan);self.assertEqual(result['removed'],1);self.assertFalse(garbage.exists())

    def test_partial_deletion_retirement_restores_owner_evidence(self):
        from verification_storage_maintenance import register_trial_owner, retire_trial
        path=self.root/'trials'/'dependency-flow-resolver-seed-partial';path.mkdir()
        register_trial_owner(path);(path/'.deploom-trial-owner.json').unlink()
        (path/'locked').write_text('remaining garbage')
        retire_trial(path)
        retired=next((self.root/'trials').glob('.deploom-trial-trash-dependency-flow-resolver-seed-partial-*'))
        self.assertEqual(json.loads((retired/'.deploom-trial-owner.json').read_text())['pid'],os.getpid())
        self.assertTrue((retired/'locked').exists())
        os.utime(retired,(time.time()-3*s.MIN_AGE_SECONDS,)*2)
        with patch('verification_storage_maintenance.owner_alive',return_value=False):
            plan=self.inspect();result,_=self.clean(plan)
        self.assertEqual(result['removed'],1);self.assertFalse(retired.exists())

    def test_workspace_inspection_and_cleanup_work_without_a_verification_volume(self):
        archive=self.archive()
        with contextlib.redirect_stdout(io.StringIO()):
            plan=s.inspect(None,[str(self.workspace)])
            result=s.clean(None,[str(self.workspace)],plan)
        self.assertEqual(plan['root'],'');self.assertEqual(plan['eligible'],1)
        self.assertEqual(result['removed'],1);self.assertTrue((archive/'sources'/'C6'/'keep').exists())

    def test_recent_prepared_retirement_is_protected_even_with_old_directory(self):
        cache=self.root/'baseline-prepared-artifacts';(cache/'index').mkdir(parents=True)
        trash=cache/'trash'/f'abcdef123456-temp.abcdef123456.999999.{time.time_ns()}';trash.mkdir(parents=True)
        (trash/'payload').write_text('recent');os.utime(trash,(time.time()-3*s.MIN_AGE_SECONDS,)*2)
        with patch('storage_cleanup_inventory.owner_alive',return_value=False):
            self.assertEqual(self.inspect()['eligible'],0)

if __name__ == '__main__':unittest.main()
