"""Crash/publication boundaries for repair archives and new-runtime adoption."""
from argparse import Namespace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import iterative_migration as m
from baseline_constraint_verifier import BaselineVerifyResult


class RepairTransactionBoundaryTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'checkpoints').mkdir()
        (root / 'checkpoints' / 'C0.json').write_text(json.dumps({'schemaVersion':m.SCHEMA_VERSION}), encoding='utf-8')
        (root / 'run.json').write_text(json.dumps({'schemaVersion':m.SCHEMA_VERSION,'runId':'fixture','phase':'READY'}), encoding='utf-8')
        (root / 'run-config.json').write_text(json.dumps({'schemaVersion':m.SCHEMA_VERSION}), encoding='utf-8')
        (root / m.PROJECT_CHECK_FILENAME).write_bytes(b'{"ok":false}\r\n')
        (root / m.REPAIR_HANDOFF_FILENAME).write_bytes(b'{"previous":true}\r\n')

    def assert_restored(self, root):
        for name in ('run.json', 'run-config.json', 'checkpoints/C0.json'):
            self.assertTrue((root / name).exists(), name)
        self.assertEqual((root / m.PROJECT_CHECK_FILENAME).read_bytes(), b'{"ok":false}\r\n')
        self.assertEqual((root / m.REPAIR_HANDOFF_FILENAME).read_bytes(), b'{"previous":true}\r\n')
        self.assertFalse((root / m.REPAIR_ARCHIVE_TRANSACTION).exists())

    def test_publication_failures_restore_every_artifact_and_prior_metadata(self):
        for destination in (m.REPAIR_HANDOFF_FILENAME, m.PROJECT_CHECK_FILENAME, 'commit'):
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                real_write = m._write_json_atomic
                def fail_publication(path, value):
                    if path.name == destination or (destination == 'commit' and value.get('phase') == 'committed'):
                        raise OSError('injected publication failure')
                    real_write(path, value)
                with patch.object(m, '_verify_repair_archive'), patch.object(m, '_build_repair_handoff', return_value={'terminal': 'REPAIR_VERIFIED'}), patch.object(m, '_write_json_atomic', side_effect=fail_publication):
                    with self.assertRaisesRegex(m.InvalidInputError, 'ROLLED_BACK'):
                        m._archive_repair_run_locked(root, {'runId': 'fixture'})
                self.assert_restored(root)

    def test_killed_writer_recovers_on_next_real_cli_launch(self):
        # os._exit bypasses finally/except. Exercise both mid-move and after
        # publishing handoff, with the writer's real lock left behind.
        script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
import iterative_migration as m
root=Path(sys.argv[1]); point=sys.argv[2]
lock=m._RunLock(root, 'crashing-writer'); lock.acquire()
real_rename=m.os.rename
real_write=m._write_json_atomic
count=0
def move(src,dst):
    global count
    real_rename(src,dst); count+=1
    if point=='move' and count==3: os._exit(79)
def write(path,value):
    real_write(path,value)
    if point=='handoff' and path.name==m.REPAIR_HANDOFF_FILENAME: os._exit(79)
    if point=='committed' and path.name==m.REPAIR_ARCHIVE_TRANSACTION and value.get('phase')=='committed': os._exit(79)
with patch.object(m,'_verify_repair_archive'), patch.object(m,'_build_repair_handoff',return_value={'terminal':'REPAIR_VERIFIED'}), patch.object(m.os,'rename',side_effect=move), patch.object(m,'_write_json_atomic',side_effect=write):
    m._archive_repair_run_locked(root, {'runId':'fixture'})
