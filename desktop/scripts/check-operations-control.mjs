import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join, resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import { StorageCleanupController } from '../dist-electron/storage-cleanup.js';
import { iterativeActivity } from '../dist-electron/iterative-activity.js';
import { defenderExclusionTarget, defenderExclusionCommand } from '../dist-electron/defender-exclusion.js';
const fixture = mkdtempSync(join(tmpdir(), 'deploom-operations-'));
const wait = ms => new Promise(done => setTimeout(done, ms));
try {
  const workspace = join(fixture, "workspace ' $ spaced"); mkdirSync(workspace);
  assert.throws(() => defenderExclusionTarget(workspace), /DEFENDER_PATH_INVALID/);
  mkdirSync(join(workspace, '.dependency-roadmap'));
  const target = defenderExclusionTarget(workspace);
  const command = defenderExclusionCommand(target);
  assert.ok(command.includes('-Verb RunAs -WindowStyle Hidden'));
  const encoded = command.match(/-EncodedCommand ([A-Za-z0-9+/=]+)/)[1];
  const elevated = Buffer.from(encoded, 'base64').toString('utf16le');
  assert.ok(elevated.includes("$target='" + target.replace(/'/g, "''") + "'"));
  assert.ok(elevated.includes('Add-MpPreference -ExclusionPath $target'));
  assert.ok(elevated.includes('(Get-MpPreference).ExclusionPath'));
  assert.ok(!elevated.includes('Set-MpPreference') && !elevated.includes('-ExclusionProcess'));
  const linked = join(fixture, 'linked'); mkdirSync(linked);
  symlinkSync(target, join(linked, '.dependency-roadmap'), process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => defenderExclusionTarget(linked), /DEFENDER_PATH_INVALID/);

  const attempt = { attemptId: 'fixture', status: 'running', stage: 'drive' };
  const validation = iterativeActivity({ ...attempt, phase: 'durable source snapshot validation: files=15000, bytes=1073741824' }, true, 'en');
  const copying = iterativeActivity({ ...attempt, phase: 'Preparing isolated source snapshot copy' }, true, 'en');
  const installing = iterativeActivity({ ...attempt, phase: 'iterative migration exact materialization: running; elapsed=15s' }, true, 'en');
  assert.notEqual(validation.title, copying.title);
  assert.notEqual(copying.title, installing.title);
  assert.ok(validation.detail.includes('15,000'));
  assert.equal(installing.percent, undefined, 'Install duration does not prove completion percentage');
  let inspectCalls = 0;
  const storage = new StorageCleanupController(async (_action, _payload, _line, controls) => {
    inspectCalls++;
    while (controls.mode() !== 'cancel') await wait(5);
    return 'STORAGE_RESULT_V1 ' + JSON.stringify({ canceled: true, eligible: 0 });
  }, () => [], () => false);
  const cleaning = storage.maintenance('inspect', 'test-cancel', () => {});
  const handlers = new Map();
  const jobs = new Map([['job', { id: 'job', workspace: { name: 'workspace' }, projectName: 'UI', action: 'build', child: {}, parallelChildren: new Set() }]]);
  let canceledScopes = 0, activeSession = true, killed = 0;
  const inFlight = new Set(['ws:App']);
  const auditJobs = new Map([['audit-dir', { finishedAt: undefined }]]);
  const context = {
    ipcMain: { handle: (name, handler) => handlers.set(name, handler) },
    jobs, iterativeStepInFlight: inFlight,
    iterativeAutopilot: { hasSession: () => activeSession, hasActiveSessions: () => activeSession, cancelAll: () => { activeSession = false } },
    loadState: () => ({ workspaces: [{ id: 'ws', name: 'ws' }] }),
    readProjects: () => [{ name: 'App' }], stepLockKey: (ws, project) => ws + ':' + project,
    iterativeTaskRunDir: () => 'migration-dir', readAttempt: () => ({ status: 'running' }),
    cancelIterativeOperation: async () => { canceledScopes++; setTimeout(() => inFlight.clear(), 20) },
    killProcessTree: () => { killed++; setTimeout(() => jobs.clear(), 20) },
    initialAuditJobs: auditJobs, auditJson: () => ({ operationId: 'audit' }), canceledAudits: new Set(),
    auditChildren: new Set([{}]), iterativeStreamPlatform: { killProcessTree: () => { killed++; setTimeout(() => auditJobs.clear(), 20) } },
    storageCleanup: storage, basename, join, Date, Promise, setTimeout,
    updateRestartBlockers: undefined, updateRestartBlocked: () => context.updateRestartBlockers().length > 0,
    operationsStopping: false,
  };
  const source = readFileSync(new URL('../dist-electron/main.js', import.meta.url), 'utf8');
  const start = source.indexOf('    updateRestartBlockers = () => {');
  const end = source.indexOf("    ipcMain.handle('flow:storage-status'", start);
  assert.ok(start >= 0 && end > start);
  runInNewContext(source.slice(start, end), context);
  assert.deepEqual(Array.from(handlers.get('flow:update-operations')()), ['UI: build', 'App: migration / agent', 'audit-dir: audit', 'Storage: inspect']);
  const first = handlers.get('flow:stop-all-operations')();
  assert.equal(context.operationsStopping, true);
  assert.equal(handlers.get('flow:stop-all-operations')(), first, 'duplicate clicks share stop ownership');
  const result = await first;
  await cleaning;
  assert.equal(result.remaining.length, 0);
  assert.equal(context.operationsStopping, false);
  assert.equal(canceledScopes, 1);
  assert.equal(killed, 2);
  assert.ok(context.canceledAudits.has('audit'));
  storage.scheduleAutomatic();
  await wait(1150);
  assert.equal(inspectCalls, 1, 'migration finalizers must not restart cleanup after stop all');
  assert.equal(storage.status().phase, 'canceled');
  console.log('operations-control: OK (all owners drain; cleanup stays canceled; Defender path/quoting/UAC contract)');
} finally {
  assert.ok(resolve(fixture).startsWith(resolve(tmpdir()) + '\\') || resolve(fixture).startsWith(resolve(tmpdir()) + '/'));
  rmSync(fixture, { recursive: true, force: true });
}
