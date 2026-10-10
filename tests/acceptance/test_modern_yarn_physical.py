"""Real pinned Yarn 2/3/4 installs, exact assignments and lifecycle checks."""
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
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from baseline_constraint_verifier import BaselineVerifyConfig, verify_assignment
from manual_dependency_audit import canonical_yarn_inventory, package_manager, run_audit
from lockfile_consistency import exact_yarn_lock_version, ensure_lockfile_consistency
from source_snapshot import capture_source_snapshot
from yarn_berry import lock_mapping


class ModernYarnPhysicalAcceptance(unittest.TestCase):
    def test_real_modern_versions_resolve_then_immutable_install_and_check_nested_package(self):
        configured = os.environ.get('DEPLOOM_TEST_YARN_ROOT')
        if not configured:
            self.skipTest('run node scripts/prepare-yarn-test-runtimes.mjs and set DEPLOOM_TEST_YARN_ROOT')
        node = shutil.which('node')
        self.assertTrue(node)
        runtimes = Path(configured)
        archives = {}
        for version in ('1.0.0', '2.0.0'):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w:gz') as tar:
                for name, content in {'package/package.json': json.dumps({'name': 'deploom-yarn-fixture', 'version': version, 'scripts': {'postinstall': 'node -e "require(\'fs\').writeFileSync(\'built.txt\',\'built\')"'}}).encode(), 'package/index.js': f'module.exports = "{version}"'.encode()}.items():
                    info = tarfile.TarInfo(name); info.size = len(content); tar.addfile(info, io.BytesIO(content))
            archives[version] = data.getvalue()
        class Registry(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path.startswith('/tar/'):
                    data = archives[self.path.split('/')[-1]]
                else:
                    host = f'http://127.0.0.1:{self.server.server_port}'
                    versions = {v: {'name': 'deploom-yarn-fixture', 'version': v, 'scripts': {'postinstall': 'node -e "require(\'fs\').writeFileSync(\'built.txt\',\'built\')"'}, 'dist': {'tarball': host+'/tar/'+v, 'shasum': hashlib.sha1(archives[v]).hexdigest()}} for v in archives}
                    data = json.dumps({'name': 'deploom-yarn-fixture', 'versions': versions, 'dist-tags': {'latest': '2.0.0'}}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                data = b'{}'  # npm bulk-advisory endpoint returns a package/advisory map
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        registry = f'http://127.0.0.1:{server.server_port}'
        for version in ('2.4.3', '3.8.7', '4.0.0'):
            with self.subTest(yarn=version), tempfile.TemporaryDirectory(prefix='deploom-modern-yarn-') as tmp:
                root = Path(tmp)/'repo'; app = root/'Web'/'App'; app.mkdir(parents=True)
                runtime = runtimes/version/'node_modules'/'@yarnpkg'/'cli-dist'/'bin'/'yarn.js'
                self.assertTrue(runtime.is_file(), str(runtime))
                local = root/'.yarn'/'releases'/'yarn.cjs'; local.parent.mkdir(parents=True)
                shutil.copyfile(runtime, local)
                (root/'package.json').write_text(json.dumps({'name': 'modern-root', 'private': True, 'packageManager': 'yarn@'+version, 'workspaces': ['./Web/App/']}))
                (app/'package.json').write_text(json.dumps({'name': 'modern-app', 'version': '1.0.0', 'dependencies': {'deploom-yarn-fixture': '1.0.0'}}))
                (root/'.gitignore').write_text('node_modules/\n.yarn/cache/\n.yarn/install-state.gz\n.yarn/releases/\n')
                (root/'.yarnrc.yml').write_text(f'yarnPath: .yarn/releases/yarn.cjs\nnodeLinker: node-modules\nenableGlobalCache: false\nnpmRegistryServer: "{registry}"\nunsafeHttpWhitelist:\n  - 127.0.0.1\n')
                (root/'backend.sql').write_text('unrelated backend')
                (app/'check.js').write_text('const fs=require("fs"); const p=require.resolve("deploom-yarn-fixture/package.json"); if(require("deploom-yarn-fixture")!=="2.0.0" || !fs.existsSync(require("path").join(require("path").dirname(p),"built.txt"))) process.exit(41); console.log("MODERN_YARN_CHECK_PASS");')
                env = {**os.environ, 'YARN_ENABLE_IMMUTABLE_INSTALLS': 'false'}
                initial = subprocess.run([node, str(local), 'install'], cwd=root, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(0, initial.returncode, initial.stdout+initial.stderr)
                for args in [('init', '-q'), ('config', 'user.name', 'Modern Yarn'), ('config', 'user.email', 'modern@example.invalid'), ('add', '.'), ('commit', '-qm', 'fixture')]:
                    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
                # Use a project-local launcher: its delegation must select the pinned version.
                bin_dir = runtimes/'4.0.0'/'node_modules'/'.bin'
                runtime_env = {**os.environ, 'PATH': str(bin_dir)+os.pathsep+os.environ['PATH']}
                with patch.dict(os.environ, {**runtime_env, 'DEPLOOM_SOURCE_SCOPE_MODE': 'project-gitignore'}):
                    snapshot = capture_source_snapshot(app)
                    self.assertFalse((snapshot.root/'backend.sql').exists())
                    self.assertTrue((snapshot.root/'.yarn/releases/yarn.cjs').is_file())
                    config = BaselineVerifyConfig(enabled=True, project_checks='strict', commands=('node check.js',), registry=registry, timeout_seconds=120, attempt_timeout_seconds=300, proof_cache_dir=str(Path(tmp)/'proof'))
                    result = verify_assignment(app, {'deploom-yarn-fixture': '2.0.0'}, config=config, run_project_checks=True, runtime_env=runtime_env)
                    self.assertTrue(result.ok, result.summary+'\n'+result.output)
                    self.assertEqual('2.0.0', result.observed_resolved_versions['deploom-yarn-fixture'])
                    self.assertEqual('1.0.0', exact_yarn_lock_version(root/'yarn.lock', 'deploom-yarn-fixture', '1.0.0'))
                    self.assertEqual('yarn', package_manager(app))
                    inventory = canonical_yarn_inventory(app)
                    self.assertTrue(inventory['complete'], inventory)
                    self.assertEqual([('deploom-yarn-fixture', '1.0.0')], inventory['pairs'])
                    self.assertFalse((root/'package-lock.json').exists())
                    # Real generator lock-sync path must use Berry args too.
                    manifest = json.loads((app/'package.json').read_text())
                    manifest['dependencies']['deploom-yarn-fixture'] = '2.0.0'
                    (app/'package.json').write_text(json.dumps(manifest))
                    state = ensure_lockfile_consistency(app, registry, {'yarnDeduplicate': 'auto'}, mode='update')
                    self.assertEqual([], state.issues)
                    self.assertEqual('2.0.0', exact_yarn_lock_version(root/'yarn.lock', 'deploom-yarn-fixture', '2.0.0'))
                    self.assertFalse((root/'node_modules/deploom-yarn-fixture/built.txt').exists(), 'lock sync must skip lifecycle builds')
                    audit = run_audit(app, 'yarn', registry, runtime_env=runtime_env)
                    self.assertTrue(audit['complete'], audit)
                    self.assertEqual('yarn-inventory', audit['engine'])
                    self.assertFalse((root/'package-lock.json').exists())
                print(f'MODERN_YARN {version}: resolver + immutable lifecycle + nested checks PASS', flush=True)
