"""Real CLI recovery of a partially published negative iteration conclusion."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import iterative_migration as im
from iterative_state_transaction import StateTransaction, state_context, TRANSACTION
from source_snapshot import capture_durable_source_snapshot
from tests.regression.test_iterative_finish_baseline_and_feedback_validation import _config_dict, _run_dict, _checkpoint_dict, _candidate_dict

ROOT = Path(__file__).resolve().parents[2]


class IterationTransactionTests(unittest.TestCase):
    def fixture(self, root):
        project = root / 'project'; project.mkdir()
        (project / 'package.json').write_text('{"name":"transition-fixture","private":true}', encoding='utf-8')
        run_dir = root / 'run'; trial = run_dir / 'trial' / 'workspace'; trial.mkdir(parents=True)
        (trial / 'package.json').write_text('{"name":"transition-fixture","private":true}', encoding='utf-8')
        source = capture_durable_source_snapshot(project, run_dir / 'sources' / 'C0', timeout_seconds=30)
        config = _config_dict(projectDir=str(project), budget={'runBudgetMinutes': 0, 'maxInfraRetries': 2})
        run = _run_dict(phase='VERIFYING', activeCandidateId='candidate-1', attempts=17)
        candidate = _candidate_dict(run, candidateId='candidate-1', stage='VERIFYING', materializationRefs={'workspaceRoot':str(trial),'projectRelative':'.'})
        im.save_config(run_dir, config); im.save_run(run_dir, run); im.save_candidate(run_dir, candidate)
        im.save_checkpoint(run_dir, _checkpoint_dict(sourceSnapshotKey=source.key, sourceSnapshotContainer=str(source.container)))
        im.save_ledger(run_dir, {'schemaVersion':1, 'blocks':[], 'counters':{'spent':17}})
        return run_dir, run, config, candidate, trial

    def test_negative_history_scope_requires_current_source_config_tool_and_environment(self):
        from unittest.mock import patch
        from iterative_stage_checkpoint import negative_evidence_scope
        with tempfile.TemporaryDirectory() as tmp:
            root, run, config, candidate, _ = self.fixture(Path(tmp).resolve())
            with patch('iterative_stage_checkpoint.command_tool_closure', return_value='runtime-one'):
                original = negative_evidence_scope(root, config, 'C0')
                self.assertEqual(original, negative_evidence_scope(root, config, 'C0'))
                self.assertNotEqual(original, negative_evidence_scope(root, {**config, 'policyHash': 'changed'}, 'C0'))
                with patch.dict(os.environ, {'DEPLOOM_FIXTURE_INPUT': 'changed'}):
                    self.assertNotEqual(original, negative_evidence_scope(root, config, 'C0'))
                checkpoint = im.load_checkpoint(root, 'C0')
                im.save_checkpoint(root, {**checkpoint, 'sourceSnapshotKey': 'different-source'})
                self.assertNotEqual(original, negative_evidence_scope(root, config, 'C0'))
                im.save_checkpoint(root, checkpoint)
            with patch('iterative_stage_checkpoint.command_tool_closure', return_value='runtime-two'):
                self.assertNotEqual(original, negative_evidence_scope(root, config, 'C0'))
            with patch('iterative_stage_checkpoint.command_tool_closure', return_value=None):
                self.assertIsNone(negative_evidence_scope(root, config, 'C0'))

    def interrupt(self, root, run, config, candidate):
        calls = 0
        def interrupted(path, value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('injected metadata write failure')
            im._write_json_atomic(path, value)
        with self.assertRaisesRegex(OSError, 'injected metadata'):
            with StateTransaction(root, state_context(root, run, config, candidate), interrupted):
                im.save_candidate(root, {**candidate, 'stage':'REJECTED'})
                im.save_ledger(root, {'schemaVersion':1,'blocks':[{'candidateId':'candidate-1','assignmentFingerprint':'confirmed-conflict'}], 'counters':{'spent':17}})
                im.save_run(root, {**run, 'phase':'READY','activeCandidateId':None})
        self.assertTrue((root / TRANSACTION).exists())

    def cli(self, root):
        return subprocess.run([sys.executable,str(ROOT/'iterative_migration.py'),'--run-dir',str(root),'resume'], cwd=ROOT, env=dict(os.environ), capture_output=True,text=True,encoding='utf-8')

    def test_real_resume_finishes_metadata_once_and_preserves_incumbent_and_spent_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, run, config, candidate, _ = self.fixture(Path(tmp).resolve())
            cp = (root/'checkpoints'/'C0.json').read_bytes()
            self.interrupt(root, run, config, candidate)
            self.assertEqual(im.load_candidate(root)['stage'], 'REJECTED')
            self.assertEqual(im.load_run(root)['phase'], 'VERIFYING')
            resumed = self.cli(root)
            self.assertEqual(resumed.returncode,0,resumed.stdout+resumed.stderr)
            self.assertIn('iteration.transition-recovered',resumed.stdout)
            self.assertEqual(im.load_run(root)['activeCheckpointId'],'C0')
            self.assertEqual(im.load_run(root)['attempts'],17)
            self.assertEqual(im.load_ledger(root)['counters'],{'spent':17})
            self.assertEqual(len(im.load_ledger(root)['blocks']),1)
            self.assertEqual((root/'checkpoints'/'C0.json').read_bytes(),cp)
            self.assertFalse((root/TRANSACTION).exists())
            self.assertEqual(self.cli(root).returncode,0)
            self.assertEqual(len(im.load_ledger(root)['blocks']),1)

    def test_changed_trial_or_config_never_applies_old_failure_conclusion(self):
        for change in ('trial','config','journal'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root, run, config, candidate, trial = self.fixture(Path(tmp).resolve())
                self.interrupt(root,run,config,candidate)
                before = (root/'run.json').read_bytes()
                ledger = (root/'ledger.json').read_bytes()
                if change=='trial':
                    (trial/'new-source.txt').write_text('changed',encoding='utf-8')
                elif change=='config':
                    im.save_config(root,{**config,'policyHash':'changed'})
                else:
                    (root/TRANSACTION).write_text('{}',encoding='utf-8')
                result=self.cli(root)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual((root/'run.json').read_bytes(),before)
                self.assertEqual((root/'ledger.json').read_bytes(),ledger)
                self.assertTrue((root/TRANSACTION).exists())


if __name__ == '__main__':
    unittest.main()
