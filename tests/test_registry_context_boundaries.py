from __future__ import annotations
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import dependency_live_roadmap_generator as roadmap
from registry_metadata_cache import load_packument, store_packument


class RegistryContextBoundaryTests(unittest.TestCase):
    def test_future_nonfinite_and_boolean_timestamps_are_misses(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store_packument(root, 'https://fixture.invalid', 'a', {}, now=1000)
            file = next(root.rglob('*.json'))
            entry = json.loads(file.read_text())
            for timestamp in (1001, float('inf'), float('-inf'), float('nan'), True):
                with self.subTest(timestamp=timestamp):
                    entry['fetchedAtUnix'] = timestamp
                    file.write_text(json.dumps(entry))
                    self.assertIsNone(load_packument(root, 'https://fixture.invalid', 'a', 600, now=1000))

    def test_context_keys_and_transport_changes_do_not_mix(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store_packument(root, 'https://fixture.invalid', 'a', {'value': 1}, context='tenant-a', now=1000)
            self.assertIsNone(load_packument(root, 'https://fixture.invalid', 'a', 600, context='tenant-b', now=1001))
            self.assertEqual(load_packument(root, 'https://fixture.invalid', 'a', 600, context='tenant-a', now=1001), {'value': 1})

    def test_authenticated_fetch_and_parallel_prefetch_keep_the_callers_view(self):
        principals = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                principal = self.headers.get('Authorization')
                principals.append(principal)
                if not principal:
                    self.send_response(401); self.end_headers(); return
                version = '1.0.0' if principal == 'fixture-a' else '2.0.0'
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'name': self.path, 'dist-tags': {'latest': version}, 'versions': {version: {}}}).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                registry = f'http://127.0.0.1:{server.server_port}'
                client = roadmap.LiveDataClient(registry, timeout=2, batch_size=10, sleep_sec=0, metadata_cache_dir=Path(raw), metadata_cache_ttl=600)
                client.session.params = {'tenant': 'fixture-tenant'}
                client.session.headers['Authorization'] = 'fixture-a'
                self.assertEqual(client.fetch_npm_metadata('a')['dist-tags']['latest'], '1.0.0')
                client.session.headers['Authorization'] = 'fixture-b'
                self.assertEqual(client.fetch_npm_metadata('a')['dist-tags']['latest'], '2.0.0')
                roadmap._prefetch_registry_metadata(client, [('b', 'runtime', '^1.0.0'), ('c', 'runtime', '^1.0.0')], max_workers=2)
                self.assertTrue(all(client.npm_cache[name]['dist-tags']['latest'] == '2.0.0' for name in ('b', 'c')))
                self.assertEqual(client.npm_cache['b']['name'], '/b?tenant=fixture-tenant')
                self.assertEqual(principals[:2], ['fixture-a', 'fixture-b'])
                self.assertEqual(principals[2:], ['fixture-b', 'fixture-b'])
                self.assertEqual(list(Path(raw).rglob('*.json')), [], 'authenticated data must never be persisted')
                del client.session.headers['Authorization']
                with self.assertRaises(roadmap.RegistryInfrastructureError): client.fetch_npm_metadata('a')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)
