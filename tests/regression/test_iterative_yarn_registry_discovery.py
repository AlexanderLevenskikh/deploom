"""Yarn Berry registry discovery must use the installing tool's registry/auth."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import iterative_migration as im
from iterative_registry_metadata import NpmDiscoveryMetadata
from iterative_registry_yarn import normalize_yarn_packument, modern_yarn_root


class YarnRegistryDiscoveryTests(unittest.TestCase):
    def test_packument_binds_dist_tag_to_published_version_and_engines(self):
        raw = json.dumps({
            'name': '@corp/widget',
            'dist-tags': {'latest': '3.0.0'},
            'versions': {
                '1.0.0': {'engines': {'node': '>=16'}},
                '2.0.0': {'engines': {'node': '>=18'}},
                '3.0.0': {'engines': {'node': '>=22'}},
            },
        })
        obj = normalize_yarn_packument(raw, '@corp/widget')
        self.assertEqual(obj['version'], '3.0.0')
        self.assertEqual(obj['engines.node'], '>=22')
        self.assertEqual(obj['enginesByVersion']['2.0.0'], '>=18')

    def test_does_not_invent_a_missing_dist_tag_or_version(self):
        for raw in (
            {'name': 'x', 'versions': ['1.0.0']},
            {'name': 'x', 'dist-tags': {'latest': '2.0.0'}, 'versions': ['1.0.0']},
            {'name': 'x', 'dist-tags': {'latest': '2.0.0'}, 'versions': ['1.0.0'], 'version': '1.0.0'},
        ):
            self.assertIsNone(normalize_yarn_packument(json.dumps(raw), 'x'))

    def test_nested_workspace_selects_canonical_yarn_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / 'packages' / 'nested'
            workspace.mkdir(parents=True)
            (root / 'package.json').write_text(json.dumps({'private': True, 'name': 'root', 'workspaces': ['packages/*'], 'packageManager': 'yarn@4.5.0'}))
            (root / 'yarn.lock').write_text('# yarn lockfile v4\n')
            (root / '.yarnrc.yml').write_text('nodeLinker: node-modules\n')
            (workspace / 'package.json').write_text(json.dumps({'name': 'nested', 'version': '1.0.0'}))
            self.assertEqual(modern_yarn_root(workspace), root)

    def test_yarn_native_registry_handles_scoped_metadata_without_npm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = root / 'packages' / 'app'
            project.mkdir(parents=True)
            (root / 'package.json').write_text(json.dumps({'name':'root','private':True,'workspaces':['packages/*'],'packageManager':'yarn@4.5.0'}))
            (root / 'yarn.lock').write_text('# yarn lockfile v4\n')
            (root / '.yarnrc.yml').write_text('nodeLinker: node-modules\n')
            (project / 'package.json').write_text(json.dumps({'name': 'app', 'version': '1.0.0'}))
            packument = {'name':'@corp/widget','dist-tags':{'latest':'3.0.0'},
                         'versions':{'1.0.0':{'engines':{'node':'>=16'}},
                                     '2.0.0':{'engines':{'node':'>=18'}},
                                     '3.0.0':{'engines':{'node':'>=22'}}}}
            calls = []
            def fake_run(argv, cwd, **kw):
                calls.append((argv, cwd, kw))
                return SimpleNamespace(returncode=0, stdout=json.dumps(packument))
            with patch.object(im, 'resolve_executable', side_effect=lambda x: 'yarn-fixture' if x=='yarn' else 'npm-fixture'), \
                 patch.object(im, '_run', side_effect=fake_run), \
                 patch.object(im, '_installed_version', return_value='1.0.0'):
                target, evidence = im._discover_one_target(project, '@corp/widget', '^1.0.0', {'YARN_ENABLE_IMMUTABLE_INSTALLS': 'false'}, '20.0.0')
            self.assertEqual(target, '2.0.0', evidence)
            self.assertEqual(evidence['status'], 'discovered-compatible')
            self.assertEqual(len(calls), 1)  # no per-alternative registry round-trips
            self.assertEqual(calls[0][0][:3], ['yarn-fixture', 'npm', 'info'])
            self.assertEqual(calls[0][1], root)
            self.assertEqual(evidence['registryMetadata']['source'], 'yarn-native')
            self.assertEqual(evidence['registryMetadata']['queries'], 1)

    def test_scoped_alternative_versions_preserve_scope_and_batch_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            calls = []
            def fake_run(argv, cwd, **kw):
                calls.append(argv)
                version = argv[3].rsplit('@', 1)[-1]
                return SimpleNamespace(returncode=0, stdout=json.dumps({'name':'@corp/widget', 'version':version, 'engines':{'node':'>=18'}}))
            with patch.object(im, 'modern_yarn_root', return_value=project), \
                 patch.object(im, 'resolve_executable', return_value='yarn-fixture'), \
                 patch.object(im, '_run', side_effect=fake_run):
                rows, count, failure, source = im._query_discovery_metadata(
                    project, ['view', '@corp/widget@2.0.0 || 2.1.0', 'version', 'engines.node', '--json'], None)
            self.assertEqual([row['version'] for row in rows], ['2.0.0', '2.1.0'])
            self.assertEqual(count, 2)
            self.assertEqual(source, 'yarn-native')
            self.assertEqual(failure, '')
            self.assertEqual([argv[3] for argv in calls], ['@corp/widget@2.0.0', '@corp/widget@2.1.0'])

    def test_yarn_auth_failure_abstains_without_npm_registry_fallback_or_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            output = 'YN0041: Invalid authentication token secret-value-NEVER-LOG'
            with patch.object(im, 'modern_yarn_root', return_value=project), \
                 patch.object(im, 'resolve_executable', return_value='yarn-fixture'), \
                 patch.object(im, '_run', return_value=SimpleNamespace(returncode=1, stdout=output, stderr='')), \
                 patch.object(im, '_installed_version', return_value='1.0.0') as installed:
                target, evidence = im._discover_one_target(project, '@corp/widget', '^1.0.0', None, None)
            self.assertIsNone(target)
            self.assertEqual(evidence['status'], 'registry-unavailable')
            self.assertEqual(evidence['registryMetadata']['errorClass'], 'authentication')
            self.assertNotIn('secret-value', json.dumps(evidence))

    def test_expired_discovery_deadline_does_not_probe_or_claim_registry_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(im, '_probe_budget_timeout', return_value=None), \
                 patch.object(im, '_run') as runner, \
                 patch.object(im, 'modern_yarn_root', return_value=Path(tmp)):
                metadata = NpmDiscoveryMetadata('foo', lambda args: im._query_discovery_metadata(Path(tmp), args, None))
                self.assertEqual(metadata.latest(), '')
                runner.assert_not_called()
                self.assertEqual(metadata.queries, 0)


if __name__ == '__main__':
    unittest.main()
