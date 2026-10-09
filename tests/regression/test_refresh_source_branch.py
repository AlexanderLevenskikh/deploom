"""Refresh uses configured Git source provenance before inspecting dependencies."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

class RefreshSourceBranchTests(unittest.TestCase):
    def git(self, root, *args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def run_refresh(self, source=None, dirty=False):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / 'project'
            project.mkdir()
            self.git(project, 'init', '-b', 'master')
            self.git(project, 'config', 'user.name', 'Fixture')
            self.git(project, 'config', 'user.email', 'fixture@example.test')
            manifest = project / 'package.json'
            manifest.write_text('{"name":"fixture","version":"1.0.0"}\n', encoding='utf-8')
            (project / 'package-lock.json').write_text(json.dumps({'name': 'fixture', 'version': '1.0.0', 'lockfileVersion': 3, 'packages': {'': {'name': 'fixture', 'version': '1.0.0'}}}), encoding='utf-8')
            self.git(project, 'add', '.')
            self.git(project, 'commit', '-m', 'initial')
            self.git(project, 'branch', 'develop')
            self.git(project, 'checkout', '-b', 'work')
            if dirty:
                manifest.write_text('{"name":"uncommitted"}\n', encoding='utf-8')
            before = manifest.read_bytes()
            entry = {'name': 'Fixture', 'path': str(project)}
            if source is not None:
                entry['git'] = {'sourceBranch': source}
            settings = root / 'settings.json'
            settings.write_text(json.dumps({'projects': [entry], 'registry': 'http://127.0.0.1:1'}), encoding='utf-8')
            env = {**os.environ, 'PYTHONUTF8': '1'}
            result = subprocess.run([sys.executable, str(ROOT / 'dependency_live_roadmap_generator.py'),
                '--project-settings', str(settings), '--refresh-source-branch', '--skip-release-intel',
                '--out', str(root / 'report.md'), '--json-out', str(root / 'report.json'), '--html-out', str(root / 'report.html')],
                cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
            if dirty:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('SOURCE_CHECKOUT_DIRTY', result.stderr)
                self.assertEqual(self.git(project, 'branch', '--show-current'), 'work')
            else:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git(project, 'branch', '--show-current'), source or 'master')
            self.assertEqual(manifest.read_bytes(), before)

    def test_selected_source_branch_is_used_by_actual_refresh_cli(self):
        self.run_refresh('develop')

    def test_unconfigured_source_defaults_to_master(self):
        self.run_refresh()

    def test_dirty_work_is_preserved_and_refresh_fails_closed(self):
        self.run_refresh('develop', dirty=True)

if __name__ == '__main__':
    unittest.main()
