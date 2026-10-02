"""Physical project-scoped submodule readiness, with an absent sibling Gitlink."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import source_snapshot
from iterative_migration import project_readiness_preflight


def git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=60)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout


def package(root: Path, dependencies=None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {'name': 'submodule-scope-demo', 'version': '1.0.0', 'private': True,
                'dependencies': dependencies or {}}
    (root / 'package.json').write_text(json.dumps(manifest), encoding='utf-8')
    (root / 'package-lock.json').write_text(json.dumps({
        'name': manifest['name'], 'version': '1.0.0', 'lockfileVersion': 3,
        'requires': True, 'packages': {'': manifest},
    }), encoding='utf-8')


class ProjectSubmoduleScopeTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which('git'):
            self.skipTest('git unavailable')
        self.temp = tempfile.TemporaryDirectory(prefix='deploom-submodule-scope-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(source_snapshot.clear_source_snapshot_epochs)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.sub = self.base / 'sub'
        for root in (self.repo, self.sub):
            root.mkdir()
            git(root, 'init', '-b', 'master')
            git(root, 'config', 'user.email', 'scope@example.invalid')
            git(root, 'config', 'user.name', 'Scope Demo')
            (root / 'seed.txt').write_text('seed', encoding='utf-8')
            git(root, 'add', '.')
            git(root, 'commit', '-m', 'seed')
        git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add',
            str(self.sub), 'src/browser-tests')
        self.front = self.repo / 'front'
        package(self.front)
        (self.front / 'check.js').write_text('process.exit(0);', encoding='utf-8')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'frontend and sibling submodule')
        git(self.repo, 'submodule', 'deinit', '-f', '--', 'src/browser-tests')

    def test_independent_frontend_readiness_and_real_capture_agree(self):
        before = git(self.repo, 'status', '--porcelain')
        self.assertEqual([], source_snapshot.incomplete_submodules(self.front))
        project_readiness_preflight(self.front)
        snapshot = source_snapshot.activate_source_snapshot_epoch(self.front, replace=True)
        self.assertEqual(Path('front'), snapshot.project_relative)
        self.assertTrue((snapshot.root / '.gitmodules').is_file())
        self.assertEqual('-', git(snapshot.root, 'submodule', 'status')[0])
        self.assertEqual(before, git(self.repo, 'status', '--porcelain'))
        self.assertFalse((self.repo / 'src/browser-tests/seed.txt').exists())

    def test_root_project_still_requires_all_submodules(self):
        package(self.repo)
        self.assertEqual([('-', 'src/browser-tests')], source_snapshot.incomplete_submodules(self.repo))
        with self.assertRaisesRegex(source_snapshot.SourceCaptureError, 'SOURCE_SUBMODULE_INCOMPLETE'):
            source_snapshot.activate_source_snapshot_epoch(self.repo, replace=True)

    def test_submodule_inside_frontend_remains_required(self):
        git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add',
            str(self.sub), 'front/vendor/runtime')
        git(self.repo, 'commit', '-am', 'frontend submodule')
        git(self.repo, 'submodule', 'deinit', '-f', '--', 'front/vendor/runtime')
        self.assertEqual([('-', 'front/vendor/runtime')], source_snapshot.incomplete_submodules(self.front))
        with self.assertRaisesRegex(source_snapshot.SourceCaptureError, 'front/vendor/runtime'):
            source_snapshot.activate_source_snapshot_epoch(self.front, replace=True)

    def test_direct_local_dependency_requires_sibling(self):
        package(self.front, {'local-runtime': 'file:../src/browser-tests'})
        self.assertEqual([('-', 'src/browser-tests')], source_snapshot.incomplete_submodules(self.front))

    def test_transitive_local_dependency_requires_sibling(self):
        package(self.front, {'shared-runtime': 'file:../shared'})
        package(self.repo / 'shared', {'local-runtime': 'link:../src/browser-tests'})
        self.assertEqual([('-', 'src/browser-tests')], source_snapshot.incomplete_submodules(self.front))

    def test_workspace_owner_keeps_repository_scope(self):
        package(self.repo)
        path = self.repo / 'package.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['workspaces'] = ['front']
        path.write_text(json.dumps(data), encoding='utf-8')
        (self.front / 'package-lock.json').unlink()
        self.assertEqual([('-', 'src/browser-tests')], source_snapshot.incomplete_submodules(self.front))

    def test_conflicted_sibling_is_never_ignored(self):
        with mock.patch.object(source_snapshot, '_repository_incomplete_submodules',
                               return_value=[('U', 'src/browser-tests')]):
            self.assertEqual([('U', 'src/browser-tests')], source_snapshot.incomplete_submodules(self.front))

    def test_real_cli_check_passes_and_missing_input_command_does_not(self):
        if not shutil.which('node') or not (shutil.which('npm') or shutil.which('npm.cmd')):
            self.skipTest('Node/npm unavailable')
        config = self.base / 'verify.json'
        config.write_text(json.dumps({'enabled': True, 'projectChecks': 'strict',
                                      'commands': ['node check.js']}), encoding='utf-8')
        command = [sys.executable, str(Path(__file__).resolve().parents[2] / 'iterative_migration.py'),
                   '--run-dir', str(self.base / 'run'), 'begin', '--check-only',
                   '--project-dir', str(self.front), '--project-name', 'SubmoduleScopeDemo',
                   '--verify-config', str(config)]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=300)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        check = json.loads((self.base / 'run/project-check.json').read_text(encoding='utf-8'))
        self.assertTrue(check['ok'])
        self.assertEqual('passed', check['control']['kind'])
        # No inference of safety from path scope: an actual required read fails.
        (self.front / 'check.js').write_text(
            "require('fs').readFileSync('../src/browser-tests/seed.txt');", encoding='utf-8')
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=300)
        self.assertNotEqual(0, result.returncode)
        check = json.loads((self.base / 'run/project-check.json').read_text(encoding='utf-8'))
        self.assertFalse(check['ok'])
        self.assertEqual('project', check['control']['kind'])
        self.assertEqual('-', git(self.repo, 'submodule', 'status')[0])


if __name__ == '__main__':
    unittest.main()
