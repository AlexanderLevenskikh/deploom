"""Live source uncertainty retries without weakening source coverage or proof."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import source_snapshot as source
import baseline_constraint_verifier as verifier
from verification_proof import SourceIdentityUnavailable
from iterative_migration import _command_argv

class LiveSourceIdentityTests(unittest.TestCase):
    def test_one_real_directory_change_retries_and_hashes_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'input.js').write_text('source', encoding='utf-8')
            real_hash = source._hash_regular_file
            changed = False
            def mutate_once(path, **kwargs):
                nonlocal changed
                result = real_hash(path, **kwargs)
                if not changed:
                    changed = True
                    (root / 'generated.js').write_text('generated input', encoding='utf-8')
                return result
            with patch.object(source, '_subject_layout', return_value=(root, Path('.'), '')), patch.object(source, '_hash_regular_file', side_effect=mutate_once), patch.object(source, 'build_source_tree_manifest', wraps=source.build_source_tree_manifest) as manifest:
                key = source.source_snapshot_fingerprint(root)
                self.assertEqual(manifest.call_count, 2)
            with patch.object(source, '_subject_layout', return_value=(root, Path('.'), '')):
                self.assertEqual(key, source.source_snapshot_fingerprint(root))
                (root / 'generated.js').write_text('different generated input', encoding='utf-8')
                self.assertNotEqual(key, source.source_snapshot_fingerprint(root))

    def test_persistent_change_is_bounded_and_returns_no_identity(self):
        with patch.object(source, '_subject_layout', return_value=(Path('.'), Path('.'), '')), patch.object(source, 'build_source_tree_manifest', side_effect=source.SourceCaptureError('SOURCE_CAPTURE_UNSTABLE: busy build')) as manifest:
            with self.assertRaisesRegex(source.SourceCaptureError, 'SOURCE_CAPTURE_UNSTABLE'):
                source.source_snapshot_fingerprint(Path('.'))
            self.assertEqual(manifest.call_count, source.SOURCE_CAPTURE_RETRIES)

    def test_non_transient_error_is_not_retried(self):
        with patch.object(source, '_subject_layout', return_value=(Path('.'), Path('.'), '')), patch.object(source, 'build_source_tree_manifest', side_effect=source.SourceCaptureError('SOURCE_SYMLINK_ESCAPE')) as manifest:
            with self.assertRaisesRegex(source.SourceCaptureError, 'SOURCE_SYMLINK_ESCAPE'):
                source.source_snapshot_fingerprint(Path('.'))
            self.assertEqual(manifest.call_count, 1)

class VerificationBoundaryTests(unittest.TestCase):
    def test_source_uncertainty_returns_infrastructure_without_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = verifier.BaselineVerifyConfig(proof_cache_dir=str(Path(tmp) / 'proofs'))
            topology = SimpleNamespace(profile=SimpleNamespace(manager='npm'))
            with patch.object(verifier, '_validate_assignment_materialization'), patch.object(verifier, 'resolve_project_topology', return_value=topology), patch.object(verifier, 'resolve_executable', return_value='npm'), patch.object(verifier, 'build_verification_proof_identity', side_effect=SourceIdentityUnavailable('SOURCE_CAPTURE_UNSTABLE: busy build')), patch.object(verifier, '_verify_assignment_uncached') as execute:
                result = verifier.verify_assignment(Path(tmp), {}, config=config, run_project_checks=True)
            self.assertFalse(result.ok)
            self.assertEqual('infrastructure', result.kind)
            self.assertIn('SOURCE_CAPTURE_UNSTABLE', result.summary)
            execute.assert_not_called()

    @unittest.skipUnless(shutil.which('npm') and shutil.which('node'), 'npm and node required')
    def test_npm_script_and_quoted_compound_command_execute_on_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'package.json').write_text(json.dumps({'name': 'shell-regression', 'scripts': {'test': 'node "check with spaces.cjs"'}}), encoding='utf-8')
            (root / 'check with spaces.cjs').write_text("require('fs').writeFileSync('check-ran.txt', process.env.DEPLOOM_SHELL_TEST);", encoding='utf-8')
            env = {**os.environ, 'DEPLOOM_SHELL_TEST': 'real-command'}
            result = subprocess.run(_command_argv('npm run test && node -e "process.exit(0)"', env), cwd=root, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual('real-command', (root / 'check-ran.txt').read_text())

if __name__ == '__main__':
    unittest.main()
