"""Parse the actual release DAG; both independent validation gates stay required."""
import copy
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def ancestors(jobs, name):
    seen = set()
    pending = [name]
    while pending:
        needs = jobs[pending.pop()].get('needs', [])
        if isinstance(needs, str):
            needs = [needs]
        for dependency in needs:
            if dependency not in seen:
                seen.add(dependency)
                pending.append(dependency)
    return seen


def validate_dag(jobs):
    for gate in ('validate', 'validate-windows'):
        if jobs[gate].get('needs'):
            raise AssertionError('validation gates must start independently')
        if not jobs[gate].get('steps'):
            raise AssertionError('validation gate must execute checks')
    for destination in ('package-windows', 'publish-release'):
        if not {'validate', 'validate-windows'}.issubset(ancestors(jobs, destination)):
            raise AssertionError('both validation gates must protect Windows publication')


class ReleaseDagTests(unittest.TestCase):
    def setUp(self):
        self.jobs = yaml.safe_load((ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8'))['jobs']

    def test_validation_runs_in_parallel_without_removing_either_gate(self):
        validate_dag(self.jobs)

    def test_real_builder_filter_keeps_runtime_and_excludes_release_only_scripts(self):
        import json
        import subprocess
        builder = yaml.safe_load((ROOT / 'desktop/electron-builder.yml').read_text(encoding='utf-8'))
        filters = builder['extraResources'][0]['filter']
        include = ['iterative_migration.py', 'run_writer_lock.py', 'scripts/iterative-node-drive.mjs',
                   'scripts/run-iterative-demo.py', 'scripts/run-real-world-demos.py', 'scripts/summarize-baseline-performance.py']
        exclude = ['scripts/check-public-sanitization.py', 'scripts/check-release-assets.mjs',
                   'scripts/check-package-windows-resilience.mjs', 'scripts/public-binary-allowlist.json',
                   'scripts/release-version.ps1', 'scripts/test-tool.ps1', 'scripts/test-tool.sh',
                   'scripts/test-tool-regression.ps1', 'scripts/test-tool-regression.sh', 'scripts/prepare-yarn-test-runtimes.mjs']
        script = "import {FileMatcher} from './node_modules/app-builder-lib/out/fileMatcher.js';import {statSync} from 'node:fs';import {resolve} from 'node:path';const config=JSON.parse(process.argv[1]);const root=process.argv[2];const filter=new FileMatcher(root,root,x=>x,config.filters).createFilter();for(const name of config.include)if(!filter(resolve(root,name),statSync(resolve(root,name))))throw Error('runtime excluded: '+name);for(const name of config.exclude)if(filter(resolve(root,name),statSync(resolve(root,name))))throw Error('release-only script included: '+name);"
        result = subprocess.run(['node', '--input-type=module', '-e', script,
                                 json.dumps({'filters': filters, 'include': include, 'exclude': exclude}), str(ROOT)],
                                cwd=ROOT / 'desktop', capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_contract_detects_serialization_and_missing_gate(self):
        serial = copy.deepcopy(self.jobs)
        serial['validate-windows']['needs'] = 'validate'
        with self.assertRaisesRegex(AssertionError, 'independently'):
            validate_dag(serial)
        for missing in ('validate', 'validate-windows'):
            weakened = copy.deepcopy(self.jobs)
            weakened['package-windows']['needs'] = [gate for gate in weakened['package-windows']['needs'] if gate != missing]
            with self.assertRaisesRegex(AssertionError, 'both validation gates'):
                validate_dag(weakened)


if __name__ == '__main__':
    unittest.main()
