"""Standalone cached checks pin source once and retire request-owned copies."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

import baseline_constraint_verifier as verifier
import source_snapshot as source


class VerificationSourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'package.json').write_text(
            json.dumps({'name': 'source-scope-test', 'version': '1.0.0', 'private': True}),
            encoding='utf-8',
        )
        (self.project / 'package-lock.json').write_text(
            json.dumps({'name': 'source-scope-test', 'version': '1.0.0', 'lockfileVersion': 3,
                        'requires': True, 'packages': {'': {'name': 'source-scope-test', 'version': '1.0.0'}}}),
            encoding='utf-8',
        )
        (self.project / 'input.txt').write_text('sealed', encoding='utf-8')

    def tearDown(self):
        source.clear_source_snapshot_epochs()
        self.tmp.cleanup()

    def test_scope_reuses_nested_snapshot_and_recaptures_next_request(self):
        with source.verification_source_snapshot_scope(self.project) as first:
            with source.verification_source_snapshot_scope(self.project) as nested:
                self.assertIs(first, nested)
            self.assertTrue(first.root.is_dir())
            (self.project / 'input.txt').write_text('next request', encoding='utf-8')
            self.assertEqual('sealed', (first.project_path / 'input.txt').read_text())
        self.assertIsNone(source.active_source_snapshot(self.project))
        self.assertFalse(first.container.exists())
        with source.verification_source_snapshot_scope(self.project) as second:
            self.assertNotEqual(first.key, second.key)
        self.assertFalse(second.container.exists())

    def test_exception_retires_copy_and_leaves_caller_epoch_owned_by_caller(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with source.verification_source_snapshot_scope(self.project) as owned:
                source.validate_source_snapshot(owned)
                raise RuntimeError('fixture')
        self.assertFalse(owned.container.exists())
        self.assertIsNone(source.active_source_snapshot(self.project))
        epoch = source.activate_source_snapshot_epoch(self.project)
        with source.verification_source_snapshot_scope(self.project) as reused:
            self.assertIs(epoch, reused)
        self.assertTrue(epoch.root.exists())
        self.assertIs(source.active_source_snapshot(self.project), epoch)

    def test_concurrent_request_has_its_own_snapshot(self):
        with source.verification_source_snapshot_scope(self.project) as first:
            def other_request():
                self.assertIsNone(source.active_source_snapshot(self.project))
                with source.verification_source_snapshot_scope(self.project) as other:
                    source.validate_source_snapshot(other)
                    return other.container
            with ThreadPoolExecutor(max_workers=1) as executor:
                other_container = executor.submit(other_request).result()
            self.assertNotEqual(first.container, other_container)
            self.assertFalse(other_container.exists())
            self.assertIs(source.active_source_snapshot(self.project), first)
            source.validate_source_snapshot(first)

    @unittest.skipUnless(shutil.which('npm') and shutil.which('node'), 'npm and node required')
    def test_real_verifier_consumes_sealed_bytes_after_live_edit(self):
        (self.project / 'check.cjs').write_text(
            "if (require('fs').readFileSync('input.txt', 'utf8') !== 'sealed') process.exit(17);",
            encoding='utf-8',
        )
        config = verifier.BaselineVerifyConfig(
            proof_cache_dir=str(self.root / 'proofs'), project_checks='strict',
            commands=('node check.cjs',),
        )
        build = verifier.build_verification_proof_identity
        snapshots = []
        def edit_after_identity(*args, **kwargs):
            identity = build(*args, **kwargs)
            snapshot = source.active_source_snapshot(self.project)
            self.assertIsNotNone(snapshot)
            self.assertEqual(identity.source_snapshot_key, snapshot.key)
            snapshots.append(snapshot)
            (self.project / 'input.txt').write_text('live edit', encoding='utf-8')
            return identity
        with patch.object(verifier, 'build_verification_proof_identity', side_effect=edit_after_identity), \
                patch.object(source, 'source_snapshot_fingerprint', side_effect=AssertionError('live hashing')), \
                patch.object(source, 'capture_source_snapshot', wraps=source.capture_source_snapshot) as capture:
            result = verifier.verify_assignment(self.project, {}, config=config, run_project_checks=True)
        self.assertTrue(result.ok, f'{result.kind}: {result.summary}\n{result.output}')
        self.assertEqual(1, capture.call_count)
        self.assertEqual('live edit', (self.project / 'input.txt').read_text())
        # A completed verification retains a *candidate* for the next request.
        # The live edit must not be consumed as sealed proof: the next request
        # revalidates continuity, retires the stale copy and captures new bytes.
        self.assertTrue(snapshots[0].container.exists())
        self.assertIsNone(source.active_source_snapshot(self.project))
        with patch.object(source, 'capture_source_snapshot', wraps=source.capture_source_snapshot) as recapture:
            with source.verification_source_snapshot_scope(self.project, reuse_completed=True) as refreshed:
                self.assertNotEqual(snapshots[0].key, refreshed.key)
                self.assertEqual('live edit', (refreshed.project_path / 'input.txt').read_text())
            self.assertEqual(1, recapture.call_count)
        self.assertFalse(snapshots[0].container.exists())


if __name__ == '__main__':
    unittest.main()