'''
        for point in ('move', 'handoff', 'committed'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); self.fixture(root)
                child=subprocess.run([sys.executable, '-c', script, str(root), point], timeout=30, capture_output=True, text=True)
                self.assertEqual(child.returncode, 79, child.stderr)
                self.assertTrue((root / m.REPAIR_ARCHIVE_TRANSACTION).exists())
                recovered=subprocess.run([sys.executable, 'iterative_migration.py', '--run-dir', str(root), 'archive-repair-run'], timeout=30, capture_output=True, text=True)
                self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                if point == 'committed':
                    self.assertFalse((root / 'run.json').exists(), 'committed archive must not be undone')
                    self.assertFalse((root / m.REPAIR_ARCHIVE_TRANSACTION).exists())
                    self.assertEqual(m._read_json(root / m.REPAIR_HANDOFF_FILENAME)['terminal'], 'REPAIR_VERIFIED')
                    self.assertTrue(m._read_json(root / m.PROJECT_CHECK_FILENAME)['ok'])
                    self.assertEqual(len(list((root / m.REPAIR_ARCHIVE_DIR).glob('*/run.json'))), 1)
                else:
                    self.assert_restored(root)

    def test_failed_rollback_preserves_journal_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); self.fixture(root)
            real_rename=m.os.rename
            def fail_restore(src,dst):
                if Path(src).parent.parent.name == m.REPAIR_ARCHIVE_DIR:
                    raise OSError('restore denied')
                real_rename(src,dst)
            with patch.object(m, '_verify_repair_archive', side_effect=OSError('verify denied')), patch.object(m.os, 'rename', side_effect=fail_restore):
                with self.assertRaisesRegex(m.InvalidInputError, 'RECOVERY_REQUIRED'):
                    m._archive_repair_run_locked(root, {'runId':'fixture'})
            self.assertTrue((root / m.REPAIR_ARCHIVE_TRANSACTION).exists())
            m._recover_repair_archive(root)
            self.assert_restored(root)


class RepairAdoptionBoundaryTests(unittest.TestCase):
    def test_new_runtime_rechecks_fixed_bytes_and_never_reuses_old_audit(self):
        self.exercise(BaselineVerifyResult(True, 'passed', 'fresh control'), success=True)

    def test_new_runtime_failed_or_unknown_control_never_publishes_verified_checkpoint(self):
        for kind in ('project', 'infrastructure', 'budget', 'unknown'):
            with self.subTest(kind=kind):
                self.exercise(BaselineVerifyResult(False, kind, 'new environment did not pass'), success=False)

    def exercise(self, result, success):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            config={'projectName':'fixture','runtime':{'contractHash':'new-runtime'},'policyHash':'new-policy',
                    'verifyConfig':{'commands':['npm run new-check']},'budget':{'runBudgetMinutes':10}}
            handoff={'runtimeContractHash':'old-runtime','source':{'container':'old-container','key':'fixed-key','projectRelative':'.','fullAssignment':{}},
                     'verifiedC0':{'verificationStatus':'passed','commands':['npm run old-check']},'audit':{'status':'PASS','evidenceRef':'old-audit'}}
            source=SimpleNamespace(key='fixed-key', container=root/'source', root=root/'source'/'tree', git_head='head')
            def materialize(snapshot, destination, timeout):
                destination.mkdir(); (destination/'fixed.txt').write_text('repaired bytes')
            def control(project, assignment, **kwargs):
                self.assertEqual((project/'fixed.txt').read_text(), 'repaired bytes')
                self.assertEqual(list(kwargs['config'].commands), ['npm run new-check'])
                self.assertEqual(kwargs['runtime_env'], {'CURRENT_NODE':'new-runtime'})
                return result
            checkpoints=[]; runs=[]
            with patch.object(m,'open_source_snapshot',return_value=source), patch.object(m,'_materialize_trial_tree',side_effect=materialize), patch.object(m,'verify_assignment',side_effect=control) as verify, patch.object(m,'_runtime_env',return_value={'CURRENT_NODE':'new-runtime'}), patch.object(m,'save_checkpoint',side_effect=lambda r,c:checkpoints.append(c)), patch.object(m,'save_run',side_effect=lambda r,v:runs.append(v)), patch.object(m,'_write_json_atomic'), patch.object(m,'_emit_status'):
                if success:
                    self.assertEqual(m._begin_adopted_locked(root,config,Namespace(),'fixture',handoff),0)
                    self.assertEqual(checkpoints[0]['runtimeContractHash'],'new-runtime')
                    self.assertEqual(checkpoints[0]['verification']['commands'],['npm run new-check'])
                    self.assertEqual(checkpoints[0]['audit'],{'status':'UNKNOWN','evidenceRef':''})
                else:
                    with self.assertRaises(m.ProjectUnreadyError):
                        m._begin_adopted_locked(root,config,Namespace(),'fixture',handoff)
                    self.assertEqual(checkpoints,[]); self.assertEqual(runs,[])
                self.assertEqual(verify.call_count,1)
            self.assertFalse(list(root.glob('adoption-control-*')), 'private control tree must be cleaned')


if __name__ == '__main__':
    unittest.main()
