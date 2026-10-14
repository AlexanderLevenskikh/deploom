import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, utimesSync, existsSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const { StorageCleanupController } = await import('../dist-electron/storage-cleanup.js');
const { createIterativeAutopilot } = await import('../dist-electron/iterative-autopilot.js');
const { readFileSync } = await import('node:fs');
const fixture = mkdtempSync(join(tmpdir(), 'deploom-storage-check-'));
const root = join(fixture, 'verification');
const trials = join(root, 'trials');
mkdirSync(trials, { recursive: true });
const old = new Date(Date.now() - 48 * 3600 * 1000);
const stamp = BigInt(old.getTime()) * 1000000n;
function trash(id) {
  const path = join(trials, `.deploom-trial-trash-dependency-flow-baseline-verify-${id}-999-${stamp}`);
  mkdirSync(path); writeFileSync(join(path, 'data.txt'), 'private retired data');
  return path;
}
const garbage = trash('old'); utimesSync(garbage, old, old);
const protectedTree = trash('linked');
const outside = join(fixture, 'protected-source'); mkdirSync(outside);
writeFileSync(join(outside, 'keep.txt'), 'must remain');
symlinkSync(outside, join(protectedTree, 'link'), process.platform === 'win32' ? 'junction' : 'dir');
utimesSync(protectedTree, old, old);
const active = join(trials, 'dependency-flow-baseline-verify-active'); mkdirSync(active);
const checkpoint = join(root, 'checkpoints'); mkdirSync(checkpoint);
const invoke = action => JSON.parse(execFileSync(process.env.PYTHON || 'python', [join(repo, 'verification_storage_maintenance.py'), action], {
  cwd: repo, env: { ...process.env, DEPLOOM_VERIFICATION_ROOT: root }, encoding: 'utf8', timeout: 60000,
}));
try {
  const inspected = invoke('inspect');
  assert.equal(resolve(inspected.root), resolve(root)); assert.equal(inspected.eligible, 2);
  const cleaned = invoke('clean'); assert.equal(cleaned.removed, 1); assert.equal(cleaned.protected, 1);
  assert.equal(existsSync(garbage), false);
  for (const path of [active, checkpoint, protectedTree, join(outside, 'keep.txt')]) assert.ok(existsSync(path), path);
  const second = trash('inventory'); utimesSync(second, old, old);
  const workspace = join(fixture, 'workspace');
  const currentSource = join(workspace, '.dependency-roadmap', 'iterative', 'demo', 'sources', 'C6');
  mkdirSync(currentSource, { recursive: true }); writeFileSync(join(currentSource, 'keep.txt'), 'verified result');
  let activeWork = false;
  const progress = [];
  const controller = new StorageCleanupController(async (action, payload, onLine) => {
    const output = execFileSync(process.env.PYTHON || 'python', [join(repo, 'storage_cleanup_inventory.py'), action], {
      cwd: repo, env: { ...process.env, DEPLOOM_VERIFICATION_ROOT: root }, input: JSON.stringify(payload), encoding: 'utf8', timeout: 60000,
    });
    for (const line of output.split(/\r?\n/)) onLine(line);
    return output;
  }, () => [workspace], () => activeWork);
  await assert.rejects(controller.maintenance('clean', 'early', () => {}), /Inspect storage/);
  const inventory = await controller.maintenance('inspect', 'preview', event => progress.push(event));
  assert.equal(inventory.eligible, 1);
  assert.ok(inventory.items.some(item => item.path === currentSource && !item.eligible));
  assert.ok(progress.every(event => event.operationId === 'preview'));
  activeWork = true;
  await assert.rejects(controller.maintenance('clean', 'blocked', () => {}), /Stop active migrations/);
  assert.ok(existsSync(second)); activeWork = false;
  const final = await controller.maintenance('clean', 'cleanup', event => progress.push(event));
  assert.equal(final.removed, 1); assert.ok(final.reclaimedBytes > 0);
  assert.equal(existsSync(second), false); assert.ok(existsSync(join(currentSource, 'keep.txt')));
  assert.equal(progress.at(-1).percent, 100); assert.equal(progress.at(-1).operationId, 'cleanup');
  await assert.rejects(controller.maintenance('clean', 'repeat', () => {}), /Inspect storage/);
  let release;
  const hold = new StorageCleanupController(async () => new Promise(resolvePromise => { release = resolvePromise }), () => [], () => false);
  const first = hold.maintenance('inspect', 'held', () => {});
  await assert.rejects(hold.maintenance('inspect', 'duplicate', () => {}), /already running/);
  release('STORAGE_RESULT_V1 ' + JSON.stringify(inventory)); await first;
  let finishClean;
  const cleaning = new StorageCleanupController(async action => action === 'inspect' ? 'STORAGE_RESULT_V1 ' + JSON.stringify(inventory) : new Promise(resolvePromise => { finishClean = resolvePromise }), () => [], () => false);
  await cleaning.maintenance('inspect', 'before-clean', () => {});
  const pendingClean = cleaning.maintenance('clean', 'in-progress', () => {});
  assert.throws(() => cleaning.assertMigrationAllowed(), /cleanup is running/);
  finishClean('STORAGE_RESULT_V1 ' + JSON.stringify(final)); await pendingClean;
  assert.doesNotThrow(() => cleaning.assertMigrationAllowed());
  assert.equal(cleaning.status().phase, 'done'); assert.equal(cleaning.status().result.removed, 1);
  let resolveBackground; const states = [];
  const background = new StorageCleanupController(async (action, payload, line) => {
    assert.equal(payload.eligibleOnly, true);
    line('STORAGE_PROGRESS_V1 ' + JSON.stringify({ stage: 'validate', percent: null }));
    return new Promise(done => { resolveBackground = done });
  }, () => [], () => false, state => states.push(state));
  const running = background.maintenance('inspect', 'background', () => {}, true);
  assert.equal(background.status().phase, 'inspect'); assert.equal(background.status().progress.stage, 'validate');
  resolveBackground('STORAGE_RESULT_V1 ' + JSON.stringify(inventory)); await running;
  assert.equal(background.status().phase, 'done'); assert.equal(states.at(-1).automatic, true);
  let automaticActive = true; const calls = [];
  const automatic = new StorageCleanupController(async (action, payload) => {
    calls.push(action); if (action === 'inspect') assert.equal(payload.eligibleOnly, true);
    return 'STORAGE_RESULT_V1 ' + JSON.stringify(action === 'inspect' ? inventory : final);
  }, () => [], () => automaticActive);
  automatic.scheduleAutomatic(); automatic.scheduleAutomatic();
  await new Promise(done => setTimeout(done, 1100)); assert.deepEqual(calls, []);
  automaticActive = false; await new Promise(done => setTimeout(done, 2100));
  assert.deepEqual(calls, ['inspect', 'clean']); assert.equal(automatic.status().phase, 'done');
  // Reproduce cleanup scheduled by an agent finally while Autopilot still owns
  // the run: reject the repair, discard its trial, then materialize a new candidate.
  const scope = { workspaceId: 'test-workspace', projectName: 'demo', autopilot: true };
  const autopilot = createIterativeAutopilot(input => input.projectName);
  const betweenSteps = Promise.withResolvers();
  const resumeDrive = Promise.withResolvers();
  const maintenanceCalls = [];
  const sessionCleanup = new StorageCleanupController(async action => {
    maintenanceCalls.push(action);
    return 'STORAGE_RESULT_V1 ' + JSON.stringify(action === 'inspect' ? inventory : final);
  }, () => [], () => autopilot.hasActiveSessions());
  // Also pin production wiring; per-step locks alone leave the inter-step gap open.
  assert.ok(readFileSync(join(repo, 'desktop/electron/main.ts'), 'utf8').includes('() => jobs.size > 0 || iterativeStepInFlight.size > 0 || iterativeAutopilot.hasActiveSessions()'));
  await sessionCleanup.maintenance('inspect', 'session-preview', () => {});
  maintenanceCalls.length = 0;
  let drives = 0;
  const drive = autopilot.register('drive', async (_event, input) => {
    sessionCleanup.assertMigrationAllowed();
    if (++drives === 1) return { ok: true, stopped: 'agent-gate' };
    assert.equal(input.discardCandidate, true);
    // This await represents a status read before the next Python step lock.
    betweenSteps.resolve();
    await resumeDrive.promise;
    sessionCleanup.assertMigrationAllowed();
    return { ok: true, stopped: 'finished' };
  });
  autopilot.register('status', async () => ({ ok: true, decision: 'repair' }));
  autopilot.register('agent', async () => {
    sessionCleanup.scheduleAutomatic();
    return { ok: false, error: 'FORBIDDEN_MUTATION: modified:tool-config' };
  });
  const migration = drive(undefined, scope);
  await betweenSteps.promise;
  assert.equal(autopilot.hasActiveSessions(), true);
  await assert.rejects(sessionCleanup.maintenance('clean', 'during-session', () => {}), /Stop active migrations/);
  await new Promise(done => setTimeout(done, 1100));
  assert.deepEqual(maintenanceCalls, [], 'automatic cleanup must wait between steps');
  resumeDrive.resolve();
  assert.equal((await migration).autopilot.stopped, 'finished');
  assert.equal(drives, 2);
  assert.equal(autopilot.hasActiveSessions(), false);
  await new Promise(done => setTimeout(done, 2100));
  assert.deepEqual(maintenanceCalls, ['inspect', 'clean'], 'cleanup resumes after session completion');

  // Pausing disables continuation but the current handler still owns its bytes.
  const parked = Promise.withResolvers();
  const releaseParked = Promise.withResolvers();
  const manual = autopilot.register('begin', async () => { parked.resolve(); await releaseParked.promise; throw new Error('test failure'); });
  const manualRun = manual(undefined, scope);
  await parked.promise;
  autopilot.setEnabled(scope, false);
  assert.equal(autopilot.hasActiveSessions(), true);
  releaseParked.resolve();
  assert.equal((await manualRun).ok, false);
  assert.equal(autopilot.hasActiveSessions(), false);
  // Commands stay pending until the child acknowledges them. Neither pause nor
  // cancel may release the migration guard while the worker still owns files.
  let controlLine, finishControlled, controlsHandle;
  const modes = [];
  const controlled = new StorageCleanupController(async (action, _payload, line, controls) => {
    if (action === 'inspect') return 'STORAGE_RESULT_V1 ' + JSON.stringify(inventory);
    controlLine = line; controlsHandle = controls;
    controls.bind(mode => modes.push(mode));
    return new Promise(done => { finishControlled = done });
  }, () => [], () => false);
  await controlled.maintenance('inspect', 'control-preview', () => {});
  const controlledRun = controlled.maintenance('clean', 'controlled-clean', () => {});
  assert.throws(() => controlled.control('pause', 'old-operation'), /no longer active/);
  assert.equal(controlled.control('pause', 'controlled-clean').control, 'pausing');
  controlLine('STORAGE_CONTROL_V1 ' + JSON.stringify({ operationId: 'old-operation', state: 'paused' }));
  assert.equal(controlled.status().control, 'pausing');
  controlLine('STORAGE_CONTROL_V1 ' + JSON.stringify({ operationId: 'controlled-clean', state: 'paused' }));
  assert.equal(controlled.status().control, 'paused');
  assert.throws(() => controlled.assertMigrationAllowed(), /cleanup is running/);
  assert.equal(controlled.control('resume', 'controlled-clean').control, 'resuming');
  controlLine('STORAGE_CONTROL_V1 ' + JSON.stringify({ operationId: 'controlled-clean', state: 'running' }));
  assert.equal(controlled.status().control, undefined);
  assert.equal(controlled.control('cancel', 'controlled-clean').control, 'canceling');
  controlled.control('resume', 'controlled-clean');
  assert.equal(controlsHandle.mode(), 'cancel', 'cancel cannot be undone by a late resume');
  assert.throws(() => controlled.assertMigrationAllowed(), /cleanup is running/);
  finishControlled('STORAGE_RESULT_V1 ' + JSON.stringify({ ...final, canceled: true, partial: true }));
  await controlledRun;
  assert.equal(controlled.status().phase, 'canceled');
  assert.doesNotThrow(() => controlled.assertMigrationAllowed());
  assert.deepEqual(modes, ['run', 'pause', 'run', 'cancel']);
  assert.throws(() => controlled.control('pause', 'controlled-clean'), /no longer active/);
  await assert.rejects(controlled.maintenance('clean', 'reuse-canceled', () => {}), /Inspect storage/);
  let inspectFails = false;
  const invalidation = new StorageCleanupController(async () => { if (inspectFails) throw new Error('read failed'); return 'STORAGE_RESULT_V1 ' + JSON.stringify(inventory) }, () => [], () => false);
  await invalidation.maintenance('inspect', 'old-preview', () => {}); inspectFails = true;
  await assert.rejects(invalidation.maintenance('inspect', 'failed-preview', () => {}), /read failed/);
  await assert.rejects(invalidation.maintenance('clean', 'stale-preview', () => {}), /Inspect storage/);
  console.log('Storage maintenance: real inventory CLI, preview binding, progress, ownership, active-job gate and junction/source preservation PASS');
} finally {
  assert.equal(dirname(fixture), resolve(tmpdir()));
  assert.ok(fixture.startsWith(join(resolve(tmpdir()), 'deploom-storage-check-')));
  rmSync(fixture, { recursive: true, force: true });
}
