import dataclasses
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import baseline_constraint_verifier as verifier
import verification_proof as proof


class ControlSelectorTests(unittest.TestCase):
    def observe(self, selector, version, *, control=True, declared=None):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'package.json').write_text(json.dumps({'devDependencies': {'demo': declared or selector}}))
            package = root / 'node_modules' / 'demo'
            package.mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps({'name': 'demo', 'version': version}))
            return verifier.observed_resolved_assignment(root, {'demo': selector}, allow_declared_selectors=control)

    def test_caret_control_records_actual_exact_version(self):
        self.assertEqual({'demo': '1.59.1'}, self.observe('^1.55.0', '1.59.1'))

    def test_tilde_and_prerelease_outside_selector_are_rejected(self):
        for selector, version in [('~1.55.0', '1.59.1'), ('^1.55.0', '2.0.0'), ('^1.55.0', '1.60.0-beta.1')]:
            with self.subTest(selector=selector, version=version), self.assertRaises(verifier.ObservedResolutionError):
                self.observe(selector, version)

    def test_union_and_wildcard_selectors_are_observed_exactly(self):
        for selector in ['1.x', '^1.55.0 || ^2.0.0', '>=1.55.0 <2']:
            self.assertEqual({'demo': '1.59.1'}, self.observe(selector, '1.59.1'))

    def test_selected_exact_target_still_rejects_drift_in_every_mode(self):
        for control in [False, True]:
            for version in ['1.59.1', '1.55.0+build']:
                with self.subTest(control=control, version=version), self.assertRaises(verifier.ObservedResolutionError):
                    self.observe('1.55.0', version, control=control)

    def test_migration_cannot_use_control_selector_permission(self):
        with self.assertRaises(verifier.ObservedResolutionError):
            self.observe('^1.55.0', '1.59.1', control=False)

    def test_control_permission_requires_matching_manifest_declaration(self):
        with self.assertRaises(verifier.ObservedResolutionError):
            self.observe('^1.55.0', '1.59.1', declared='^1.50.0')

    def test_registry_tag_is_recorded_but_fixed_input_is_not_accepted(self):
        self.assertEqual({'demo': '1.59.1'}, self.observe('latest', '1.59.1'))
        with self.assertRaises(verifier.ObservedResolutionError):
            self.observe('file:../other', '1.59.1')

    def test_selector_control_proof_cannot_satisfy_exact_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            context = {'environmentKey': 'env', 'fixedResolverInputsKey': 'fixed'}
            with patch.object(proof, '_resolver_context_payload', return_value=context), patch.object(proof, 'tool_build_id', return_value='build'):
                args = dict(assignment={'demo': '^1.55.0'}, remove_packages=(), manager='npm', manager_executable='npm', registry='', project_checks='strict', commands=(), environment={}, source_snapshot_key='source')
                exact = proof.build_verification_proof_identity(root, **args)
                control = proof.build_verification_proof_identity(root, **args, assignment_semantics='declared-selectors')
            self.assertNotEqual(exact.resolver_input_key, control.resolver_input_key)
            self.assertNotEqual(exact.project_proof_key, control.project_proof_key)


@unittest.skipUnless(shutil.which('npm') and shutil.which('git'), 'npm and Git required')
class RealNpmControlSelectorTests(unittest.TestCase):
    def test_real_resolver_lifecycle_and_project_check_preserve_selector_and_pin_observation(self):
        blobs = {}
        for version in ['1.55.0', '1.59.1']:
            data = json.dumps({'name': 'control-demo', 'version': version}).encode()
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                entry = tarfile.TarInfo('package/package.json')
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            blobs[version] = stream.getvalue()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                if self.path == '/control-demo':
                    versions = {v: {'name': 'control-demo', 'version': v, 'dist': {'tarball': f'{url}/control-demo/-/{v}.tgz', 'shasum': hashlib.sha1(blob).hexdigest()}} for v, blob in blobs.items()}
                    body = json.dumps({'name': 'control-demo', 'dist-tags': {'latest': '1.59.1'}, 'versions': versions}).encode()
                    content_type = 'application/json'
                else:
                    version = self.path.rsplit('/', 1)[-1].removesuffix('.tgz')
                    if version not in blobs:
                        self.send_error(404)
                        return
                    body = blobs[version]
                    content_type = 'application/octet-stream'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        url = f'http://127.0.0.1:{server.server_port}'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / 'project'
                root.mkdir()
                manifest = json.dumps({'name': 'control-fixture', 'private': True, 'packageManager': 'npm@10.0.0', 'devDependencies': {'control-demo': '^1.55.0'}})
                (root / 'package.json').write_text(manifest)
                lock = {'name': 'control-fixture', 'lockfileVersion': 3, 'requires': True,
                        'packages': {'': {'name': 'control-fixture', 'devDependencies': {'control-demo': '^1.55.0'}},
                                     'node_modules/control-demo': {'version': '1.59.1', 'resolved': f'{url}/control-demo/-/1.59.1.tgz', 'dev': True}}}
                (root / 'package-lock.json').write_text(json.dumps(lock))
                (root / 'check.cjs').write_text("require('node:assert/strict').equal(require('control-demo/package.json').version, '1.59.1');\n")
                (root / 'mutate.cjs').write_text("const fs=require('node:fs'); const p='node_modules/control-demo/package.json'; const v=JSON.parse(fs.readFileSync(p)); v.version='1.59.2'; fs.writeFileSync(p,JSON.stringify(v));\n")
                def git(*args):
                    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
                git('init', '-b', 'master')
                git('config', 'user.email', 'control@example.invalid')
                git('config', 'user.name', 'Control Fixture')
                git('add', '.')
                git('commit', '-m', 'fixture')
                config = verifier.BaselineVerifyConfig(verification_purpose='baseline-control', commands=('node check.cjs',), project_checks='strict', registry=url, proof_cache_dir=str(Path(raw) / 'proof-cache'), publish_durable_prepared_artifact=False)
                result = verifier.verify_assignment(root, {'control-demo': '^1.55.0'}, config=config, run_project_checks=True, runtime_env={'npm_config_registry': url, 'npm_config_audit': 'false', 'npm_config_fund': 'false'})
                self.assertTrue(result.ok, result.summary)
                self.assertEqual({'control-demo': '1.59.1'}, result.observed_resolved_versions)
                changed = verifier.verify_assignment(root, {'control-demo': '^1.55.0'}, config=dataclasses.replace(config, commands=('node mutate.cjs',)), run_project_checks=True, runtime_env={'npm_config_registry': url, 'npm_config_audit': 'false', 'npm_config_fund': 'false'})
                self.assertFalse(changed.ok, 'a later version within the range must not replace the proven tree')
                self.assertEqual('unknown', changed.kind)
                self.assertRegex(changed.summary, 'DRIFT|TAINT|MUTAT')
                self.assertEqual(manifest, (root / 'package.json').read_text())
                self.assertFalse((root / 'node_modules').exists())
                self.assertEqual('', subprocess.run(['git', '-C', str(root), 'status', '--porcelain'], check=True, capture_output=True, text=True).stdout)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == '__main__':
    unittest.main()
