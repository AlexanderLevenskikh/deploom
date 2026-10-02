"""Fresh-run archival and fixed inputs at real filesystem/CLI boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import iterative_migration as migration
import iterative_restart as restart
from baseline_constraint_verifier import _apply_assignment, AssignmentMaterializationError

class RestartAndFixedInputsTests(unittest.TestCase):
    def make_run(self, root):
        root.mkdir()
        for name, data in {'run.json': {'schemaVersion': 1, 'runId': 'old', 'phase': 'READY'},
                           'attempt.json': {'status': 'running'}, 'project-check.json': {'ok': True}}.items():
            (root / name).write_text(json.dumps(data), encoding='utf-8')
        (root / 'sources' / 'C0').mkdir(parents=True)
        (root / 'sources' / 'C0' / 'source.js').write_text('verified bytes')
        (root / 'checkpoints').mkdir()
        (root / 'checkpoints' / 'C0.json').write_text('{"status":"PROJECT_VERIFIED"}')
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def test_actual_cli_archives_verified_bytes_and_journal_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            before = self.make_run(root)
            project = Path(tmp) / 'project'
            project.mkdir()
            (project / 'source.js').write_text('user dirty bytes')
            command = [sys.executable, str(Path(migration.__file__)), '--run-dir', str(root), 'archive-run']
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            archive = Path(json.loads(result.stdout.split('ITERATIVE_ARCHIVE_RESULT_V1 ')[1])['archiveDir'])
            self.assertFalse((root / 'run.json').exists())
            self.assertEqual(before, {str(p.relative_to(archive)): p.read_bytes() for p in archive.rglob('*') if p.is_file()})
            self.assertEqual('user dirty bytes', (project / 'source.js').read_text())
            second = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            self.assertTrue((archive / 'run.json').exists())

    def test_failed_move_restores_every_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            before = self.make_run(root)
            real_rename = os.rename
            calls = 0
            def fail_once(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('injected busy file')
                return real_rename(source, target)
            with patch.object(restart.os, 'rename', side_effect=fail_once):
                with self.assertRaisesRegex(OSError, 'busy file'):
                    restart.archive_for_restart(root, migration.LOCK_FILENAME, migration._write_json_atomic)
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})
            self.assertFalse((root / restart.TRANSACTION).exists())

    def test_process_interruption_recovers_before_status_without_archiving_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            before = self.make_run(root)
            archive = root / restart.HISTORY / 'interrupted'
            archive.mkdir(parents=True)
            entries = ['attempt.json', 'run.json']
            migration._write_json_atomic(root / restart.TRANSACTION, {'phase': 'moving', 'archiveName': 'interrupted', 'entries': entries})
            os.rename(root / 'attempt.json', archive / 'attempt.json')
            result = subprocess.run([sys.executable, str(Path(migration.__file__)), '--run-dir', str(root), 'archive-run', '--recover-only'], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})

    def test_committed_archive_recovery_never_rolls_back_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            self.make_run(root)
            archived = restart.archive_for_restart(root, migration.LOCK_FILENAME, migration._write_json_atomic)
            archive = Path(archived['archiveDir'])
            new_bytes = b'{"schemaVersion":1,"runId":"new","phase":"READY"}'
            (root / 'run.json').write_bytes(new_bytes)
            migration._write_json_atomic(root / restart.TRANSACTION, {
                'phase': 'committed', 'archiveName': archive.name, 'entries': ['run.json']})
            restart.recover_restart_archive(root, migration._write_json_atomic)
            self.assertEqual(new_bytes, (root / 'run.json').read_bytes())
            self.assertEqual('old', json.loads((archive / 'run.json').read_text())['runId'])
            self.assertFalse((root / restart.TRANSACTION).exists())

    def test_agent_lease_prevents_abandoning_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            self.make_run(root)
            (root / 'trial').mkdir()
            (root / 'trial' / 'agent-lease.json').write_text('{}')
            with self.assertRaisesRegex(restart.RestartArchiveError, 'RESTART_AGENT_PENDING'):
                restart.archive_for_restart(root, migration.LOCK_FILENAME, migration._write_json_atomic)
            self.assertTrue((root / 'run.json').exists())

    def test_invalid_recovery_path_cannot_escape_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            self.make_run(root)
            migration._write_json_atomic(root / restart.TRANSACTION, {'archiveName': '../outside', 'entries': ['run.json']})
            with self.assertRaisesRegex(restart.RestartArchiveError, 'RESTART_JOURNAL_INVALID'):
                restart.recover_restart_archive(root, migration._write_json_atomic)
            self.assertTrue((root / 'run.json').exists())

    def test_old_lock_with_living_owner_is_never_reclaimed_by_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migration._write_json_atomic(root / migration.LOCK_FILENAME, {
                'owner': 'slow-verifier', 'pid': os.getpid(), 'startedAt': '2000-01-01T00:00:00Z'})
            self.assertFalse(migration._RunLock(root, 'restart')._stale())
            with patch.object(migration, '_process_owner_dead', return_value=True):
                self.assertTrue(migration._RunLock(root, 'restart')._stale())

    def test_fixed_inputs_stay_in_manifest_but_not_in_control_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixed = {'git-lib': 'git+ssh://git@example.invalid:team/library.git#semver:2.146.0',
                     'alias': 'npm:compat@^4.0.0', 'local': 'file:vendor/local', 'workspace': 'workspace:*'}
            manifest = {'dependencies': {**fixed, 'registry': '^1.0.0'}}
            (root / 'package.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.assertEqual({'registry': '^1.0.0'}, migration.direct_dependency_assignment(root))
            _apply_assignment(root, migration.direct_dependency_assignment(root))
            after = json.loads((root / 'package.json').read_text())['dependencies']
            self.assertEqual(fixed, {name: after[name] for name in fixed})
            with self.assertRaisesRegex(AssignmentMaterializationError, 'ASSIGNMENT_TARGETS_FIXED_INPUT'):
                _apply_assignment(root, {'git-lib': '3.0.0'})

    def test_mixed_source_conflict_is_not_silently_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'package.json').write_text(json.dumps({'dependencies': {'mixed': 'git+https://example.invalid/lib.git'}, 'devDependencies': {'mixed': '^1.0.0'}}))
            assignment = migration.direct_dependency_assignment(root)
            self.assertIn('mixed', assignment)
            with self.assertRaisesRegex(AssignmentMaterializationError, 'ASSIGNMENT_HETEROGENEOUS_SOURCE_CONFLICT'):
                _apply_assignment(root, assignment)

if __name__ == '__main__':
    unittest.main()
