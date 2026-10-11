"""Offline physical npm verification: cold/warm install counts and shared-root isolation."""
from __future__ import annotations
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import baseline_constraint_verifier as verifier
import block_v_prepared_artifact as artifact_store

@unittest.skipUnless(shutil.which('npm') and shutil.which('git') and shutil.which('node'), 'npm/git/node required')
class PreparedInstallMeasurements(unittest.TestCase):
    def test_cold_warm_install_and_shared_lockfile_isolation(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            for name, data in {'package/package.json': b'{"name":"deploom-install-fixture","version":"1.0.0"}', 'package/index.js': b'module.exports="1.0.0"'}.items():
                entry = tarfile.TarInfo(name); entry.size = len(data); tar.addfile(entry, io.BytesIO(data))
        payload = archive.getvalue()
        class Registry(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                host = f'http://127.0.0.1:{self.server.server_port}'
                metadata = {'name': 'deploom-install-fixture', 'dist-tags': {'latest': '1.0.0'}, 'versions': {'1.0.0': {'name': 'deploom-install-fixture', 'version': '1.0.0', 'dist': {'tarball': host + '/fixture.tgz', 'shasum': hashlib.sha1(payload).hexdigest()}}}}
                data = payload if self.path.startswith('/fixture.tgz') else json.dumps(metadata).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                temporary = Path(raw).resolve()
                measurements = {}
                original_run = verifier._run
                calls = []; active = set(); guard = threading.Lock()
                def measured_run(argv, cwd, **kwargs):
                    install = any(str(arg) in ('install', 'ci') or ' install ' in str(arg) or ' ci ' in str(arg) for arg in argv)
                    key = os.path.normcase(str(Path(cwd).resolve()))
                    if install:
                        with guard:
                            self.assertNotIn(key, active, 'two writers used the same install root')
                            active.add(key)
                            calls.append(key)
                    try:
                        return original_run(argv, cwd, **kwargs)
                    finally:
                        if install:
                            with guard: active.remove(key)
                with patch.dict(os.environ, {'DEPLOOM_VERIFICATION_ROOT': str(temporary / 'substrate'), 'DEPLOOM_SOURCE_SCOPE_MODE': 'project-gitignore'}), patch.object(verifier, '_PREPARED_SNAPSHOT_ROOT', None), patch.object(artifact_store, '_CONFIGURED_ROOT', None), patch.object(verifier, '_run', measured_run):
                    # An owned fixture only: no user workspace is migrated.
                    for topology in ('root', 'monorepo'):
                        root = temporary / topology; root.mkdir()
                        (root / '.gitignore').write_text('node_modules/\n', encoding='utf-8')
                        projects = [root] if topology == 'root' else [root / 'packages' / 'a', root / 'packages' / 'b']
                        manifest = {'name': topology + '-fixture', 'version': '1.0.0', 'private': True}
                        if topology == 'monorepo': manifest['workspaces'] = ['packages/*']
                        (root / 'package.json').write_text(json.dumps(manifest), encoding='utf-8')
                        for index, project in enumerate(projects):
                            project.mkdir(parents=True, exist_ok=True)
                            child = {**manifest, 'name': f'fixture-{topology}-{index}', 'dependencies': {'deploom-install-fixture': '1.0.0'}} if topology == 'root' else {'name': f'fixture-{index}', 'version': '1.0.0', 'dependencies': {'deploom-install-fixture': '1.0.0'}}
                            (project / 'package.json').write_text(json.dumps(child), encoding='utf-8')
                            (project / 'check.cjs').write_text('if(require("deploom-install-fixture")!=="1.0.0")process.exit(41)', encoding='utf-8')
                        registry = f'http://127.0.0.1:{server.server_port}'
                        npm = shutil.which('npm'); argv, _ = verifier._command_prefix(npm, ['install', '--ignore-scripts', '--no-audit', '--no-fund', '--registry', registry, '--cache', str(temporary / 'npm-cache')])
                        initial = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=120)
                        self.assertEqual(0, initial.returncode, initial.stdout + initial.stderr)
                        for args in [('init', '-q'), ('config', 'user.name', 'Install Fixture'), ('config', 'user.email', 'fixture@example.invalid'), ('add', '.'), ('commit', '-qm', 'fixture')]:
                            subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
                        original_lock = (root / 'package-lock.json').read_bytes()
                        original_manifests = [p.joinpath('package.json').read_bytes() for p in projects]
                        config = verifier.BaselineVerifyConfig(enabled=True, project_checks='strict', commands=('node check.cjs',), registry=registry, timeout_seconds=120, attempt_timeout_seconds=300, proof_cache_dir=str(temporary / 'proof' / topology))
                        def verify(project):
                            return verifier.verify_assignment(project, {'deploom-install-fixture': '1.0.0'}, config=config, run_project_checks=True)
                        def batch():
                            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                                return list(pool.map(verify, projects))
                        before = len(calls); started = time.perf_counter(); cold = batch(); cold_ms = (time.perf_counter() - started) * 1000; cold_calls = len(calls) - before
                        for result in cold: self.assertTrue(result.ok, result.summary + '\n' + result.output)
                        self.assertGreater(cold_calls, 0, 'cold path must run a real package-manager install')
                        cold_roots = set(calls[before:])
                        self.assertNotIn(os.path.normcase(str(root)), cold_roots, 'source lockfile cannot be the install target')
                        if topology == 'monorepo': self.assertGreaterEqual(len(cold_roots), 2, 'members must install into distinct isolated clones')
                        before = len(calls); started = time.perf_counter(); warm = batch(); warm_ms = (time.perf_counter() - started) * 1000; warm_calls = len(calls) - before
                        for result in warm: self.assertTrue(result.ok, result.summary + '\n' + result.output)
                        self.assertLess(warm_calls, cold_calls, 'exact verified reuse must reduce repeated install calls')
                        self.assertEqual(original_lock, (root / 'package-lock.json').read_bytes())
                        self.assertEqual(original_manifests, [p.joinpath('package.json').read_bytes() for p in projects])
                        measurements[topology] = {'coldMs': round(cold_ms, 1), 'warmMs': round(warm_ms, 1), 'coldInstalls': cold_calls, 'warmInstalls': warm_calls, 'isolatedRoots': len(cold_roots), 'sourceLockUnchanged': True}
                print('[C4-install-measurement] ' + json.dumps(measurements, sort_keys=True), flush=True)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)