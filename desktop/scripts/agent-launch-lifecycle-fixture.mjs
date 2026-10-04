// Runs the production agent handler with real streamed child processes and
// durable lease/journal files. Provider and Python endpoints are fixture-only;
// no account, project migration or model request is contacted.
import assert from 'node:assert/strict';
import ts from 'typescript';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import * as agents from '../dist-electron/iterative-agent.js';
import * as attempts from '../dist-electron/iterative-attempt.js';
import * as commands from '../dist-electron/agent-command.js';
import * as errors from '../dist-electron/agent-launch-errors.js';
import { extractAgentSessionId } from '../dist-electron/agent-session.js';
import { spawnIterativeStreamed } from '../dist-electron/iterative-stream.js';

const desktop = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(desktop, 'electron/main.ts'), 'utf8');
const start = source.indexOf('  const runIterativeAgent = async');
const end = source.indexOf("  ipcMain.handle('flow:iterative:agent'", start);
assert.ok(start >= 0 && end > start);
const js = ts.transpileModule(source.slice(start, end), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
const handler = new Function('bindings', `with(bindings) { ${js}; return runIterativeAgent; }`);

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'agent-launch-lifecycle-'));
  const runDir = join(root, 'run');
  const projectPath = join(root, 'trial');
  mkdirSync(projectPath, { recursive: true });
  mkdirSync(runDir);
  writeFileSync(join(runDir, 'run.json'), '{}');
  writeFileSync(join(projectPath, 'input.js'), 'original');
  writeFileSync(join(projectPath, 'package.json'), '{"name":"fixture","private":true}');
  const workspace = { id: 'fixture-workspace', path: root, agent: 'opencode', agentModel: 'fixture/model' };
  const project = { name: 'fixture' };
  const candidate = { runId: 'run', candidateId: 'candidate', baseCheckpointId: 'C0', attemptId: 2, fullAssignment: {}, materializationRefs: { workspaceRoot: projectPath, projectRelative: '.' } };
  const payload = { run: { runId: 'run', phase: 'REPAIRING' }, candidate, config: {}, openRepairRequests: [{ requestId: 'request', reason: 'fixture repair' }] };
  const launches = [], waits = [], databases = [];
  let childSource = '';
  let beforeChild;
  let serverError;
  const bindings = {
    ...agents, ...attempts, ...commands, ...errors,
    existsSync, join, readFileSync, writeFileSync, extractAgentSessionId,
    loadState: () => ({}), findWorkspace: () => workspace, findProject: () => project,
    iterativeTaskRunDir: () => runDir, iterativeStepInFlight: new Set(), stepLockKey: () => 'fixture',
    publishIterativeAttempt: () => {}, ensureAttemptRecord: () => { if (!attempts.readAttempt(runDir)) attempts.startAttempt(runDir, project.name, 'none'); },
    isPidAlive: () => false, resolveExecutable: () => 'python', bundledToolDir: () => '.',
    refreshIterativeTaskArtifact: async () => ({ status: 'ok' }),
    iterativeStatusInvocation: () => ({args: ['status']}),
    spawnCapture: async () => ({code: 0, stdout: '{}', stderr: ''}),
    parseIterativeStatusPayload: () => payload, readIterativeStatus: () => payload,
    decideNextStep: () => ({step: 'agent', phase: 'REPAIRING'}),
    repairPromptTaskText: () => '', taskDispatchable: () => false,
    app: {getPath: () => root}, randomUUID: () => 'fixture-db',
    scheduleAgentWaitRetry: (_scope, retryAt) => waits.push(retryAt), cancelPendingAgentWait: () => { waits.length = 0; },
    agentLaunchWaitDetail: (diag) => diag.detail,
    startStandaloneOpenCodeServer: async (_cwd, databasePath) => {
      databases.push(databasePath);
      if (serverError) throw Error(serverError);
      return {url: 'http://fixture.invalid', databasePath, stop: async () => {}};
    },
    iterativeStreamIo: () => ({cancelRequested: () => attempts.readAttempt(runDir)?.cancelRequested === true, recordLine: (dir, line) => attempts.recordAttemptLog(dir, line), trimLog: () => {}}),
    iterativeStreamPlatform: {}, commandEnvironment: env => env, openCodeDatabaseEnv: env => env,
    spawnIterativeStreamed: async (dir, command, args, cwd, timeout, io, _platform, onEvent, options) => {
      launches.push({command, args});
      const script = childSource;
      beforeChild?.();
      return spawnIterativeStreamed(dir, process.execPath, ['-e', script], cwd, timeout, io, {
        processTreeDetached: () => false, commandEnvironment: env => env,
        resolveSpawnInvocation: (cmd, argv) => ({command: cmd, args: argv}),
        killProcessTree: child => child.kill(), decodeChunk: chunk => chunk.toString('utf8'),
      }, onEvent, options);
    },
  };
  const run = handler(bindings);
  return { root, runDir, projectPath, bindings, run, launches, waits, databases,
    setChild: script => { childSource = script; }, beforeChild: fn => { beforeChild = fn; },
    setServerError: value => { serverError = value; },
    lease: () => agents.readAgentLease(agents.agentLeaseFile(runDir)),
    expireWait: () => agents.writeAgentLease(agents.agentLeaseFile(runDir), {...agents.readAgentLease(agents.agentLeaseFile(runDir)), retryAt: Date.now() - 1}),
  };
}

