"""Exercise the real check runner with controlled package/compiler/child fixtures."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DesktopCheckRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='deploom-check-runner-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'desktop/scripts/run-all-contract-checks.mjs', self.root / 'scripts/runner.mjs')
        compiler = self.root / 'node_modules/typescript/bin/tsc'
        compiler.parent.mkdir(parents=True)
        compiler.write_text("require('fs').appendFileSync('events','compile\\n');process.exit(process.env.FIXTURE_COMPILER_FAIL==='1'?1:0)", encoding='utf-8')
        for name in ('a', 'b', 'custom', 'pre'):
            (self.root / f'scripts/{name}.mjs').write_text(
                f"import fs from 'node:fs';fs.appendFileSync('events','{name}\\n');" +
                ("process.exit(process.env.FIXTURE_CHECK_FAIL==='1'?1:0)" if name == 'b' else ''), encoding='utf-8')
        (self.root / 'package.json').write_text(json.dumps({'scripts': {
            'precheck:a': 'tsc -p tsconfig.electron.json', 'check:a': 'node scripts/a.mjs',
            'precheck:b': 'tsc -p tsconfig.electron.json', 'check:b': 'node scripts/b.mjs',
            'precheck:custom': 'node scripts/pre.mjs', 'check:custom': 'node scripts/custom.mjs'}}), encoding='utf-8')

    def run_fixture(self, **overrides):
        import os
        result = subprocess.run(['node', 'scripts/runner.mjs'], cwd=self.root,
                                env={**os.environ, **overrides}, capture_output=True, text=True, timeout=20)
        events = (self.root / 'events').read_text(encoding='utf-8').splitlines()
        return result, events

    def test_compiles_once_runs_every_check_and_preserves_custom_lifecycle(self):
        result, events = self.run_fixture()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['compile', 'a', 'b', 'pre', 'custom'], events)

    def test_child_failure_remains_a_failing_gate_and_later_checks_run(self):
        result, events = self.run_fixture(FIXTURE_CHECK_FAIL='1')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(['compile', 'a', 'b', 'pre', 'custom'], events)
        self.assertIn('FAILED check:b', result.stderr)

    def test_failed_compilation_never_runs_checks_on_stale_outputs(self):
        result, events = self.run_fixture(FIXTURE_COMPILER_FAIL='1')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(['compile'], events)


if __name__ == '__main__':
    unittest.main()
