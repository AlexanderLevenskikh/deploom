"""Real Git/filesystem/process regressions for migration authority boundaries."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import iterative_migration as migration
from delivery_source_identity import assert_committed_files, file_identities
from iterative_task import build_progress_summary
from manual_dependency_audit import dependency_input_identity
from run_writer_lock import acquire, release
from source_snapshot import capture_durable_source_snapshot
from tests.regression.test_iterative_engine_precheck import _config_dict, _run_dict, _checkpoint_dict

ROOT = Path(__file__).resolve().parents[2]


class AuthorityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='deploom-authority-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.write('package.json', {'name': 'fixture', 'version': '1.0.0', 'private': True})
        self.write('package-lock.json', {'name': 'fixture', 'lockfileVersion': 3,
                                        'packages': {'': {'name': 'fixture', 'version': '1.0.0'}}})
        (self.project / 'input.txt').write_text('good', encoding='utf-8')
        (self.project / 'check.cjs').write_text("if(require('fs').readFileSync('input.txt','utf8')!=='good')process.exit(1)", encoding='utf-8')
        self.git('init', '-q')
        # Detached Git housekeeping can recreate .git entries during temp cleanup.
        # This fixture tests committed bytes, so keep its repository quiescent.
        self.git('config', 'maintenance.auto', 'false')
        self.git('config', 'gc.auto', '0')
        self.git('config', 'core.autocrlf', 'false')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')

    def write(self, name, value):
        (self.project / name).write_text(json.dumps(value), encoding='utf-8')

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.project), *args], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def test_hidden_index_flags_cannot_certify_different_commit_bytes(self):
        expected, _ = file_identities(self.project, ['input.txt'])
        assert_committed_files(self.project, expected)
        for flag in ('--assume-unchanged', '--skip-worktree'):
            with self.subTest(flag=flag):
                self.git('update-index', flag, 'input.txt')
                (self.project / 'input.txt').write_text('unverified', encoding='utf-8')
                self.assertEqual('', self.git('status', '--porcelain'))
                changed, _ = file_identities(self.project, ['input.txt'])
                with self.assertRaisesRegex(RuntimeError, 'DELIVERY_COMMITTED_TREE_MISMATCH'):
                    assert_committed_files(self.project, changed)
                self.git('update-index', '--no-assume-unchanged', '--no-skip-worktree', 'input.txt')
                (self.project / 'input.txt').write_text('good', encoding='utf-8')

    def test_commit_pin_and_eol_tolerance_do_not_accept_other_content(self):
        (self.project / 'input.txt').write_bytes(b'good\n')
        self.git('add', 'input.txt')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'newline')
        commit = self.git('rev-parse', 'HEAD')
        (self.project / 'input.txt').write_bytes(b'good\r\n')
        expected, _ = file_identities(self.project, ['input.txt'])
        assert_committed_files(self.project, expected, commit=commit)
        (self.project / 'input.txt').write_bytes(b'changed\r\n')
        expected, _ = file_identities(self.project, ['input.txt'])
        with self.assertRaisesRegex(RuntimeError, 'DELIVERY_COMMITTED_TREE_MISMATCH'):
            assert_committed_files(self.project, expected, commit=commit)

    def test_git_replace_refs_cannot_redefine_pinned_raw_commit_bytes(self):
        original = self.git('rev-parse', 'HEAD:input.txt')
        changed = subprocess.run(['git', '-C', str(self.project), 'hash-object', '-w', '--stdin'],
            input=b'changed', capture_output=True, timeout=15, check=True).stdout.decode().strip()
        self.git('replace', original, changed)
        self.assertEqual('changed', self.git('show', 'HEAD:input.txt'))
        (self.project / 'input.txt').write_text('changed', encoding='utf-8')
        expected, _ = file_identities(self.project, ['input.txt'])
        with self.assertRaisesRegex(RuntimeError, 'DELIVERY_COMMITTED_TREE_MISMATCH'):
            assert_committed_files(self.project, expected)

    def test_committed_blob_checks_bound_actual_git_batch_count_and_bytes(self):
        import delivery_source_identity as identities
        names = [f'batch-{index}.txt' for index in range(300)]
        for name in names:
            (self.project / name).write_text(name, encoding='utf-8')
        for index in range(3):
            name = f'large-{index}.bin'
            names.append(name)
            (self.project / name).write_bytes(bytes([index]) * (3 * 1024 * 1024))
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'batch fixture')
        expected, _ = file_identities(self.project, names)
        run = subprocess.run
        batches = []
        def observe(argv, **kwargs):
            result = run(argv, **kwargs)
            if 'cat-file' in argv:
                batches.append((len(kwargs['input'].splitlines()), len(result.stdout)))
            return result
        with mock.patch.object(identities.subprocess, 'run', side_effect=observe):
            assert_committed_files(self.project, expected)
        self.assertGreaterEqual(len(batches), 3)
        self.assertTrue(all(count <= 256 and size <= 8*1024*1024+65536 for count, size in batches), batches)

    def test_lfs_pointer_requires_exact_physical_digest_size_and_attribute(self):
        import hashlib
        payload = b'physical binary\x00payload'
        digest = hashlib.sha256(payload).hexdigest()
        (self.project / '.gitattributes').write_text('asset.bin filter=lfs -text\n', encoding='utf-8')
        # No filter executable: commit the literal pointer in this fixture.
        self.git('config', 'filter.lfs.required', 'false')
        self.git('config', 'filter.lfs.clean', '')
        self.git('config', 'filter.lfs.smudge', '')
        self.git('config', 'filter.lfs.process', '')
        def commit_pointer(oid=digest, size=len(payload), attributes=True):
            (self.project / '.gitattributes').write_text('asset.bin filter=lfs -text\n' if attributes else 'asset.bin -text\n', encoding='utf-8')
            (self.project / 'asset.bin').write_text(f'version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {size}\n', encoding='utf-8', newline='\n')
            self.git('add', '.')
            self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'pointer')
            (self.project / 'asset.bin').write_bytes(payload)
        commit_pointer()
        expected, _ = file_identities(self.project, ['asset.bin'])
        assert_committed_files(self.project, expected)
        for kwargs in ({'oid': '0'*64}, {'size': len(payload)+1}, {'attributes': False}):
            with self.subTest(kwargs=kwargs):
                commit_pointer(**kwargs)
                with self.assertRaisesRegex(RuntimeError, 'DELIVERY_COMMITTED_TREE_MISMATCH'):
                    assert_committed_files(self.project, expected)

    def test_root_registry_change_invalidates_nested_audit_identity(self):
        nested = self.project / 'packages' / 'app'
        nested.mkdir(parents=True)
        (nested / 'package.json').write_text('{"name":"app"}', encoding='utf-8')
        before, _ = dependency_input_identity(nested)
        (self.project / '.npmrc').write_text('registry=https://registry.example.invalid/\n', encoding='utf-8')
        after, files = dependency_input_identity(nested)
        self.assertNotEqual(before, after)
        self.assertIn('../../.npmrc', files)
        script = 'import {dependencyInputIdentity as read} from ' + json.dumps((ROOT / 'desktop/dist-electron/acceptance-policy.js').as_uri()) + ';console.log(JSON.stringify(read(process.argv[1])))'
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(nested)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({'hash': after, 'files': files}, json.loads(result.stdout))

    def test_successful_real_verification_cannot_accept_post_result_mutation(self):
        run_dir = self.root / 'run'
        config = _config_dict(projectDir=str(self.project), targets={}, verifyConfig={'commands': ['node check.cjs'], 'timeoutSeconds': 30})
        run = _run_dict(phase='VERIFYING', activeCandidateId='candidate')
        source = capture_durable_source_snapshot(self.project, run_dir / 'sources' / 'C0', timeout_seconds=30)
        base = _checkpoint_dict(fullAssignment={}, sourceSnapshotKey=source.key, sourceSnapshotContainer=str(source.container))
        candidate = {'schemaVersion': 1, 'candidateId': 'candidate', 'runId': run['runId'], 'baseCheckpointId': 'C0',
                     'stage': 'VERIFYING', 'fullAssignment': {}, 'materializationRefs': {'workspaceRoot': str(self.project), 'projectRelative': '.'}}
        migration.save_config(run_dir, config)
        migration.save_run(run_dir, run)
        migration.save_checkpoint(run_dir, base)
        migration.save_candidate(run_dir, candidate)
        events = []
        def interleave(event):
            if event.get('event') == 'verify-exact.result':
                events.append(event)
                if event.get('ok'):
                    (self.project / 'input.txt').write_text('bad', encoding='utf-8')
        with mock.patch.dict(os.environ, {'npm_config_offline': 'true', 'npm_config_cache': str(self.root / 'npm-cache')}), mock.patch.object(migration, '_emit_status', interleave):
            with self.assertRaisesRegex(migration.InvalidInputError, 'VERIFIED_SOURCE_IDENTITY_MISMATCH'):
                migration.cmd_verify_exact(argparse.Namespace(run_dir=str(run_dir), owner='fixture'))
        self.assertTrue(events[0]['ok'], events)
        self.assertEqual('C0', migration.load_run(run_dir)['activeCheckpointId'])
        self.assertFalse((run_dir / 'checkpoints' / 'C1.json').exists())
        self.assertEqual('good', (source.root / 'input.txt').read_text(encoding='utf-8'))

    def test_mutating_wrappers_read_state_after_writer_admission(self):
        entrypoints = [('cmd_bootstrap_materialize', '_bootstrap_materialize_locked'),
            ('cmd_plan_next', '_plan_next_locked'), ('cmd_materialize', '_materialize_locked'),
            ('cmd_precheck', '_precheck_locked'), ('cmd_verify_exact', '_verify_exact_locked'),
            ('cmd_audit', '_audit_locked')]
        acquire_writer = migration._RunLock.acquire
        for command, action in entrypoints:
            with self.subTest(command=command):
                run_dir = self.root / command
                run = _run_dict(generation=5)
                config = _config_dict(projectDir=str(self.project), targets={'pkg-a': '1.0.0'})
                migration.save_run(run_dir, run)
                migration.save_config(run_dir, config)
                observed = []
                def admit(lock):
                    # A previous admitted writer commits newer state before
                    # this operation obtains the real OS writer lock.
                    migration.save_run(run_dir, {**run, 'generation': 6})
                    migration.save_config(run_dir, {**config, 'targets': {'pkg-a': '2.0.0'}})
                    acquire_writer(lock)
                def work(directory, current, settings, args):
                    observed.append((current['generation'], settings['targets']['pkg-a']))
                    return 0
                with mock.patch.object(migration._RunLock, 'acquire', admit), mock.patch.object(migration, action, side_effect=work):
                    self.assertEqual(0, getattr(migration, command)(argparse.Namespace(run_dir=str(run_dir), owner='fixture')))
                self.assertEqual([(6, '2.0.0')], observed)

    def test_real_timeout_precheck_preserves_candidate_without_opening_agent_gate(self):
        from baseline_constraint_verifier import _run
        with self.assertRaises(subprocess.TimeoutExpired) as failure:
            _run(['node', '-e', 'setTimeout(()=>{},10000)'], self.project, timeout_seconds=1)
        run_dir = self.root / 'precheck-run'
        run = _run_dict(phase='MATERIALIZING', activeCandidateId='candidate')
        config = _config_dict(projectDir=str(self.project), verifyConfig={'commands': ['node check.cjs']})
        candidate = {'schemaVersion': 1, 'candidateId': 'candidate', 'runId': run['runId'], 'baseCheckpointId': 'C0',
                     'stage': 'MATERIALIZED', 'fullAssignment': {}, 'materializationRefs': {'workspaceRoot': str(self.project), 'projectRelative': '.'}}
        migration.save_run(run_dir, run)
        migration.save_config(run_dir, config)
        migration.save_candidate(run_dir, candidate)
        migration.save_checkpoint(run_dir, _checkpoint_dict())
        with mock.patch.object(migration, '_run', side_effect=failure.exception), mock.patch.object(migration, '_emit_status'):
            migration._precheck_locked(run_dir, run, config, argparse.Namespace(timeout_seconds=120))
        self.assertEqual('PRECHECKED', migration.load_candidate(run_dir)['stage'])
        self.assertEqual('VERIFYING', migration.load_run(run_dir)['phase'])
        self.assertEqual([], migration.read_repair_requests(run_dir))
        self.assertEqual(0, migration.load_run(run_dir)['budgetLedger']['repairAttempts'])

    def test_c0_swap_rejects_unverified_bytes_and_keeps_original_container(self):
        run_dir = self.root / 'bootstrap'
        original = capture_durable_source_snapshot(self.project, run_dir / 'sources' / 'C0', timeout_seconds=30)
        (self.project / 'input.txt').write_text('bad', encoding='utf-8')
        with self.assertRaisesRegex(migration.InvalidInputError, 'VERIFIED_SOURCE_IDENTITY_MISMATCH'):
            migration._replace_c0_source_snapshot(run_dir, self.project, timeout_seconds=30, expected_key=original.key)
        self.assertEqual('good', (original.root / 'input.txt').read_text(encoding='utf-8'))


class WriterLockBoundaryTests(unittest.TestCase):
    def test_release_cannot_unlink_foreign_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'run' / 'run.lock'
            lease = acquire(path, 'fixture', lambda: True)
            path.write_text('{"token":"other-owner"}', encoding='utf-8')
            release(path, lease)
            self.assertEqual('other-owner', json.loads(path.read_text())['token'])

    def test_three_real_reclaimers_never_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            lock_path = directory / 'run' / 'run.lock'
            lock_path.parent.mkdir()
            lock_path.write_text('{"pid":0,"owner":"dead"}', encoding='utf-8')
            marker = directory / 'critical-section'
            script = "from run_writer_lock import acquire,release;import sys,time;from pathlib import Path;p=Path(sys.argv[1]);m=Path(sys.argv[2]);lease=acquire(p,'reclaimer',lambda:True,timeout=5);f=m.open('x');f.close();time.sleep(.08);m.unlink();release(p,lease)"
            children = [subprocess.Popen([sys.executable, '-c', script, str(lock_path), str(marker)], cwd=ROOT,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
            try:
                for child in children:
                    _, error = child.communicate(timeout=10)
                    self.assertEqual(0, child.returncode, error)
                self.assertFalse(marker.exists())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill(); child.communicate(timeout=10)

    def test_os_lock_blocks_second_process_and_recovers_after_owner_death(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'run' / 'run.lock'
            script = "from run_writer_lock import acquire,release;import sys;from pathlib import Path;p=Path(sys.argv[1]);lease=acquire(p,'child',lambda:True);print('locked',flush=True);sys.stdin.readline();release(p,lease)"
            child = subprocess.Popen([sys.executable, '-u', '-c', script, str(path)], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                import queue
                import threading
                ready = queue.Queue()
                threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
                self.assertEqual('locked\n', ready.get(timeout=10))
                with self.assertRaisesRegex(TimeoutError, 'RUN_LOCK_BUSY'):
                    acquire(path, 'parent', lambda: True, timeout=.15)
                child.terminate()
                child.wait(timeout=10)
                lease = acquire(path, 'recovered', lambda: True, timeout=2)
                release(path, lease)
                self.assertFalse(path.exists())
            finally:
                if child.poll() is None:
                    child.kill(); child.wait(timeout=10)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()


class ProgressCountBoundaryTests(unittest.TestCase):
    def test_two_actions_one_package_and_one_current_goal_remain_distinct(self):
        checkpoints = [{'checkpointId': 'C0', 'fullAssignment': {'a': '1'}},
                       {'checkpointId': 'C1', 'parentCheckpointId': 'C0', 'fullAssignment': {'a': '2'}, 'acceptedDelta': {'changed': {'a': '2'}}},
                       {'checkpointId': 'C2', 'parentCheckpointId': 'C1', 'fullAssignment': {'a': '3'}, 'acceptedDelta': {'changed': {'a': '3'}}}]
        value = build_progress_summary({'targets': {'a': '3'}}, checkpoints, checkpoints[-1], {})
        self.assertEqual((2, 1, 1, 1, 0), tuple(value[key] for key in ('acceptedActions', 'accepted', 'goalsSatisfied', 'denominator', 'remaining')))
        value = build_progress_summary({'targets': {'a': '4'}}, checkpoints, checkpoints[-1], {})
        self.assertEqual((1, 0, 1), tuple(value[key] for key in ('accepted', 'goalsSatisfied', 'remaining')))


if __name__ == '__main__':
    unittest.main()
