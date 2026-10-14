"""Declared npm file:. links stay private through hashing and materialization."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from artifact_integrity import build_artifact_tree_integrity
from reparse_materialization import inventory_reparse_plan, ReparseMaterializationError, _create_junction, _remove_destination
from verification_workspace_backend import materialize_private_tree


class PackageSelfLinkTests(unittest.TestCase):
    def link(self, path, target):
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            _create_junction(path, target)
        else:
            path.symlink_to(target, target_is_directory=True)

    def test_declared_scoped_and_unscoped_aliases_rebase_to_private_copy(self):
        for name in ('fixture-loader', '@fixture/loader'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve(); root = base / 'source'; root.mkdir()
                target = base / 'clone'
                (root / 'package.json').write_text(json.dumps({'name': '@fixture/widget', 'version': '1.0.0',
                    'devDependencies': {name: 'file:.'}}), encoding='utf-8')
                (root / 'package-lock.json').write_text(json.dumps({'name': '@fixture/widget', 'version': '1.0.0',
                    'lockfileVersion': 3, 'packages': {'': {'name': '@fixture/widget', 'version': '1.0.0',
                        'devDependencies': {name: 'file:.'}}, 'node_modules/' + name: {'resolved': '', 'link': True}}}), encoding='utf-8')
                (root / 'index.cjs').write_text('module.exports=42;', encoding='utf-8')
                link = root / 'node_modules' / name
                self.link(link, root)
                copied = target / 'node_modules' / name
                try:
                    plan = inventory_reparse_plan(root)
                    self.assertEqual(len(plan), 1)
                    self.assertEqual(plan[0].target_relative, '.')
                    if os.name == 'nt':
                        from baseline_constraint_verifier import _canonical_workspace_reparse_plan
                        declared = _canonical_workspace_reparse_plan(root, root)
                        self.assertEqual(declared[0].authority, 'workspace-private-upper')
                        self.assertEqual(build_artifact_tree_integrity(root, reparse_plan=declared).key,
                            build_artifact_tree_integrity(root).key)
                    before = build_artifact_tree_integrity(root)
                    materialize_private_tree(root, target, timeout_seconds=30, reparse_plan=plan)
                    self.assertEqual(copied.resolve(), target)
                    self.assertEqual(before.key, build_artifact_tree_integrity(target).key)
                    (copied / 'index.cjs').write_text('module.exports=99;', encoding='utf-8')
                    self.assertEqual((root / 'index.cjs').read_text(encoding='utf-8'), 'module.exports=42;')
                finally:
                    _remove_destination(copied)
                    _remove_destination(link)

    def test_undeclared_ancestor_cycles_and_external_targets_remain_rejected(self):
        for declaration in ({}, {'fixture-loader': '^1.0.0'}):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                (root / 'package.json').write_text(json.dumps({'devDependencies': declaration}), encoding='utf-8')
                link = root / 'node_modules' / 'fixture-loader'; self.link(link, root)
                try:
                    with self.assertRaisesRegex(ReparseMaterializationError, 'CYCLE_UNSUPPORTED'):
                        inventory_reparse_plan(root)
                finally:
                    _remove_destination(link)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve(); root = base / 'root'; root.mkdir(); outside = base / 'outside'; outside.mkdir()
            (root / 'package.json').write_text('{"devDependencies":{"fixture-loader":"file:."}}', encoding='utf-8')
            link = root / 'node_modules' / 'fixture-loader'; self.link(link, outside)
            try:
                with self.assertRaisesRegex(ReparseMaterializationError, 'EXTERNAL_TARGET_UNSUPPORTED'):
                    inventory_reparse_plan(root)
            finally:
                _remove_destination(link)

    @unittest.skipUnless(shutil.which('node') and shutil.which('npm'), 'Node/npm required')
    def test_real_npm_self_alias_completes_resolver_lifecycle_publication_and_check(self):
        from baseline_constraint_verifier import BaselineVerifyConfig, verify_assignment
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve(); root = base / 'project'; root.mkdir()
            (root / 'package.json').write_text(json.dumps({'name': '@fixture/widget', 'version': '1.0.0',
                'private': True, 'main': 'index.cjs', 'devDependencies': {'fixture-loader': 'file:.'}}), encoding='utf-8')
            (root / 'index.cjs').write_text('module.exports=42;', encoding='utf-8')
            (root / 'check.cjs').write_text("if(require('fixture-loader')!==42)process.exit(1);", encoding='utf-8')
            installed = subprocess.run([shutil.which('npm'), 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund'],
                cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            config = BaselineVerifyConfig(commands=('node check.cjs', 'node check.cjs'), proof_cache_dir=str(base / 'cache'),
                recovery_checkpoints=True, recovery_journal_dir=str(base / 'records'),
                publish_durable_prepared_artifact=True, project_checks='strict', timeout_seconds=60)
            result = verify_assignment(root, {}, config=config, run_project_checks=True)
            self.assertTrue(result.ok, result.summary)
            self.assertEqual((root / 'node_modules' / 'fixture-loader').resolve(), root)
            self.assertEqual((root / 'index.cjs').read_text(encoding='utf-8'), 'module.exports=42;')


if __name__ == '__main__':
    unittest.main()
