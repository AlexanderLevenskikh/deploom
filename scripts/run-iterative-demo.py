"""Run a retained, physical 12/24-dependency cumulative migration demo.

Uses the same real CLI driver as physical acceptance. No fake verifier, no
external agent session, and no changes to a user's project. Scripted repairs
are proposals; only verify-exact may accept a checkpoint.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import subprocess
import signal
import sys
import time
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('physical_driver', ROOT / 'tests/acceptance/test_iterative_migration_physical.py')
physical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(physical)

OLD = {'is-number': '5.0.0', 'is-finite': '1.0.0', 'is-string': '1.0.7',
       'is-boolean': '0.0.1', 'is-date-object': '1.0.5', 'is-symbol': '1.0.4',
       'is-bigint': '1.0.4', 'is-regex': '1.1.4', 'is-negative-zero': '2.0.2',
       'is-map': '2.0.2', 'is-set': '2.0.2', 'object-is': '1.1.5'}
NEW = {'is-number': '7.0.0', 'is-finite': '1.1.0', 'is-string': '1.1.1',
       'is-boolean': '0.0.2', 'is-date-object': '1.1.0', 'is-symbol': '1.1.1',
       'is-bigint': '1.1.0', 'is-regex': '1.2.1', 'is-negative-zero': '2.0.3',
       'is-map': '2.0.3', 'is-set': '2.0.3', 'object-is': '1.1.6'}

EXTRA_OLD = {'is-array-buffer': '3.0.2', 'is-typed-array': '1.1.9',
             'is-data-view': '1.0.0', 'is-shared-array-buffer': '1.0.2',
             'is-weakmap': '2.0.1', 'is-weakset': '2.0.2',
             'is-generator-function': '1.0.10', 'is-async-function': '2.0.0',
             'is-callable': '1.2.4', 'has-symbols': '1.0.3',
             'has-tostringtag': '1.0.0', 'isobject': '3.0.1'}
EXTRA_NEW = {'is-array-buffer': '3.0.5', 'is-typed-array': '1.1.15',
             'is-data-view': '1.0.2', 'is-shared-array-buffer': '1.0.4',
             'is-weakmap': '2.0.2', 'is-weakset': '2.0.4',
             'is-generator-function': '1.1.0', 'is-async-function': '2.1.1',
             'is-callable': '1.2.7', 'has-symbols': '1.1.0',
             'has-tostringtag': '1.0.2', 'isobject': '4.0.0'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', default='.dependency-roadmap/iterative-demo')
    parser.add_argument('--packages', type=int, choices=(12, 24), default=24)
    parser.add_argument('--seed-cache', help='Reuse an existing demo npm cache inside this repository')
    args = parser.parse_args()
    old = {**OLD, **(EXTRA_OLD if args.packages == 24 else {})}
    new = {**NEW, **(EXTRA_NEW if args.packages == 24 else {})}
    blocked = {'is-string', *({'is-weakset'} if args.packages == 24 else set())}
    output = (ROOT / args.output_root).resolve()
    if not output.is_relative_to(ROOT):
        parser.error('output-root must be inside this repository')
    output.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(output)
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    cls = physical.IterativeMigrationPhysicalAcceptance
    cls.setUpClass()
    if args.seed_cache:
        cached = (ROOT / args.seed_cache).resolve()
        if not cached.is_relative_to(ROOT) or not cached.is_dir():
            parser.error('seed-cache must be an existing directory inside this repository')
        cls.cache = cached
        cls.seed_env['npm_config_cache'] = str(cached)
    driver = cls('test_vertical_loop_first_upgrade_repair_and_recovery')
    stage, project = cls.stage, cls.project
    print('DEMO_WORKSPACE ' + str(stage), flush=True)
    def write(path, value):
        path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    # Warm both exact dependency sets so subsequent materialization is offline.
    seed = stage / 'seed'
    for deps in (old, new):
        write(seed / 'package.json', {'name': 'demo-seed', 'version': '1.0.0', 'dependencies': deps})
        result = physical._run([cls.npm, 'install', '--no-audit', '--no-fund'], seed, env=cls.seed_env, timeout=1800)
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
    manifest = json.loads((project / 'package.json').read_text(encoding='utf-8'))
    manifest['dependencies'] = old
    # Exercise the real npm.cmd path and shell semantics, not node directly.
    manifest['scripts']['test'] = 'node check.js'
    write(project / 'package.json', manifest)
    # Exercise every dependency, with two additional independently repairable
    # application contracts. These deliberately model adaptation, not claims
    # that these public releases themselves changed those APIs.
    check = project / 'check.js'
    checks = """
