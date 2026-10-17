"""Physical multi-process admission and exact private-tree reuse boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from artifact_integrity import build_artifact_tree_integrity
from cross_process_io import shared_io_slot
from scoped_source_policy import policy_for_project, policy_payload, policy_from_payload
from source_snapshot import build_source_tree_manifest
from verification_source_closure import require_complete_source_policy, reset_observed_trial_outputs

ROOT = Path(__file__).resolve().parents[2]


class MigrationIoOptimizationTests(unittest.TestCase):
    def test_other_process_waits_and_releases_after_owner_is_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "locked"
            env = {**os.environ, "DEPLOOM_IO_PM_SLOTS": "1", "PYTHONUTF8": "1"}
            worker = "from cross_process_io import shared_io_slot; from pathlib import Path; import sys,time; " + \
                "\nwith shared_io_slot('pm'):\n Path(sys.argv[1]).write_text('locked'); time.sleep(30)"
            owner = subprocess.Popen([sys.executable, "-c", worker, str(marker)], cwd=ROOT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists(), owner.communicate(timeout=2) if owner.poll() is not None else 'no marker')
                with patch.dict(os.environ, {"DEPLOOM_IO_PM_SLOTS": "1"}):
                    with self.assertRaises(TimeoutError):
                        with shared_io_slot("pm", timeout_seconds=.2):
                            self.fail("concurrent process exceeded shared capacity")
                    owner.kill()
                    owner.communicate(timeout=5)
                    with shared_io_slot("pm", timeout_seconds=2):
                        # Nested callers must not spend a second slot.
                        with shared_io_slot("pm", timeout_seconds=.2):
                            pass
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.communicate(timeout=5)

    def test_queue_wait_preserves_active_install_budget_and_expired_run_never_launches(self):
        import threading
        from types import SimpleNamespace
        import iterative_migration as migration
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); marker = root / 'locked'
            worker = "from cross_process_io import shared_io_slot; from pathlib import Path; import sys,time\nwith shared_io_slot('pm'):\n Path(sys.argv[1]).write_text('locked'); time.sleep(30)"
            env = {**os.environ, 'DEPLOOM_IO_PM_SLOTS': '1'}
            owner = subprocess.Popen([sys.executable, '-c', worker, str(marker)], cwd=ROOT, env=env)
            timer = None
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists())
                timer = threading.Timer(1.2, owner.kill); timer.start()
                with patch.dict(os.environ, {'DEPLOOM_IO_PM_SLOTS': '1'}), \
                     patch.object(migration, '_manager_and_install', return_value=('npm', 'fixture-npm', ['install'])), \
                     patch.object(migration, '_run', return_value=SimpleNamespace(returncode=0)) as run:
                    started = time.monotonic()
                    migration._run_install(root, timeout_seconds=1, progress_label='queue fixture')
                    self.assertGreaterEqual(time.monotonic() - started, 1)
                    self.assertEqual(1, run.call_args.kwargs['timeout_seconds'])
                    run.reset_mock()
                    with self.assertRaisesRegex(migration.BudgetExceededError, 'RUN_DEADLINE_EXCEEDED'):
                        migration._run_install(root, timeout_seconds=1, progress_label='expired fixture',
                                               run_deadline_at='2001-01-01T00:00:00Z')
                    run.assert_not_called()
            finally:
                if timer is not None:
                    timer.cancel(); timer.join(timeout=3)
                if owner.poll() is None: owner.kill()
                owner.communicate(timeout=5)

    def test_queued_copy_timeout_is_typed_and_never_creates_a_destination(self):
        from verification_workspace_backend import materialize_private_tree
        from reparse_materialization import ReparseMaterializationError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); marker = root / 'locked'; source = root / 'source'; source.mkdir()
            (source / 'keep').write_text('original'); target = root / 'target'
            child_temp = root / 'different-temp'; child_temp.mkdir()
            env = {**os.environ, 'DEPLOOM_IO_COPY_SLOTS': '1', 'TMPDIR': str(child_temp),
                   'TMP': str(child_temp), 'TEMP': str(child_temp)}
            worker = "from cross_process_io import shared_io_slot; from pathlib import Path; import sys,time\nwith shared_io_slot('copy'):\n Path(sys.argv[1]).write_text('locked'); time.sleep(30)"
            owner = subprocess.Popen([sys.executable, '-c', worker, str(marker)], cwd=ROOT, env=env)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists())
                with patch.dict(os.environ, {'DEPLOOM_IO_COPY_SLOTS': '1'}):
                    with self.assertRaisesRegex(ReparseMaterializationError, 'IO_SLOT_WAIT_TIMEOUT'):
                        materialize_private_tree(source, target, timeout_seconds=.2)
                self.assertFalse(target.exists())
                self.assertEqual('original', (source / 'keep').read_text())
            finally:
                if owner.poll() is None: owner.kill()
                owner.communicate(timeout=5)

    def test_nx_cache_does_not_change_source_but_nx_configuration_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nx.json").write_text('{}')
            (root / ".nx/cache").mkdir(parents=True)
            (root / ".nx/custom-input.json").write_text('source')
            policy = policy_for_project(root, root)
            self.assertEqual(policy, policy_from_payload(policy_payload(policy)))
            self.assertEqual([".nx/cache", ".nx/workspace-data"], require_complete_source_policy(policy)["cacheExclusions"])
            first = build_source_tree_manifest(root, policy=policy)
            (root / ".nx/cache/large-output").write_bytes(b'generated' * 10000)
            self.assertEqual(first.key, build_source_tree_manifest(root, policy=policy).key)
            (root / ".nx/custom-input.json").write_text('changed source')
            self.assertNotEqual(first.key, build_source_tree_manifest(root, policy=policy).key)

    def test_private_cache_and_overwritten_outputs_return_to_exact_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base / "trial"; root.mkdir()
            (root / "dist").mkdir(); (root / "dist/out.js").write_text('initial output')
            prepared = base / "prepared"; shutil.copytree(root, prepared)
            seal = build_artifact_tree_integrity(root, source_only=True)
            expected = {os.path.normcase(e['path']).replace('\\','/'):e for e in seal.source_entries}
            (root / "dist/out.js").write_text('new build output')
            (root / "node_modules/.vite-temp").mkdir(parents=True)
            (root / "node_modules/.vite-temp/config.mjs").write_text('temporary config')
            changes = ('3:dist/out.js', '3:node_modules', '1:node_modules/.vite-temp',
                       '1:node_modules/.vite-temp/config.mjs')
            self.assertTrue(reset_observed_trial_outputs(root, expected, changes, prepared_root=prepared,
                                                        dependency_roots=('node_modules',)))
            self.assertEqual('initial output', (root / 'dist/out.js').read_text())
            self.assertFalse((root / 'node_modules/.vite-temp').exists())
            (root / 'node_modules/real-package').mkdir()
            (root / 'node_modules/real-package/index.js').write_text('mutated')
            self.assertFalse(reset_observed_trial_outputs(root, expected,
                ('3:node_modules/real-package/index.js',), prepared_root=prepared, dependency_roots=('node_modules',)))

    def test_seed_directory_notifications_require_original_objects_and_no_package_events(self):
        from types import SimpleNamespace
        from block_psi57_yarn1_resolver_seed import SameRunResolverSeedStore
        from prepared_workspace_fastpath import WorkspaceChangeResult
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); modules = root / 'node_modules'; modules.mkdir()
            info = modules.stat()
            guard = SimpleNamespace(stop=lambda: WorkspaceChangeResult(changes=('3:node_modules',)))
            record = SimpleNamespace(guard=guard, workspace_root=root,
                directory_identities={'node_modules': (info.st_dev, info.st_ino)})
            self.assertEqual((True, 'clean'), SameRunResolverSeedStore._stop_guard(record))
            guard.stop = lambda: WorkspaceChangeResult(changes=('3:node_modules/pkg/index.js',))
            self.assertEqual((False, 'workspace-mutated'), SameRunResolverSeedStore._stop_guard(record))
            modules.rename(root / 'old-modules'); modules.mkdir()
            guard.stop = lambda: WorkspaceChangeResult(changes=('3:node_modules',))
            self.assertEqual((False, 'workspace-directory-replaced'), SameRunResolverSeedStore._stop_guard(record))

    def test_intermediate_recovery_does_not_force_durable_dependency_seal(self):
        from baseline_constraint_verifier import BaselineVerifyConfig, _durable_prepared_publication_policy
        config = BaselineVerifyConfig(recovery_checkpoints=True, publish_durable_prepared_artifact=True,
                                      verification_purpose='intermediate-candidate')
        self.assertEqual((False, 'intermediate-role-private'), _durable_prepared_publication_policy(config))
        config = BaselineVerifyConfig(recovery_checkpoints=True, publish_durable_prepared_artifact=True)
        self.assertEqual((True, 'durable-stage-recovery'), _durable_prepared_publication_policy(config))


if __name__ == '__main__':
    unittest.main()
