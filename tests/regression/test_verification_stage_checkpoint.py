"""Cross-process command recovery uses physical proofs and prepared artifacts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from verification_stage_checkpoint import StageJournal, CommandCheckpoint, digest

ROOT = Path(__file__).resolve().parents[2]
WORKER = '''import json,sys
from pathlib import Path
from baseline_constraint_verifier import BaselineVerifyConfig,verify_assignment
project,cache,receipts,telemetry=map(Path,sys.argv[1:])
config=BaselineVerifyConfig(commands=("npm run lint","npm run lint:types","node build.cjs","node test.cjs"), proof_cache_dir=str(cache),
    telemetry_path=str(telemetry), recovery_checkpoints=True,recovery_journal_dir=str(receipts),
    project_checks="strict",timeout_seconds=30,attempt_timeout_seconds=120,publish_durable_prepared_artifact=True)
result=verify_assignment(project,{},config=config,run_project_checks=True)
print(json.dumps({"ok":result.ok,"kind":result.kind,"summary":result.summary,"receipts":result.command_checkpoint_refs}))
sys.exit(0 if result.ok else 1)
'''


class StageCheckpointTests(unittest.TestCase):
    def test_intent_is_not_pass_and_corrupt_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = StageJournal(tmp, {'source': 'a', 'runtime': 'b'})
            journal.write('build', 'RUNNING')
            self.assertIsNone(journal.pass_record('build'))
            journal.write('build', 'PASS', {'outputHash': 'kept'})
            self.assertEqual(journal.pass_record('build')['attempts'], 1)
            row = json.loads(journal.path('build').read_text(encoding='utf-8'))
            row['evidence']['outputHash'] = 'tampered'
            journal.path('build').write_text(json.dumps(row), encoding='utf-8')
            self.assertIsNone(journal.pass_record('build'))
            self.assertIsNone(StageJournal(tmp, {'source': 'changed'}).pass_record('build'))

    def test_tool_drift_during_command_does_not_publish_pass_under_new_identity(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            proof = SimpleNamespace(event_fields=lambda: {'sourceSnapshotKey': 'sealed'})
            prepared = {'workspaceRoot': str(root), 'projectRelative': '.', 'artifactContentKey': 'prepared'}
            checkpoint = CommandCheckpoint(root / 'records', proof, prepared, ['node check.cjs'], {})
            outcome = SimpleNamespace(returncode=0, stdout='', supervision=SimpleNamespace(quality='guaranteed-tree'))
            with patch('verification_stage_checkpoint.command_tool_closure', side_effect=['old-tool', 'new-tool']):
                receipt = checkpoint.begin(1, 'node check.cjs')
                self.assertEqual(checkpoint.complete(1, 'node check.cjs', outcome, True), '')
            self.assertEqual(json.loads(Path(receipt).read_text(encoding='utf-8-sig'))['status'], 'UNKNOWN')

    @unittest.skipUnless(shutil.which('node') and shutil.which('npm'), 'Node/npm required')
    def test_real_kill_after_build_resumes_tests_without_repeating_build_or_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = root / 'project'; project.mkdir()
            (project / 'package.json').write_text(json.dumps({'name': 'recovery-fixture', 'version': '1.0.0', 'private': True,
                'scripts': {'lint': 'node lint.cjs', 'lint:types': 'node lint-types.cjs'}}), encoding='utf-8')
            (project / 'package-lock.json').write_text(json.dumps({'name': 'recovery-fixture', 'version': '1.0.0', 'lockfileVersion': 3,
                'requires': True, 'packages': {'': {'name': 'recovery-fixture', 'version': '1.0.0'}}}), encoding='utf-8')
            for script in ('lint', 'lint-types'):
                (project / (script + '.cjs')).write_text(
                    "require('fs').appendFileSync(process.env.DEPLOOM_FIXTURE_TRACE," + json.dumps(script + '\n') + ");\n", encoding='utf-8')
            (project / 'build.cjs').write_text("require('fs').appendFileSync(process.env.DEPLOOM_FIXTURE_TRACE,'build\\n');\n", encoding='utf-8')
            (project / 'test.cjs').write_text("require('fs').appendFileSync(process.env.DEPLOOM_FIXTURE_TRACE,'test-start\\n');setTimeout(()=>process.exit(0),8000);\n", encoding='utf-8')
            trace, telemetry, receipts, cache = root / 'trace.log', root / 'telemetry.jsonl', root / 'receipts', root / 'cache'
            args = [sys.executable, '-c', WORKER, str(project), str(cache), str(receipts), str(telemetry)]
            isolated_temp = root / 'isolated-temp'; isolated_temp.mkdir()
            env = {**os.environ, 'PYTHONUTF8': '1', 'DEPLOOM_FIXTURE_TRACE': str(trace),
                'TMP': str(isolated_temp), 'TEMP': str(isolated_temp), 'TMPDIR': str(isolated_temp),
                'DEPLOOM_VERIFICATION_ROOT': str(root / 'verification-storage')}
            if os.name != 'nt':
                # Existing POSIX process groups are best-effort: descendants may
                # detach/scrub the ownership token. Durable PASS remains denied.
                for _ in range(2):
                    outcome = subprocess.run(args, cwd=ROOT, env=env, capture_output=True,
                        text=True, encoding='utf-8', timeout=90)
                    self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
                    self.assertEqual(json.loads(outcome.stdout.splitlines()[-1])['receipts'], [])
                for script in ('lint', 'lint-types', 'build', 'test-start'):
                    self.assertEqual(trace.read_text(encoding='utf-8').splitlines().count(script), 2)
                self.assertFalse(any(json.loads(path.read_text(encoding='utf-8')).get('stage') == 'project-command'
                    and json.loads(path.read_text(encoding='utf-8')).get('status') == 'PASS'
                    for path in receipts.glob('*.json')))
                return
            worker_log = root / 'worker.log'
            output = worker_log.open('w', encoding='utf-8')
            self.addCleanup(output.close)
            process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and process.poll() is None:
                if (receipts.exists() and trace.exists() and 'test-start' in trace.read_text(encoding='utf-8')
                    and any(json.loads(path.read_text(encoding='utf-8')).get('stage') == 'project-command'
                        and json.loads(path.read_text(encoding='utf-8')).get('status') == 'PASS' for path in receipts.glob('*.json'))):
                    break
                time.sleep(.1)
            else:
                process.terminate(); process.communicate(timeout=15)
                output.close()
                self.fail('No build receipt before deadline: ' + worker_log.read_text(encoding='utf-8'))
            process.terminate(); process.communicate(timeout=15)
            output.close()
            first_events = [json.loads(line) for line in telemetry.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(row.get('event') == 'verify.project-check.checkpoint-published' for row in first_events))
            self.assertEqual(trace.read_text(encoding='utf-8').splitlines().count('build'), 1)
            recovered = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertEqual(trace.read_text(encoding='utf-8').splitlines().count('build'), 1)
            events = [json.loads(line) for line in telemetry.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(row.get('event') == 'verify.project-check.checkpoint-hit' and row.get('check') == 3 for row in events), events[-10:])
            self.assertEqual(sum(row.get('event') == 'verify.resolver.start' for row in events), 1)
            self.assertEqual(sum(row.get('event') == 'verify.preparation.start' for row in events), 1)
            self.assertEqual(trace.read_text(encoding='utf-8').splitlines().count('test-start'), 2)
            for script in ('lint', 'lint-types'):
                self.assertEqual(trace.read_text(encoding='utf-8').splitlines().count(script), 1)
            # A changed execution environment cannot reuse either command PASS.
            changed = subprocess.run(args, cwd=ROOT, env={**env, 'DEPLOOM_FIXTURE_INPUT': 'different'},
                capture_output=True, text=True, encoding='utf-8', timeout=90)
            self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
            self.assertEqual(trace.read_text(encoding='utf-8').splitlines().count('build'), 2)


if __name__ == '__main__':
    unittest.main()
