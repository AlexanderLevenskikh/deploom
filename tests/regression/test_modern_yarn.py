"""Modern Yarn contracts: selectors, profiles, runtime and update semantics."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from package_manager_profile import resolve_package_manager_profile, install_args_for_profile
from lockfile_consistency import parse_yarn_lock_v1, exact_yarn_lock_version, validate_yarn_lock
from manual_dependency_audit import canonical_yarn_inventory, run_audit, dependency_input_identity
from iterative_migration import _yarn_lock_installed_version
from yarn_berry import lock_entries, require_runtime_version, update_berry_lockfile


class ModernYarnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {'name': 'root', 'packageManager': 'yarn@4.0.0', 'dependencies': {'sample': '^1.0.0'}}
        (self.root/'package.json').write_text(json.dumps(self.manifest))
        (self.root/'.yarnrc.yml').write_text('nodeLinker: node-modules\n')
        self.lock = self.root/'yarn.lock'
        self.lock.write_text('__metadata:\n  version: 8\n"sample@npm:^1.0.0, sample@npm:~1.2.0":\n  version: 1.2.3\n  resolution: "sample@npm:1.2.3"\n  dependencies:\n    child: "npm:^2.0.0"\n"sample@npm:^9.0.0":\n  version: 9.0.1\n  resolution: "sample@npm:9.0.1"\n"child@npm:^2.0.0":\n  version: 2.0.5\n  resolution: "child@npm:2.0.5"\n')
    def profile(self, version='4.0.0'):
        return resolve_package_manager_profile(self.root, package_json={**self.manifest, 'packageManager': 'yarn@'+version}, lockfile=self.lock)
    def test_classic_and_modern_install_flags_are_separate(self):
        for version, skip in [('2.4.3','--skip-builds'), ('3.8.7','--mode=skip-build'), ('4.0.0','--mode=skip-build')]:
            with self.subTest(version=version):
                self.assertEqual(['install', skip], install_args_for_profile(self.profile(version), ignore_scripts=True))
                self.assertEqual(['install', '--immutable'], install_args_for_profile(self.profile(version), ignore_scripts=False, frozen=True))
        self.lock.write_text('# yarn lockfile v1\n');(self.root/'.yarnrc.yml').unlink()
        self.assertEqual(['install', '--frozen-lockfile', '--ignore-scripts'], install_args_for_profile(self.profile('1.22.22'),ignore_scripts=True,frozen=True))
    def test_exact_direct_selector_never_takes_transitive_version(self):
        self.assertEqual('1.2.3', exact_yarn_lock_version(self.lock,'sample','^1.0.0'))
        self.assertEqual('1.2.3',_yarn_lock_installed_version(self.lock,'sample','~1.2.0'))
        self.assertIsNone(_yarn_lock_installed_version(self.lock,'sample','^7.0.0'))
        self.assertEqual([],validate_yarn_lock(self.root,self.manifest,self.lock))
        inventory=canonical_yarn_inventory(self.root)
        self.assertTrue(inventory['complete'],inventory)
        self.assertEqual([('child','2.0.5'),('sample','1.2.3')],inventory['pairs'])
    def test_duplicate_keys_and_invalid_entries_fail_closed(self):
        for data in ('__metadata: {}\n__metadata: {}\n', '__metadata: {}\n"sample@npm:1":\n  version: 1\n'):
            self.lock.write_text(data)
            with self.assertRaises(ValueError): lock_entries(self.lock)
    def test_root_resolutions_override_the_parent_edge(self):
        for rule in ('child', 'sample/child'):
            with self.subTest(rule=rule):
                (self.root/'package.json').write_text(json.dumps({**self.manifest, 'resolutions': {rule: '2.0.0'}}))
                extra = '"child@npm:2.0.0":\n  version: 2.0.0\n  resolution: "child@npm:2.0.0"\n'
                original = self.lock.read_text()
                self.lock.write_text(original + extra)
                inventory = canonical_yarn_inventory(self.root)
                self.assertTrue(inventory['complete'], inventory)
                self.assertEqual([('child','2.0.0'),('sample','1.2.3')],inventory['pairs'])
                self.lock.write_text(original)
    def test_workspace_topology_does_not_walk_unrelated_backend(self):
        from project_topology import resolve_project_topology
        app=self.root/'packages'/'app';app.mkdir(parents=True)
        (app/'package.json').write_text('{"name":"app"}')
        (self.root/'package.json').write_text(json.dumps({**self.manifest,'workspaces':['./packages/*/']}))
        subprocess.run(['git', '-C', str(self.root), 'init', '-q'], check=True, capture_output=True)
        with patch('project_topology.os.walk',side_effect=AssertionError('unrelated repository traversal')):
            topology=resolve_project_topology(app,allow_discovery=False,require_supported=True)
        self.assertIn((app/'package.json').resolve(),topology.workspace_member_manifests)
    def test_missing_transitive_edge_stays_incomplete(self):
        self.lock.write_text(self.lock.read_text().split('"child@npm:^2.0.0":')[0])
        inventory=canonical_yarn_inventory(self.root)
        self.assertFalse(inventory['complete']);self.assertTrue(inventory['unresolvedEdges'])
    def test_pnp_unknown_major_and_unpinned_versions_remain_infrastructure(self):
        for linker in ('pnp','pnpm'):
            (self.root/'.yarnrc.yml').write_text('nodeLinker: '+linker+'\n')
            self.assertFalse(self.profile().authoritative_supported)
        (self.root/'.yarnrc.yml').write_text('nodeLinker: node-modules\n')
        self.assertFalse(self.profile('5.0.0').authoritative_supported)
        self.assertFalse(resolve_package_manager_profile(self.root, package_json={'name':'root'},lockfile=self.lock).authoritative_supported)
    def test_runtime_version_mismatch_is_rejected(self):
        with patch('yarn_berry.subprocess.run',return_value=subprocess.CompletedProcess([],0,'1.22.22\n','')):
            with self.assertRaisesRegex(RuntimeError,'RUNTIME_VERSION_MISMATCH'):
                require_runtime_version(self.profile(),'yarn',self.root,{})
    def test_auto_audit_uses_inventory_without_npm_range_resolution(self):
        with patch('manual_dependency_audit._yarn_inventory_audit',return_value={'complete':True}) as inventory, patch('manual_dependency_audit._yarn_npm_lock_audit') as bridge:
            self.assertTrue(run_audit(self.root,'yarn','',yarn_audit_engine='auto')['complete'])
            inventory.assert_called_once();bridge.assert_not_called()
    def test_update_uses_modern_args_env_and_immutable_postcheck(self):
        calls=[]
        def run(args,**kwargs):
            calls.append((args,kwargs['env']));return subprocess.CompletedProcess(args,0,'','')
        with patch('lockfile_consistency.resolve_executable',return_value='yarn'):
            update,dedupe,verify=update_berry_lockfile(self.root,self.manifest,self.lock,'https://registry.example.invalid',{'yarnDeduplicate':'off'},runner=run)
        self.assertIn('--mode=skip-build',update);self.assertIn('--immutable',verify);self.assertEqual([],dedupe)
        self.assertEqual('false',calls[0][1]['YARN_ENABLE_IMMUTABLE_INSTALLS']);self.assertEqual('true',calls[1][1]['YARN_ENABLE_IMMUTABLE_INSTALLS'])
        self.assertEqual('https://registry.example.invalid',calls[0][1]['YARN_NPM_REGISTRY_SERVER'])
        self.assertFalse((self.root/'package-lock.json').exists())
    def test_nested_root_lock_changes_invalidate_audit_identity(self):
        app=self.root/'packages'/'app';app.mkdir(parents=True);(app/'package.json').write_text('{}')
        (self.root/'.git').mkdir()
        before,files=dependency_input_identity(app)
        self.assertIn('../../yarn.lock',files)
        self.lock.write_text(self.lock.read_text()+'# changed\n')
        self.assertNotEqual(before,dependency_input_identity(app)[0])
