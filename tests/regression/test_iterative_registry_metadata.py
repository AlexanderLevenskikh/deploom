from __future__ import annotations
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import iterative_migration as im

META = {'version': '3.0.0', 'dist-tags': {'latest': '3.0.0'}, 'versions': ['1.0.0', '2.0.0', '3.0.0'], 'engines.node': '>=22', 'time': {}}
ALTERNATIVES = [{'version': '1.0.0', 'engines.node': '>=16'}, {'version': '2.0.0', 'engines.node': '>=18'}]


class IterativeRegistryBatchTests(unittest.TestCase):
    def discover(self, responses, node='18.0.0', installed='1.0.0'):
        calls = []
        def run(argv, cwd, **kwargs):
            calls.append((argv, cwd, kwargs))
            data = responses[len(calls)-1]
            if isinstance(data, Exception): raise data
            return SimpleNamespace(returncode=0, stdout=json.dumps(data))
        env = {'npm_config_registry': 'https://fixture.invalid', 'npm_config_userconfig': 'fixture.npmrc', 'HTTPS_PROXY': 'http://fixture.invalid'}
        with patch.object(im, '_installed_version', return_value=installed), patch.object(im, 'resolve_executable', return_value='fixture-npm'), patch.object(im, '_run', side_effect=run):
            result = im._discover_one_target(Path('.'), 'fixture', '^1.0.0', env, node)
        return result, calls, env

    def test_compatible_fallback_uses_two_context_bound_queries(self):
        (target, row), calls, env = self.discover([META, ALTERNATIVES])
        self.assertEqual(target, '2.0.0')
        self.assertEqual(row['status'], 'discovered-compatible')
        self.assertEqual(len(calls), 2)
        self.assertEqual(row['registryMetadata']['queries'], 2)
        self.assertTrue(all(call[2]['env'] == env for call in calls))
        self.assertIn('--tag=latest', calls[0][0])
        self.assertIn(' || ', calls[1][0][2])
        self.assertIsNone(im._ACTIVE_REGISTRY_METADATA.get())

    def test_compatible_latest_is_one_query(self):
        (target, row), calls, _ = self.discover([META], node='22.0.0')
        self.assertEqual(target, '3.0.0')
        self.assertEqual(len(calls), 1)
        self.assertEqual(row['latestNodeSpec'], '>=22')

    def test_prerelease_and_never_downgrade_keep_existing_rules(self):
        data = {**META, 'version': '3.0.0-beta.1', 'dist-tags': {'latest': '3.0.0-beta.1'}}
        (target, _), _, _ = self.discover([data], node='22.0.0')
        self.assertEqual(target, '3.0.0-beta.1')
        (target, row), calls, _ = self.discover([META], installed='8.0.0')
        self.assertIsNone(target)
        self.assertEqual(row['status'], 'no-newer-compatible')
        self.assertEqual(len(calls), 1)

    def test_v_prefixed_tag_binds_the_same_manifest_and_engine(self):
        data = {**META, 'dist-tags': {'latest': 'v3.0.0'}}
        (target, row), calls, _ = self.discover([data], node='22.0.0')
        self.assertEqual(target, 'v3.0.0')
        self.assertEqual(row['latestNodeSpec'], '>=22')
        self.assertEqual(len(calls), 1)

    def test_failed_or_inconsistent_metadata_is_unknown(self):
        for data in (RuntimeError('fixture auth/timeout'), {}, {**META, 'version': '2.0.0'}):
            with self.subTest(data=data):
                (target, row), _, _ = self.discover([data])
                self.assertIsNone(target)
                self.assertEqual(row['status'], 'registry-unavailable')

    def test_unknown_alternative_engines_do_not_become_a_constraint(self):
        (target, row), _, _ = self.discover([META, []])
        self.assertIsNone(target)
        self.assertTrue(all(item['status'] in ('engines-unknown', 'not-newer-than-current') for item in row['rejected']))

    def test_cache_lease_deadline_returns_budget_result_without_spawning(self):
        import planning_cache
        with tempfile.TemporaryDirectory() as tmp, patch.object(planning_cache, 'public_cli_context', return_value={'fixture': 'public'}), \
             patch.object(planning_cache, 'cache_root', return_value=Path(tmp)), \
             patch.object(im, '_probe_budget_timeout', side_effect=[10, 10, None]), patch.object(im, '_run') as runner:
            result = im._query_discovery_metadata(Path(tmp), ['view', 'fixture', 'version', '--json'], {})
            self.assertEqual(result, (None, False, 'budget-exhausted', ''))
            runner.assert_not_called()

    def test_expired_deadline_does_not_return_warm_registry_metadata(self):
        import planning_cache
        context = {'fixture': 'public'}
        args = ['view', 'fixture', 'version', '--json']
        with tempfile.TemporaryDirectory() as tmp:
            planning_cache.write_entry(Path(tmp), ['registry-cli-v1', context, args], {'version': '3.0.0'})
            with patch.object(planning_cache, 'public_cli_context', return_value=context), \
                 patch.object(planning_cache, 'cache_root', return_value=Path(tmp)), \
                 patch.object(im, '_probe_budget_timeout', return_value=None), patch.object(im, '_run') as runner:
                self.assertEqual(im._query_discovery_metadata(Path(tmp), args, {}), (None, False, 'budget-exhausted', ''))
                runner.assert_not_called()

    def test_expired_deadline_spawns_nothing(self):
        previous = im._ACTIVE_DISCOVERY_DEADLINE
        try:
            im._ACTIVE_DISCOVERY_DEADLINE = time.monotonic()-1
            with patch.object(im, '_installed_version', return_value='1.0.0'), patch.object(im, '_run') as run:
                target, row = im._discover_one_target(Path('.'), 'fixture', '^1', {}, '18.0.0')
            run.assert_not_called()
            self.assertIsNone(target)
            self.assertEqual(row['status'], 'registry-unavailable')
            self.assertNotIn('registryMetadata', row)
        finally:
            im._ACTIVE_DISCOVERY_DEADLINE = previous

    @unittest.skipUnless(shutil.which('npm') and shutil.which('node'), 'npm/node required')
    def test_real_npm_cli_keeps_auth_and_default_tag_independent(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                hits.append(self.headers.get('Authorization'))
                if hits[-1] != 'Bearer fixture-token':
                    self.send_response(401)
                    self.end_headers()
                    return
                versions = {v: {'name': 'metadata-fixture', 'version': v, 'engines': {'node': engine}} for v, engine in [('1.0.0','>=16'),('2.0.0','>=18'),('3.0.0','>=22'),('4.0.0','>=26')]}
                body = json.dumps({'name':'metadata-fixture','_id':'metadata-fixture','dist-tags':{'latest':'3.0.0','next':'4.0.0'},'versions':versions,'time':{}}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                root=Path(raw)
                registry=f'http://127.0.0.1:{server.server_port}'
                (root/'package.json').write_text(json.dumps({'name':'fixture','dependencies':{'metadata-fixture':'^1.0.0'}}))
                (root/'package-lock.json').write_text(json.dumps({'lockfileVersion':3,'packages':{'':{'dependencies':{'metadata-fixture':'^1.0.0'}},'node_modules/metadata-fixture':{'version':'1.0.0'}}}))
                config=root/'fixture.npmrc'
                fixture_token = 'fixture-token'
                config.write_text(f'//127.0.0.1:{server.server_port}/:_authToken={fixture_token}\n')
                env={'npm_config_registry':registry,'npm_config_userconfig':str(config),'npm_config_tag':'next','npm_config_update_notifier':'false','npm_config_cache':str(root/'cache'),'HTTP_PROXY':'','HTTPS_PROXY':''}
                target,row=im._discover_one_target(root,'metadata-fixture','^1.0.0',env,'18.0.0')
                self.assertEqual(target,'2.0.0',row)
                self.assertEqual(row['registryMetadata']['queries'],2)
                self.assertEqual(hits,['Bearer fixture-token','Bearer fixture-token'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
