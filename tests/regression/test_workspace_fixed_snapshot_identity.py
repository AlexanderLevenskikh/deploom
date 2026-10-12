"""Fixed workspace inputs consume the selected sealed snapshot and frozen policy."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import baseline_constraint_verifier as verifier
import source_snapshot as source
import verification_proof as proof


class WorkspaceFixedSnapshotIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='deploom-fixed-workspace-')
        self.base = Path(self.tmp.name)
        self.root = self.base / 'repo'
        self.app = self.root / 'packages/app'
        self.lib = self.root / 'packages/lib'
        self.app.mkdir(parents=True)
        self.lib.mkdir(parents=True)
        self.write_json(self.root / 'package.json', {
            'name': 'workspace-root', 'private': True, 'packageManager': 'yarn@4.0.0',
            'workspaces': ['packages/*'],
        })
        self.write_json(self.app / 'package.json', {
            'name': 'workspace-app', 'version': '1.0.0',
            'dependencies': {'workspace-lib': 'workspace:*'},
        })
        self.write_json(self.lib / 'package.json', {'name': 'workspace-lib', 'version': '1.0.0', 'main': 'index.js'})
        (self.lib / 'index.js').write_text("module.exports = 'sealed';\n", encoding='utf-8')
        (self.app / 'check.cjs').write_text("if(require('workspace-lib') !== 'sealed') process.exit(41);", encoding='utf-8')
        (self.root / 'yarn.lock').write_text('__metadata:\n  version: 8\n', encoding='utf-8')
        (self.root / '.gitignore').write_text('node_modules/\n.yarn/\n.dependency-roadmap/\nignored/\n', encoding='utf-8')
        (self.root / 'backend').mkdir()
        (self.root / 'backend/outside.sql').write_text('excluded backend', encoding='utf-8')
        for args in [('init', '-q'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.invalid'), ('add', '.'), ('commit', '-qm', 'fixture')]:
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        (self.root / 'ignored').mkdir()
        (self.root / 'ignored/build.bin').write_bytes(b'ignored generated input')
        self.env = patch.dict(os.environ, {'DEPLOOM_SOURCE_SCOPE_MODE': 'project-gitignore'})
        self.env.start()

    def tearDown(self):
        source.clear_source_snapshot_epochs()
        self.env.stop()
        self.tmp.cleanup()

    def write_json(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_excluded_watcher_directory_does_not_change_workspace_fixed_key(self):
        with source.verification_source_snapshot_scope(self.app) as snapshot:
            expected = proof.fixed_resolver_input_fingerprint(snapshot.project_path, manager='yarn')
            # ReadDirectoryChangesW's delivery barrier creates this excluded
            # directory during pre-consumption validation. Re-discovering policy
            # from the sealed path used to add it to excludedPaths and drift.
            (snapshot.root / '.dependency-roadmap').mkdir(exist_ok=True)
            observed = proof.fixed_resolver_input_fingerprint(snapshot.project_path, manager='yarn')
            self.assertEqual(expected, observed)

    def test_sealed_alias_uses_original_policy_and_validated_snapshot_key(self):
        with source.verification_source_snapshot_scope(self.app) as snapshot:
            self.assertEqual(snapshot.key, source.source_snapshot_fingerprint(snapshot.project_path))
            self.assertIs(snapshot, source.active_source_snapshot(snapshot.project_path))
            (self.lib / 'index.js').write_text("module.exports = 'live edit';", encoding='utf-8')
            self.assertEqual(snapshot.key, source.source_snapshot_fingerprint(snapshot.project_path))

    def test_global_epoch_sealed_alias_is_the_same_subject(self):
        snapshot = source.activate_source_snapshot_epoch(self.app)
        self.assertIs(snapshot, source.active_source_snapshot(snapshot.project_path))
        self.assertEqual(snapshot.key, source.source_snapshot_fingerprint(snapshot.project_path))

    def test_sealed_alias_tampering_is_rejected(self):
        with source.verification_source_snapshot_scope(self.app) as snapshot:
            source.source_snapshot_fingerprint(snapshot.project_path)
            victim = snapshot.root / 'packages/lib/index.js'
            victim.chmod(victim.stat().st_mode | stat.S_IWRITE)
            victim.write_text("module.exports = 'tampered';", encoding='utf-8')
            with self.assertRaises(proof.SourceIdentityUnavailable):
                proof.fixed_resolver_input_fingerprint(snapshot.project_path, manager='yarn')

    def prepare_yarn_workspace(self):
        configured = os.environ.get('DEPLOOM_TEST_YARN_ROOT')
        if not configured:
            self.skipTest('pinned Yarn runtimes required')
        node = shutil.which('node')
        self.assertIsNotNone(node)
        runtime = Path(configured) / '4.0.0/node_modules/@yarnpkg/cli-dist/bin/yarn.js'
        self.assertTrue(runtime.is_file())
        local = self.root / '.yarn/releases/yarn.cjs'
        local.parent.mkdir(parents=True)
        shutil.copyfile(runtime, local)
        (self.root / '.yarnrc.yml').write_text('yarnPath: .yarn/releases/yarn.cjs\nnodeLinker: node-modules\nenableGlobalCache: false\n', encoding='utf-8')
        initial = subprocess.run([node, str(local), 'install'], cwd=self.root,
            env={**os.environ, 'YARN_ENABLE_IMMUTABLE_INSTALLS': 'false'}, capture_output=True, text=True, timeout=90)
        self.assertEqual(0, initial.returncode, initial.stdout + initial.stderr)
        bin_dir = Path(configured) / '4.0.0/node_modules/.bin'
        runtime_env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
        return runtime_env

    def test_real_yarn_workspace_resolver_lifecycle_checks_survive_live_edit(self):
        runtime_env = self.prepare_yarn_workspace()
        real_run = verifier._run
        for cached in (True, False):
            with self.subTest(cached=cached):
                (self.lib / 'index.js').write_text("module.exports = 'sealed';\n", encoding='utf-8')
                snapshots = []
                def observed_run(*args, **kwargs):
                    result = real_run(*args, **kwargs)
                    if kwargs.get('progress_label') == 'package-manager resolver install':
                        snapshot = source.active_source_snapshot(self.app)
                        self.assertIsNotNone(snapshot)
                        snapshots.append(snapshot)
                        (snapshot.root / '.dependency-roadmap').mkdir(exist_ok=True)
                        (self.lib / 'index.js').write_text("module.exports = 'live edit';", encoding='utf-8')
                    return result
                config = verifier.BaselineVerifyConfig(project_checks='strict', commands=('node check.cjs',),
                    proof_cache_dir=str(self.base / 'proofs') if cached else '', timeout_seconds=90, attempt_timeout_seconds=240)
                with patch.object(verifier, '_run', side_effect=observed_run):
                    result = verifier.verify_assignment(self.app, {}, config=config, run_project_checks=True, runtime_env=runtime_env)
                self.assertTrue(result.ok, f'{result.kind}: {result.summary}\n{result.output}')
                self.assertTrue(snapshots, 'real resolver install must run')
                self.assertEqual('live edit', (self.lib / 'index.js').read_text().split("'")[1])
                self.assertIsNone(source.active_source_snapshot(self.app))

    def test_binding_failure_restores_context_and_retires_owned_watcher(self):
        snapshot = source.capture_source_snapshot(self.app)
        self.addCleanup(source._force_rmtree, snapshot.container)
        with self.assertRaisesRegex(RuntimeError, 'fixture interruption'):
            with source.bind_source_snapshot(snapshot):
                self.assertEqual(snapshot.key, source.source_snapshot_fingerprint(snapshot.project_path))
                raise RuntimeError('fixture interruption')
        self.assertIsNone(source.active_source_snapshot(self.app))
        self.assertIsNone(source.active_source_snapshot(snapshot.project_path))
        self.assertFalse(any(entry[0] is snapshot for entry in source._ACTIVE_WATCHERS.values()))
        self.assertTrue(snapshot.root.exists(), 'binding must not delete caller-owned source bytes')

    def test_real_cli_check_only_passes_twice_and_reports_a_real_check_failure(self):
        runtime_env = self.prepare_yarn_workspace()
        config = self.base / 'verify.json'
        self.write_json(config, {'enabled': True, 'projectChecks': 'strict', 'commands': ['node check.cjs']})
        run = self.base / 'run'
        command = [sys.executable, str(Path(verifier.__file__).with_name('iterative_migration.py')),
            '--run-dir', str(run), 'begin', '--check-only', '--project-dir', str(self.app),
            '--project-name', 'WorkspaceFixture', '--verify-config', str(config)]
        for attempt in range(2):
            result = subprocess.run(command, env=runtime_env, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=120)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            verdict = json.loads((run / 'project-check.json').read_text(encoding='utf-8'))
            self.assertTrue(verdict['ok'], verdict)
            self.assertEqual('passed', verdict['control']['kind'])
            self.assertFalse((run / 'run.json').exists(), 'standalone check must not create a migration')
            self.assertIn('begin.check-done', result.stdout)
        (self.app / 'check.cjs').write_text('process.exit(23);', encoding='utf-8')
        result = subprocess.run(command, env=runtime_env, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120)
        self.assertNotEqual(0, result.returncode)
        verdict = json.loads((run / 'project-check.json').read_text(encoding='utf-8'))
        self.assertFalse(verdict['ok'])
        self.assertEqual('project', verdict['control']['kind'])
        self.assertNotIn('FIXED_SOURCE_IDENTITY_DRIFT', result.stdout)


if __name__ == '__main__':
    unittest.main()
