import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, utimesSync, existsSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const { StorageCleanupController } = await import('../dist-electron/storage-cleanup.js');
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
