import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from baseline_constraint_verifier import _clamped_phase_seconds
from iterative_migration import _deadline_iso, run_budget_ok, verify_config_from
from source_snapshot import build_source_tree_manifest, capture_source_snapshot, materialize_source_for_verification
from verification_process_supervisor import run_supervised


class UnboundedPreparationTests(unittest.TestCase):
    def test_no_run_deadline_and_explicit_deadline_remains_effective(self):
        self.assertEqual('', _deadline_iso(0))
        self.assertTrue(run_budget_ok({}, {'deadlineAt': _deadline_iso(0)}))
        self.assertFalse(run_budget_ok({}, {'deadlineAt': '2000-01-01T00:00:00Z'}))
        self.assertTrue(run_budget_ok({}, {'deadlineAt': _deadline_iso(10)}))

    def test_iterative_configuration_removes_aggregate_but_keeps_command_timeout(self):
        config = verify_config_from({'timeoutSeconds': 90}, Path('run'))
        self.assertTrue(config.unbounded_preparation)
        self.assertEqual(90, config.timeout_seconds)
        self.assertEqual(90, _clamped_phase_seconds(attempt_remaining_seconds=float('inf'),
            budget_remaining_seconds=None, timeout_seconds=90, attempt_timeout_seconds=3600, progress_label='test'))
        self.assertEqual(5, _clamped_phase_seconds(attempt_remaining_seconds=float('inf'),
            budget_remaining_seconds=5, timeout_seconds=90, attempt_timeout_seconds=3600, progress_label='test'))

    def test_unbounded_hash_survives_elapsed_time_that_exhausts_bounded_hash(self):
        from source_snapshot import SourceCaptureError
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'package.json').write_text('{"name":"demo"}', encoding='utf-8')
            # Advance only the timeout clock: no need to wait 30 minutes.
            with mock.patch('source_snapshot.time.monotonic', side_effect=range(0, 1000000, 2000)):
                manifest = build_source_tree_manifest(root, timeout_seconds=0)
                self.assertGreater(manifest.file_count, 0)
            with mock.patch('source_snapshot.time.monotonic', side_effect=range(0, 1000000, 2000)):
                with self.assertRaises(SourceCaptureError):
                    build_source_tree_manifest(root, timeout_seconds=1800)

    def test_real_snapshot_copy_and_post_copy_validation_accept_zero(self):
        with tempfile.TemporaryDirectory() as raw:
            project = Path(raw) / 'project'
            project.mkdir()
            (project / 'package.json').write_text('{"name":"unbounded-demo","version":"1.0.0"}', encoding='utf-8')
            for args in (['init'], ['add', 'package.json'], ['-c', 'user.name=Demo', '-c', 'user.email=demo@example.invalid', 'commit', '-m', 'demo']):
                subprocess.run(['git', *args], cwd=project, check=True, capture_output=True)
            materialized, snapshot, _method = materialize_source_for_verification(
                project, Path(raw) / 'copy', timeout_seconds=0)
            try:
                self.assertEqual((project / 'package.json').read_bytes(), (materialized / 'package.json').read_bytes())
                self.assertEqual(snapshot.manifest_key, build_source_tree_manifest(snapshot.root, timeout_seconds=0).key)
            finally:
                from source_snapshot import clear_source_snapshot_epochs
                clear_source_snapshot_epochs()
                # Standalone captures are retained by the snapshot owner/reaper.

    def test_unbounded_supervised_child_finishes_normally(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_supervised([sys.executable, '-c', 'import time; time.sleep(1.2); print("completed")'],
                                    Path(raw), timeout_seconds=0)
            self.assertEqual(0, result.returncode)
            self.assertIn('completed', result.stdout)


if __name__ == '__main__':
    unittest.main()
