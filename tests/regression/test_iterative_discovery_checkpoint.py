"""Preparation progress survives interruption without granting proof."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import iterative_migration as im
from iterative_discovery_checkpoint import DiscoveryCheckpoint, discovery_identity, TTL_SECONDS


class DiscoveryCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'package.json').write_text(json.dumps({'name': 'fixture', 'dependencies': {'a': '1.0.0', 'b': '1.0.0'}}), encoding='utf-8')
        (self.root / 'package-lock.json').write_text('{}', encoding='utf-8')
        self.path = self.root / 'run' / 'preparation-discovery.json'
        self.identity = discovery_identity(self.root, {'node': '22'})

    def probe(self, project, name, declared, env, node):
        return '2.0.0', {'package': name, 'declared': declared, 'status': 'discovered', 'latest': '2.0.0'}

    def test_interrupted_discovery_resumes_only_remaining_packages(self):
        def kill(processed, total):
            raise RuntimeError('process interrupted')
        with patch.object(im, '_discover_one_target', side_effect=self.probe):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                im._discover_targets(self.root, {'a': '1.0.0', 'b': '1.0.0'}, None,
                    progress=kill, checkpoint=DiscoveryCheckpoint(self.path, self.identity))
        self.assertEqual(list(json.loads(self.path.read_text(encoding='utf-8'))['entries']), ['a'])
        with patch.object(im, '_discover_one_target', side_effect=self.probe) as probe:
            targets, rows = im._discover_targets(self.root, {'a': '1.0.0', 'b': '1.0.0'}, None,
                parallelism=2, checkpoint=DiscoveryCheckpoint(self.path, self.identity))
        self.assertEqual([call.args[1] for call in probe.call_args_list], ['b'])
        self.assertEqual(targets, {'a': '2.0.0', 'b': '2.0.0'})
        self.assertEqual(rows[0]['discoveryCheckpoint']['scope'], 'planning-only')
        self.assertIn('savedAt', rows[0]['discoveryCheckpoint'])
        self.assertEqual([row['package'] for row in rows], ['a', 'b'])
        self.assertFalse((self.path.parent / 'run.json').exists())
        self.assertFalse((self.path.parent / 'checkpoints').exists())

    def test_unavailable_or_budget_skipped_are_retried(self):
        cp = DiscoveryCheckpoint(self.path, self.identity)
        for status in ('registry-unavailable', 'discovery-budget-skipped', 'installed-version-unknown'):
            cp.record('a', None, {'package': 'a', 'declared': '1.0.0', 'status': status})
            self.assertIsNone(cp.lookup('a', '1.0.0'))
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            self.assertEqual(saved['entries']['a']['evidence']['status'], status)
        cp.record('a', '2.0.0', {'package': 'a', 'declared': '1.0.0', 'status': 'discovered', 'registryMetadata': {'errorClass': 'timeout'}})
        self.assertIsNone(cp.lookup('a', '1.0.0'))

    def test_ttl_corrupt_file_and_changed_identity_fail_closed(self):
        with patch('iterative_discovery_checkpoint.time.time', return_value=100):
            cp = DiscoveryCheckpoint(self.path, self.identity)
            target, row = self.probe(self.root, 'a', '1.0.0', None, None)
            cp.record('a', target, row)
            self.assertIsNotNone(cp.lookup('a', '1.0.0'))
            self.assertIsNone(cp.lookup('a', '^1.0.0'))
        with patch('iterative_discovery_checkpoint.time.time', return_value=100 + 23 * 3600):
            self.assertIsNotNone(DiscoveryCheckpoint(self.path, self.identity).lookup('a', '1.0.0'))
        with patch('iterative_discovery_checkpoint.time.time', return_value=100 + TTL_SECONDS):
            self.assertIsNone(DiscoveryCheckpoint(self.path, self.identity).lookup('a', '1.0.0'))
        self.assertIsNone(DiscoveryCheckpoint(self.path, 'different').lookup('a', '1.0.0'))
        self.path.write_text('truncated', encoding='utf-8')
        self.assertIsNone(DiscoveryCheckpoint(self.path, self.identity).lookup('a', '1.0.0'))

    def test_inconsistent_saved_target_is_reprobed(self):
        cp = DiscoveryCheckpoint(self.path, self.identity)
        target, row = self.probe(self.root, 'a', '1.0.0', None, None)
        cp.record('a', target, row)
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        saved['entries']['a']['target'] = '9.0.0'
        self.path.write_text(json.dumps(saved), encoding='utf-8')
        self.assertIsNone(DiscoveryCheckpoint(self.path, self.identity).lookup('a', '1.0.0'))
        saved['entries']['a']['target'] = target
        saved['entries']['a']['evidence']['registryMetadata'] = 'malformed'
        self.path.write_text(json.dumps(saved), encoding='utf-8')
        self.assertIsNone(DiscoveryCheckpoint(self.path, self.identity).lookup('a', '1.0.0'))

    def test_replay_does_not_extend_freshness(self):
        with patch('iterative_discovery_checkpoint.time.time', return_value=100):
            cp = DiscoveryCheckpoint(self.path, self.identity)
            target, row = self.probe(self.root, 'a', '1.0.0', None, None)
            cp.record('a', target, row)
        with patch('iterative_discovery_checkpoint.time.time', return_value=150):
            replayed_target, replayed_row = cp.lookup('a', '1.0.0')
            cp.record('a', replayed_target, replayed_row)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['entries']['a']['savedAt'], 100)

    def test_manifest_lock_auth_runtime_policy_invalidate_without_saved_secrets(self):
        for filename in ('package.json', 'package-lock.json', '.yarnrc.yml', '.npmrc'):
            path = self.root / filename
            old = path.read_bytes() if path.exists() else None
            path.write_text('changed', encoding='utf-8')
            self.assertNotEqual(discovery_identity(self.root, {'node': '22'}), self.identity)
            if old is None:
                path.unlink()
            else:
                path.write_bytes(old)
        self.assertNotEqual(discovery_identity(self.root, {'node': '24'}), self.identity)
        with patch.dict(os.environ, {'NPM_TOKEN': 'fixture-private-token'}):
            identity = discovery_identity(self.root, {'node': '22'})
            self.assertNotEqual(identity, self.identity)
            cp = DiscoveryCheckpoint(self.path, identity)
            target, row = self.probe(self.root, 'a', '1.0.0', None, None)
            cp.record('a', target, row)
        self.assertNotIn('fixture-private-token', self.path.read_text(encoding='utf-8'))

    def test_build_config_wires_progress_to_same_run_directory(self):
        from tests.regression.test_iterative_target_discovery import _base_args
        args = _base_args(str(self.root), run_dir=str(self.path.parent))
        with patch.object(im, '_discover_one_target', side_effect=self.probe) as probe:
            first = im.build_run_config(self.path.parent, args)
            second = im.build_run_config(self.path.parent, args)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(first['targets'], second['targets'])
        self.assertTrue(self.path.exists())


if __name__ == '__main__':
    unittest.main()
