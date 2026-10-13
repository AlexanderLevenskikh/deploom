"""Default agent-state policy and bounded capture preserve source proof obligations."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import source_snapshot as source
from verification_source_closure import require_complete_source_policy, excluded_source_failure_evidence

class CaptureProgressPolicyTests(unittest.TestCase):
    def tearDown(self):
        source.clear_source_snapshot_epochs()

    def test_repository_omits_agent_state_but_keeps_dot_configuration(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'app'; root.mkdir()
            (root / 'package.json').write_text('{"name":"fixture"}')
            for name in ('.claude/worktrees/copy/source.js', '.codex/logs/session.json', '.env', '.storybook/main.js'):
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture')
            with patch.dict(os.environ, {'DEPLOOM_SOURCE_SCOPE_MODE':'repository'}):
                snapshot = source.capture_source_snapshot(root)
            self.addCleanup(source._force_rmtree, snapshot.container)
            self.assertFalse((snapshot.root / '.claude').exists())
            self.assertFalse((snapshot.root / '.codex').exists())
            self.assertTrue((snapshot.root / '.env').is_file())
            self.assertTrue((snapshot.root / '.storybook/main.js').is_file())
            self.assertEqual(['.claude', '.codex'], require_complete_source_policy(snapshot.input_policy)['agentStateExclusions'])
            evidence = excluded_source_failure_evidence(snapshot, "Cannot find module '.claude/worktrees/copy/source.js'", execution_root=snapshot.root, execution_cwd=snapshot.project_path)
            self.assertEqual(['.claude/worktrees/copy/source.js'], evidence)
            self.assertEqual([], excluded_source_failure_evidence(snapshot, "Cannot find module 'missing.js'", execution_root=snapshot.root, execution_cwd=snapshot.project_path))

    def test_sealed_c0_can_be_bound_to_original_without_recapture(self):
        import dataclasses
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'app'; root.mkdir()
            (root / 'package.json').write_text('{"name":"fixture"}')
            snapshot = source.capture_durable_source_snapshot(root, Path(raw) / 'c0')
            logical = dataclasses.replace(snapshot, original_project_path=root.resolve(), capture_root=root.resolve())
            with source.bind_source_snapshot(logical), patch.object(source, 'capture_source_snapshot', side_effect=AssertionError('unexpected recapture')):
                with source.verification_source_snapshot_scope(root) as current:
                    self.assertIs(current, logical)
                    source.validate_source_snapshot(current)
                    self.assertEqual(snapshot.key, current.key)
            (root / 'package.json').write_text('{"name":"changed"}')
            self.assertNotEqual(logical.manifest_key, source.build_source_tree_manifest(root).key)
            self.assertFalse(source.source_snapshot_live_source_continuity(logical))

    def test_retries_share_budget_and_never_start_after_expiration(self):
        with patch.object(source.time, 'monotonic', side_effect=[100, 101, 112]), patch.object(source, '_capture_once', side_effect=source.SourceCaptureError('SOURCE_CAPTURE_UNSTABLE: fixture')) as capture:
            with self.assertRaisesRegex(source.SourceCaptureError, 'SOURCE_MATERIALIZATION_TIMEOUT'):
                source._capture_source_snapshot_impl(Path('fixture'), timeout_seconds=10)
            self.assertEqual(1, capture.call_count)
            self.assertEqual(9, capture.call_args.kwargs['timeout_seconds'])

    def test_durable_publication_only_gets_remaining_budget(self):
        fake = unittest.mock.Mock(container=Path('owned-fixture'))
        with patch.object(source.time, 'monotonic', side_effect=[100, 101, 108]), patch.object(source, 'capture_source_snapshot', return_value=fake), patch.object(source, 'persist_source_snapshot', return_value=fake) as persist, patch.object(source, '_retire_snapshot_watcher'), patch.object(source, '_force_rmtree'):
            source.capture_durable_source_snapshot(Path('fixture'), Path('durable'), timeout_seconds=10)
            self.assertEqual(2, persist.call_args.kwargs['timeout_seconds'])

if __name__ == '__main__': unittest.main()
