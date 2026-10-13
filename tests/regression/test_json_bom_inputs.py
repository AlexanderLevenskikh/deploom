"""UTF-8 BOM is accepted at JSON input boundaries without changing source bytes."""
import ast
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import baseline_constraint_verifier as baseline
import dependency_live_roadmap_generator as generator
import iterative_delivery as delivery
import iterative_migration as migration
import lockfile_consistency as locks
import manual_dependency_audit as audit
from project_topology import resolve_project_topology
from scoped_source_policy import MODE_ENV
from source_snapshot import capture_durable_source_snapshot

ROOT = Path(__file__).resolve().parents[2]


class JsonBomInputTests(unittest.TestCase):
    def test_json_readers_accept_bom_and_still_reject_invalid_json(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'package.json'
            readers = (generator.read_json, locks.read_json, audit.read_json, migration._read_json)
            for encoding in ('utf-8', 'utf-8-sig'):
                path.write_text('{"name":"fixture","description":"Привет"}', encoding=encoding)
                original = path.read_bytes()
                for reader in readers:
                    with self.subTest(reader=reader.__module__, encoding=encoding):
                        self.assertEqual(reader(path)['description'], 'Привет')
                        self.assertEqual(path.read_bytes(), original)
            path.write_text('{broken', encoding='utf-8-sig')
            for reader in readers:
                with self.subTest(reader=reader.__module__), self.assertRaises(Exception):
                    reader(path)

    def test_bom_workspace_members_and_local_inputs_keep_checks_and_exact_bytes(self):
        with tempfile.TemporaryDirectory(prefix='deploom-bom-scope-') as raw:
            base = Path(raw); root = base / 'repo'; app = root / 'packages/app'; lib = root / 'shared/lib'
            app.mkdir(parents=True); lib.mkdir(parents=True)
            manifests = {
                root / 'package.json': {'name':'root','private':True,'packageManager':'yarn@4.0.0','workspaces':['packages/*']},
                app / 'package.json': {'name':'app','packageManager':'yarn@4.0.0','dependencies':{'local-lib':'file:../../shared/lib'},'scripts':{'build':'node check.cjs'}},
                lib / 'package.json': {'name':'local-lib','version':'1.0.0'},
            }
            for path, payload in manifests.items():
                path.write_text(json.dumps(payload), encoding='utf-8-sig')
            (root / 'yarn.lock').write_text('__metadata:\n  version: 8\n', encoding='utf-8-sig')
            (root / '.yarnrc.yml').write_text('yarnPath: .yarn/releases/yarn.cjs\nnodeLinker: node-modules\n', encoding='utf-8-sig')
            (root / '.gitignore').write_text('.yarn/\n', encoding='utf-8')
            (root / '.yarn/releases').mkdir(parents=True)
            yarn = root / '.yarn/releases/yarn.cjs'; yarn.write_text('// fixture runtime', encoding='utf-8')
            delivery.git(root, 'init', '-b', 'fixture')
            delivery.git(root, 'config', 'user.name', 'Fixture')
            delivery.git(root, 'config', 'user.email', 'fixture@example.invalid')
            delivery.git(root, 'add', '.')
            delivery.git(root, 'commit', '-m', 'BOM fixture')
            topology = resolve_project_topology(app, require_supported=True)
            self.assertEqual(topology.profile.family, 'yarn-berry')
            self.assertEqual(topology.profile.node_linker, 'node-modules')
            self.assertTrue(any('build' in command for command in baseline.discover_baseline_project_checks(app)))
            with patch.dict(os.environ, {MODE_ENV:'project-gitignore'}):
                snapshot = capture_durable_source_snapshot(app, base / 'snapshot', timeout_seconds=0)
            self.assertEqual((), snapshot.input_policy.scope_roots)
            self.assertTrue((snapshot.root / 'shared/lib/package.json').is_file())
            for path in [*manifests, root / 'yarn.lock', root / '.yarnrc.yml', yarn]:
                self.assertEqual((snapshot.root / path.relative_to(root)).read_bytes(), path.read_bytes())

    def test_core_json_file_decoders_do_not_reintroduce_plain_utf8(self):
        violations = []
        for path in ROOT.glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'json'
                        and node.func.attr == 'loads' and node.args):
                    continue
                for read in ast.walk(node.args[0]):
                    if not (isinstance(read, ast.Call) and isinstance(read.func, ast.Attribute)
                            and read.func.attr in ('read_text', 'decode')):
                        continue
                    values = [kw.value for kw in read.keywords if kw.arg == 'encoding']
                    if read.func.attr == 'decode': values += read.args[:1]
                    if any(isinstance(value, ast.Constant) and value.value == 'utf-8' for value in values):
                        violations.append(f'{path.name}:{read.lineno}')
        self.assertEqual(violations, [], 'Use utf-8-sig for JSON file inputs')
