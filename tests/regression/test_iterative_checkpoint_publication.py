"""Physical crash/restart boundaries for verified incumbent publication."""
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import iterative_migration as im
from iterative_checkpoint_publication import TRANSACTION, prepare_publication, digest
from source_snapshot import capture_durable_source_snapshot
from tests.regression.test_iterative_finish_baseline_and_feedback_validation import _config_dict, _run_dict, _checkpoint_dict

ROOT = Path(__file__).resolve().parents[2]


class CheckpointPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'package.json').write_text('{"name":"publication-fixture","private":true}', encoding='utf-8')
        self.run_dir = self.root / 'run'
        self.snapshot = capture_durable_source_snapshot(self.project, self.run_dir / 'sources' / 'C1', timeout_seconds=30)
        manifest, lock, _ = im.manifest_and_lock_hashes(self.snapshot.project_path)
        self.config = _config_dict(projectDir=str(self.project), budget={'runBudgetMinutes': 0, 'maxInfraRetries': 2})
        self.run = _run_dict(phase='VERIFYING', activeCandidateId='candidate-1', attempts=7, budgetLedger={'repair': 2})
        self.next_run = {**self.run, 'activeCheckpointId': 'C1', 'activeCandidateId': None, 'generation': 2, 'phase': 'READY'}
        self.cp = _checkpoint_dict(checkpointId='C1', parentCheckpointId='C0', seq=1,
            sourceSnapshotKey=self.snapshot.key, sourceSnapshotContainer=str(self.snapshot.container),
            manifestHash=manifest, lockfileHash=lock)
        im.save_config(self.run_dir, self.config)
        im.save_run(self.run_dir, self.run)
        im.save_checkpoint(self.run_dir, _checkpoint_dict())
        im.save_candidate(self.run_dir, {'schemaVersion': 1, 'candidateId': 'candidate-1', 'stage': 'VERIFYING'})
        im.save_ledger(self.run_dir, {'schemaVersion': 1, 'blocks': [{'predicate': 'kept'}], 'counters': {'attempts': 7}})
        prepare_publication(self.run_dir, self.run, self.config, self.cp, self.next_run, 'candidate-1', im._write_json_atomic)

    def cli(self, command='resume'):
        return subprocess.run([sys.executable, str(ROOT / 'iterative_migration.py'), '--run-dir', str(self.run_dir), command],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8')

    def test_real_resume_recovers_all_atomic_write_boundaries_without_budget_reset(self):
        ledger = (self.run_dir / 'ledger.json').read_bytes()
        config = (self.run_dir / 'run-config.json').read_bytes()
        for boundary in ('journal-only', 'checkpoint-written', 'pointer-written'):
            with self.subTest(boundary=boundary):
                if boundary != 'journal-only':
                    im.save_checkpoint(self.run_dir, self.cp)
                if boundary == 'pointer-written':
                    im.save_run(self.run_dir, self.next_run)
                result = self.cli()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                state = im.load_run(self.run_dir)
                self.assertEqual(state['activeCheckpointId'], 'C1')
                self.assertEqual(state['attempts'], 7)
                self.assertEqual(state['budgetLedger'], {'repair': 2})
                self.assertIsNone(im.load_candidate(self.run_dir))
                self.assertFalse((self.run_dir / TRANSACTION).exists())
                self.assertEqual((self.run_dir / 'ledger.json').read_bytes(), ledger)
                self.assertEqual((self.run_dir / 'run-config.json').read_bytes(), config)
                self.assertEqual(self.cli().returncode, 0)
                # Recreate only the deliberately interrupted transaction fixture.
                im.save_run(self.run_dir, self.run)
                prepare_publication(self.run_dir, self.run, self.config, self.cp, self.next_run, 'candidate-1', im._write_json_atomic)

    def test_verify_exact_recovers_before_another_expensive_verification(self):
        result = self.cli('verify-exact')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('checkpoint.publication-recovered', result.stdout)
        self.assertNotIn('verify-exact.start', result.stdout)
        self.assertEqual(im.load_run(self.run_dir)['activeCheckpointId'], 'C1')

    def test_mutated_artifact_config_or_stale_generation_never_switches_pointer(self):
        for change in ('artifact', 'config', 'generation', 'checksum'):
            with self.subTest(change=change):
                run_before = (self.run_dir / 'run.json').read_bytes()
                if change == 'artifact':
                    target = self.snapshot.project_path / 'package.json'
                    original = target.read_bytes()
                    target.chmod(0o600)
                    target.write_text('{"mutated":true}', encoding='utf-8')
                elif change == 'config':
                    im.save_config(self.run_dir, {**self.config, 'policyHash': 'changed'})
                elif change == 'generation':
                    im.save_run(self.run_dir, {**self.run, 'generation': 9})
                    run_before = (self.run_dir / 'run.json').read_bytes()
                else:
                    record = json.loads((self.run_dir / TRANSACTION).read_text(encoding='utf-8'))
                    record['checkpoint']['fullAssignment'] = {'unexpected': '9.0.0'}
                    im._write_json_atomic(self.run_dir / TRANSACTION, record)
                result = self.cli()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.run_dir / 'run.json').read_bytes(), run_before)
                self.assertTrue((self.run_dir / TRANSACTION).exists())
                if change == 'artifact':
                    target.write_bytes(original)
                im.save_config(self.run_dir, self.config)
                im.save_run(self.run_dir, self.run)
                (self.run_dir / TRANSACTION).unlink()
                prepare_publication(self.run_dir, self.run, self.config, self.cp, self.next_run, 'candidate-1', im._write_json_atomic)

    def test_real_verified_acceptance_producer_recovers_after_pointer_write_failure(self):
        from baseline_constraint_verifier import BaselineVerifyConfig, verify_assignment
        (self.project / 'package-lock.json').write_text(json.dumps({'name': 'publication-fixture',
            'lockfileVersion': 3, 'requires': True, 'packages': {'': {'name': 'publication-fixture'}}}), encoding='utf-8')
        producer = self.root / 'producer-run'
        config = _config_dict(projectDir=str(self.project), targets={}, verifyConfig={'commands': ['node -e "process.exit(0)"']},
            budget={'runBudgetMinutes': 0, 'maxInfraRetries': 2})
        run = _run_dict(phase='VERIFYING', activeCandidateId='candidate-real')
        base = _checkpoint_dict(fullAssignment={})
        candidate = {'schemaVersion': 1, 'candidateId': 'candidate-real', 'runId': run['runId'],
            'baseCheckpointId': 'C0', 'stage': 'VERIFYING', 'fullAssignment': {},
            'materializationRefs': {'workspaceRoot': str(self.project), 'projectRelative': '.'}}
        im.save_config(producer, config)
        im.save_run(producer, run)
        im.save_checkpoint(producer, base)
        im.save_candidate(producer, candidate)
        result = verify_assignment(self.project, {}, config=BaselineVerifyConfig(
            commands=('node -e "process.exit(0)"',), timeout_seconds=30), run_project_checks=True)
        self.assertTrue(result.ok, result.summary)
        real_save = im.save_run
        def interrupted(path, value):
            if value.get('activeCheckpointId') == 'C1':
                raise OSError('injected pointer write failure')
            real_save(path, value)
        with patch.object(im, 'save_run', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'injected pointer'):
                im._accept_checkpoint(producer, run, config, candidate, base, result)
        self.assertEqual(im.load_run(producer)['activeCheckpointId'], 'C0')
        self.assertTrue((producer / TRANSACTION).exists())
        recovered = subprocess.run([sys.executable, str(ROOT / 'iterative_migration.py'), '--run-dir', str(producer), 'resume'],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(im.load_run(producer)['activeCheckpointId'], 'C1')
        self.assertEqual(im.load_checkpoint(producer, 'C1')['status'], 'VERIFIED')

    def test_completed_publication_survives_expired_work_budget(self):
        state = {**self.run, 'deadlineAt': '2000-01-01T00:00:00Z'}
        next_state = {**self.next_run, 'deadlineAt': state['deadlineAt']}
        im.save_run(self.run_dir, state)
        (self.run_dir / TRANSACTION).unlink()
        prepare_publication(self.run_dir, state, self.config, self.cp, next_state, 'candidate-1', im._write_json_atomic)
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(im.load_run(self.run_dir)['deadlineAt'], state['deadlineAt'])


if __name__ == '__main__':
    unittest.main()