// Exit-zero error envelopes wait; clicking while waiting releases the guard.
// A restart retries the SAME provider session, DB and last repair attempt.
{
  const f = fixture();
  f.setChild(`console.log(JSON.stringify({type:'text',sessionID:'ses-real',part:{text:'started'}})); console.log(JSON.stringify({type:'error',error:{data:{message:'429 Too Many Requests',headers:{'retry-after':'30'}}}}));`);
  const limited = await f.run({projectName: 'fixture'});
  assert.equal(limited.waiting, true);
  assert.equal(f.lease().sessionId, 'ses-real');
  assert.equal(f.lease().attemptId, 2);
  assert.equal(f.lease().retryAfterSeconds, 30);
  assert.equal(f.lease().launchDiagnostics.code, 0);
  assert.equal(f.lease().launchDiagnostics.kind, 'rate-limited');
  assert.equal(f.bindings.iterativeStepInFlight.size, 0);
  await f.run({projectName: 'fixture'});
  assert.equal(f.launches.length, 1);
  assert.equal(f.bindings.iterativeStepInFlight.size, 0);
  f.expireWait();
  f.bindings.iterativeStepInFlight = new Set(); // new Desktop process guard
  f.setChild(`require('fs').writeFileSync('input.js','repaired');console.log('FEEDBACK_KIND: READY_FOR_VERIFY');`);
  const resumed = await f.run({projectName: 'fixture'});
  assert.equal(resumed.ok, true);
  assert.ok(f.launches[1].args.includes('--session'));
  assert.ok(f.launches[1].args.includes('ses-real'));
  assert.equal(f.databases[1], f.databases[0]);
  assert.equal(f.lease(), undefined);
  assert.equal(f.bindings.iterativeStepInFlight.size, 0);
  const feedback = JSON.parse(readFileSync(join(f.runDir,'trial','agent-feedback.json'),'utf8'));
  assert.equal(feedback.attemptId, 2);
  assert.deepEqual(feedback.changedFiles, ['input.js']);
}

// User Stop is distinct from timeout: no automatic relaunch, lease or wait.
{
  const f = fixture();
  f.setChild(`console.log(JSON.stringify({type:'text',sessionID:'ses-cancel',part:{text:'started'}}));setTimeout(()=>{},60000);`);
  f.beforeChild(() => setTimeout(() => attempts.requestCancel(f.runDir), 100));
  const stopped = await f.run({projectName: 'fixture'});
  assert.equal(stopped.error, 'AGENT_CANCELED');
  assert.equal(attempts.readAttempt(f.runDir).status, 'canceled');
  assert.equal(f.lease(), undefined);
  assert.equal(f.waits.length, 0);
  assert.equal(f.bindings.iterativeStepInFlight.size, 0);
}

// A server startup outage also waits; missing session starts fresh after reset.
{
  const f = fixture();
  f.setServerError('OPENCODE_SERVER_START_FAILED: temporarily unavailable');
  const outage = await f.run({projectName: 'fixture'});
  assert.equal(outage.waiting, true);
  assert.equal(f.lease().sessionId, '');
  assert.equal(f.launches.length, 0);
  f.expireWait(); f.setServerError(undefined);
  f.setChild(`console.log(JSON.stringify({type:'error',error:{data:{message:'401 Unauthorized'}}}));`);
  const permanent = await f.run({projectName: 'fixture'});
  assert.equal(permanent.waiting, undefined);
  assert.match(permanent.error, /auth-config/);
  assert.equal(f.launches[0].args.includes('--session'), false);
  assert.equal(f.bindings.iterativeStepInFlight.size, 0);
}

// Exhausted finite budget returns early, releases its lock, and spends no
// candidate repair attempt (the durable candidate is still attempt 2).
{
  const f = fixture();
  agents.writeAgentLease(agents.agentLeaseFile(f.runDir), {schemaVersion:1,sessionId:'',provider:'opencode',databasePath:'',runId:'run',candidateId:'candidate',attemptId:2,pid:0,startedAt:new Date().toISOString(),waiting:true,waitKind:'unknown',retryAt:0,launchAttempts:3});
  const exhausted = await f.run({projectName:'fixture'});
  assert.match(exhausted.error, /BUDGET_EXHAUSTED/);
  assert.equal(f.launches.length, 0);
  assert.equal(f.bindings.iterativeStepInFlight.size, 0);
}
console.log('agent-launch-lifecycle: production handler + real child wait/resume/cancel OK');