for (const [name, versions] of Object.entries(DEMO_ALLOWED)) {
  const version = installedVersion(name);
  if (!versions.includes(version)) throw Error(`unexpected ${name}@${version}`);
  if (typeof require(name) !== 'function') throw Error(`unusable export: ${name}`);
}
for (const [name, field] of [['is-array-buffer', 'buffer'], ['is-callable', 'callable']]) {
  if (DEMO_NEW[name] && installedVersion(name) === DEMO_NEW[name] && cfg[field] !== 'adapted') {
    throw Error(`${name} upgrade requires application adaptation: ${field}`);
  }
}
"""
    pause = "if (process.env.DEPLOOM_DEMO_PAUSE === '1') { require('fs').mkdirSync('.dependency-roadmap', {recursive:true}); require('fs').writeFileSync('.dependency-roadmap/demo-stop-ready','ready'); Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,60000); }\n"
    check.write_text(pause + check.read_text(encoding='utf-8') + '\nconst DEMO_ALLOWED = ' +
                     json.dumps({name: [old[name], new[name]] for name in old}) +
                     ';\nconst DEMO_NEW = ' + json.dumps(new) + ';\n' + checks, encoding='utf-8')
    (project / '.gitignore').write_text('node_modules/\n.dependency-roadmap/\n', encoding='utf-8')
    result = physical._run([cls.npm, 'install', '--package-lock-only', '--no-audit', '--no-fund'], project, env=cls.seed_env)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    git_env = {**os.environ, 'GIT_AUTHOR_NAME': 'Migration Demo', 'GIT_AUTHOR_EMAIL': 'demo@example.test',
               'GIT_COMMITTER_NAME': 'Migration Demo', 'GIT_COMMITTER_EMAIL': 'demo@example.test'}
    for argv in ([cls.git, 'add', '-A'], [cls.git, 'commit', '-qm', f'{args.packages} dependency demo']):
        result = physical._run(argv, project, env=git_env)
        if result.returncode: raise RuntimeError(result.stderr)
    targets = {**new, **{name: '99.99.99' for name in blocked}}
    verify = driver._verify_config_file()
    verify_config = json.loads(verify.read_text(encoding='utf-8'))
    verify_config['commands'] = ['npm run test']
    write(verify, verify_config)
    run = stage / 'run'
    timeline = []
    def cli(*argv):
        result = driver._run_cli(run, *argv)
        last = result['last'] or result['failure'] or {}
        timeline.append({'command': list(argv), 'returncode': result['returncode'], 'result': last})
        write(stage / 'TIMELINE.json', timeline)
        print(json.dumps({'command': argv[0], 'result': last}, ensure_ascii=True), flush=True)
        (stage / 'cli.log').open('a', encoding='utf-8').write(result['stdout'] + result['stderr'])
        return result
    begin = cli('begin', '--project-dir', str(project), '--project-name', 'Iterative.Demo',
                '--targets-file', str(driver._targets_file(targets)), '--verify-config', str(verify),
                '--run-budget-minutes', '120', '--phase-timeout-seconds', '1200')
    driver.assertEqual('begin.done', begin['last']['event'])
    checkpoints, wrong_repair, deferred = [], False, 0
    stopped = None
    repair_cohorts = []
    deferred_bases = set()
    for _ in range(args.packages * 3):
        planned = cli('plan-next')
        if planned['last']['event'] == 'plan-next.no-candidate': break
        candidate = json.loads((run / 'trial/candidate.json').read_text(encoding='utf-8'))
        before = physical.read_runtime_state(run)
        result = cli('materialize', '--timeout-seconds', '1200')
        if result['last']['event'] == 'materialize.failed':
            driver.assertEqual('resolver', result['last']['kind'])
            broken_key = (candidate['baseCheckpointId'], tuple(sorted(candidate['delta']['changed'])))
            driver.assertNotIn(broken_key, deferred_bases, 'Same broken assignment repeated on unchanged base')
            deferred_bases.add(broken_key)
            feedback = driver._write_feedback(run, 'INCONCLUSIVE', reason='Demonstration target version does not exist')
            cli('apply-feedback', '--feedback-file', str(feedback))
            driver.assertEqual(before['activeCheckpointId'], physical.read_runtime_state(run)['activeCheckpointId'])
            deferred += 1
            continue
        driver.assertEqual('materialize.done', result['last']['event'])
        if stopped is None and checkpoints:
            # Stop a real running project command, then resume the SAME trial.
            env = {**cls.seed_env, 'npm_config_offline': 'true', 'DEPLOOM_DEMO_PAUSE': '1'}
            marker = driver._trial_project(run) / '.dependency-roadmap/demo-stop-ready'
            with (stage / 'stopped-precheck.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen([sys.executable, str(ROOT / 'iterative_migration.py'),
                    '--run-dir', str(run), 'precheck'], cwd=ROOT, env=env, stdout=log, stderr=log,
                    start_new_session=os.name != 'nt')
                try:
                    deadline = time.monotonic() + 120
                    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.1)
                    driver.assertTrue(marker.exists(), 'Pause marker was not reached by real project command')
                    driver.assertIsNone(process.poll(), 'Command exited before stop')
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=True, capture_output=True)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=30)
                finally:
                    if process.poll() is None:
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=30)
            after_stop = physical.read_runtime_state(run)
            driver.assertEqual(before['activeCheckpointId'], after_stop['activeCheckpointId'])
            same_trial = json.loads((run / 'trial/candidate.json').read_text(encoding='utf-8'))
            driver.assertEqual(candidate['candidateId'], same_trial['candidateId'])
            stopped = {'runId': before['runId'], 'candidateId': candidate['candidateId'],
                       'checkpointBefore': before['activeCheckpointId'], 'checkpointAfterStop': after_stop['activeCheckpointId']}
            write(stage / 'STOP_RESUME.json', stopped)
        precheck = cli('precheck', '--timeout-seconds', '1200')
        attempt = 0
        if precheck['last']['event'] == 'precheck.repair-required':
            repair_cohorts.append(candidate['delta']['changed'])
            if not wrong_repair:
                driver._scripted_repair(run, api='wrong-api', feature='wrong-feature')
                feedback = driver._write_feedback(run, 'READY_FOR_VERIFY', changed_files=['src/config.js'], attempt_id=0)
                cli('apply-feedback', '--feedback-file', str(feedback))
                bad = cli('verify-exact')
                driver.assertFalse(bad['last']['ok'])
                driver.assertEqual(before['activeCheckpointId'], physical.read_runtime_state(run)['activeCheckpointId'])
                wrong_repair, attempt = True, 1
            driver._repair_for_assignment(run, candidate['fullAssignment'])
            config_path = driver._trial_project(run) / 'src/config.js'
            adaptations = {field: 'adapted' for name, field in
                           [('is-array-buffer', 'buffer'), ('is-callable', 'callable')]
                           if candidate['fullAssignment'].get(name) == new.get(name) and name in new}
            if adaptations:
                with config_path.open('a', encoding='utf-8') as stream:
                    stream.write('\nObject.assign(module.exports, ' + json.dumps(adaptations) + ');\n')
        feedback = driver._write_feedback(run, 'READY_FOR_VERIFY', changed_files=['src/config.js'] if precheck['last']['event'] == 'precheck.repair-required' else [], attempt_id=attempt)
        cli('apply-feedback', '--feedback-file', str(feedback))
        verified = cli('verify-exact')
        driver.assertEqual('checkpoint.accepted', verified['last']['event'])
        state = physical.read_runtime_state(run)
        driver.assertEqual(before['activeCheckpointId'], state['activeCheckpoint']['parentCheckpointId'])
        for name, version in before['activeCheckpoint']['fullAssignment'].items():
            if name not in candidate['delta']['changed']:
                driver.assertEqual(version, state['activeCheckpoint']['fullAssignment'][name])
        checkpoints.append(state['activeCheckpointId'])
    else: raise AssertionError('Planner did not terminate within bounded cohort attempts')
    driver.assertTrue(wrong_repair)
    driver.assertGreaterEqual(deferred, 1)
    driver.assertLessEqual(deferred, 3 * len(blocked))
    accepted_updates = sum(1 for name, version in physical.read_runtime_state(run)["activeCheckpoint"]["fullAssignment"].items() if version != old[name])
    driver.assertEqual(len(old) - len(blocked), accepted_updates)
    driver.assertLess(len(checkpoints), accepted_updates, "Greedy cohorts must reduce physical verification cycles")
    driver.assertIsNotNone(stopped)
    finished = cli('finish')
    driver.assertEqual('finish.done', finished['last']['event'])
    # Each CLI call above is a new process: cumulative state survives restart.
    state = physical.read_runtime_state(run)
    final = state['activeCheckpoint']['fullAssignment']
    for name, version in new.items():
        driver.assertEqual(old[name] if name in blocked else version, final[name])
    for name in ('MIGRATION_REPORT.md', 'DEVELOPER_UPGRADE_GUIDE.md'):
        driver.assertTrue((run / 'reports' / name).is_file())
    write(stage / 'ACCEPTANCE.json', {'directDependencies': len(old), 'acceptedUpdates': accepted_updates, 'acceptedCohorts': len(checkpoints),
          'deferredUpdates': len(blocked), 'repairCohorts': repair_cohorts, 'stopResume': stopped, 'deferredAttempts': deferred, 'wrongRepairRejected': wrong_repair, 'checkpoints': checkpoints,
          'finalRun': state, 'sourceGitStatus': physical._run([cls.git, 'status', '--porcelain'], project).stdout})
    print('DEMO_ACCEPTED ' + str(stage / 'ACCEPTANCE.json'), flush=True)

if __name__ == '__main__':
    main()
