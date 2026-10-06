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
import { createIterativeAutopilot } from '../dist-electron/iterative-autopilot.js';
import { extractAgentSessionId } from '../dist-electron/agent-session.js';
import { spawnIterativeStreamed } from '../dist-electron/iterative-stream.js';

const desktop = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(desktop, 'electron/main.ts'), 'utf8');
const start = source.indexOf('  const runIterativeAgent = async');
const end = source.indexOf("  ipcMain.handle('flow:iterative:agent'", start);
assert.ok(start >= 0 && end > start);
const js = ts.transpileModule(source.slice(start, end), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
const handler = new Function('bindings', `with(bindings) { ${js}; return {run: runIterativeAgent, autonomous: runIterativeAgentWithAutopilot}; }`);
const retryStart = source.indexOf('  async function retryIterativeAgent(');
const retryEnd = source.indexOf('  function scheduleAgentWaitRetry(', retryStart);
assert.ok(retryStart >= 0 && retryEnd > retryStart);
const retryJs = ts.transpileModule(source.slice(retryStart,retryEnd), {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
const retryHandler = new Function('bindings', `with(bindings) { ${retryJs}; return retryIterativeAgent; }`);

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
  const iterativeAutopilot = createIterativeAutopilot(() => 'fixture');
  let drives = 0;
  iterativeAutopilot.register('drive', async () => { drives++; return {ok:true, stopped:'finished'}; });
  let childSource = '';
  let beforeChild;
  let serverError;
  const bindings = {
    ...agents, ...attempts, ...commands, ...errors, iterativeAutopilot,
    existsSync, join, readFileSync, writeFileSync, extractAgentSessionId,
    loadState: () => ({}), findWorkspace: () => workspace, autopilotWorkspace: () => workspace, findProject: () => project,
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
      launches.push({command, args, timeout});
      const script = typeof childSource === 'function' ? childSource(launches.length) : childSource;
      beforeChild?.();
      return spawnIterativeStreamed(dir, process.execPath, ['-e', script], cwd, timeout, io, {
        processTreeDetached: () => false, commandEnvironment: env => env,
        resolveSpawnInvocation: (cmd, argv) => ({command: cmd, args: argv}),
        killProcessTree: child => child.kill(), decodeChunk: chunk => chunk.toString('utf8'),
      }, onEvent, options);
    },
  };
  const {run, autonomous} = handler(bindings);
  const retry = retryHandler({...bindings, runIterativeAgentWithAutopilot: autonomous});
  return { retry, root, runDir, projectPath, bindings, run, autonomous, launches, waits, databases,
    drives: () => drives,
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
// Autopilot waits beyond the manual budget and then continues the migration.
{
  const f = fixture();
  f.bindings.agentLaunchRetryDelayMs = () => 10;
  f.setChild(n => n <= 6
    ? `console.log(JSON.stringify({type:'error',sessionID:'ses-timeout',error:{message:'APITimeoutError: model request timed out'}}));`
    : `require('fs').writeFileSync('input.js','repaired');console.log('FEEDBACK_KIND: READY_FOR_VERIFY');`);
  const result = await f.autonomous(undefined, {projectName:'fixture',autopilot:true});
  assert.equal(result.autopilot.stopped, 'finished', JSON.stringify(result));
  assert.equal(f.launches.length, 7);
  assert.equal(f.drives(), 1);
  assert.deepEqual(f.launches.slice(0,3).map(x => x.timeout), [900_000,1800_000,3600_000]);
  assert.ok(f.launches.slice(1).every(x => x.args.includes('ses-timeout')));
  assert.equal(f.lease(), undefined);
}
// A parked autonomous wait survives a Desktop restart beyond five launches.
{
  const f = fixture();
  agents.writeTrialBaseline(f.projectPath, agents.trialBaselineFile(f.runDir));
  agents.writeAgentLease(agents.agentLeaseFile(f.runDir), {schemaVersion:1,sessionId:'ses-restart',provider:'opencode',databasePath:'saved.db',runId:'run',candidateId:'candidate',attemptId:2,pid:0,startedAt:new Date().toISOString(),waiting:true,waitKind:'temporary',retryAt:0,launchAttempts:8,autopilot:true});
  f.setChild(`require('fs').writeFileSync('input.js','repaired');console.log('FEEDBACK_KIND: READY_FOR_VERIFY');`);
  await f.retry({projectName:'fixture'});
  assert.equal(f.lease(),undefined);
  assert.equal(f.databases[0],'saved.db');
  assert.ok(f.launches[0].args.includes('ses-restart'));
  assert.equal(f.drives(),1);
}
// The durable timer cannot race a live coordinator into a duplicate child.
{
  const f = fixture();
  f.bindings.agentLaunchRetryDelayMs = () => 150;
  f.setChild(n => n === 1 ? `console.log(JSON.stringify({type:'error',error:{message:'503 model unavailable'}}));` : `require('fs').writeFileSync('input.js','repaired');console.log('FEEDBACK_KIND: READY_FOR_VERIFY');`);
  const pending = f.autonomous(undefined,{projectName:'fixture',autopilot:true});
  const deadline = Date.now()+5000;
  while (!f.lease()?.waiting && Date.now()<deadline) await new Promise(resolve=>setTimeout(resolve,5));
  assert.equal(f.lease()?.waiting,true);
  f.expireWait();
  await f.retry({projectName:'fixture'});
  assert.equal(f.launches.length,1);
  assert.equal((await pending).autopilot.stopped,'finished');
  assert.equal(f.launches.length,2);
}
// Disabling autopilot during the child must persist manual retry policy.
{
  const f = fixture();
  f.beforeChild(()=>setTimeout(()=>f.bindings.iterativeAutopilot.setEnabled({projectName:'fixture'},false),10));
  f.setChild(`setTimeout(()=>console.log(JSON.stringify({type:'error',error:{message:'503 unavailable'}})),100);`);
  const result = await f.autonomous(undefined,{projectName:'fixture',autopilot:true});
  assert.equal(result.autopilot.stopped,'paused');
  assert.equal(f.lease()?.waiting,true);
  assert.notEqual(f.lease()?.autopilot,true);
  assert.equal(f.launches.length,1);
  assert.equal(f.drives(),0);
}
// A pre-dispatch status timeout retries without buying a new paid attempt.
{
  const f = fixture();
  const status = f.bindings.spawnCapture;
  let reads = 0;
  f.bindings.spawnCapture = async (...args) => ++reads === 1 ? {code:-1,timedOut:true,stdout:'',stderr:''} : status(...args);
  f.setChild(`require('fs').writeFileSync('input.js','repaired');console.log('FEEDBACK_KIND: READY_FOR_VERIFY');`);
  const result = await f.autonomous(undefined,{projectName:'fixture',autopilot:true});
  assert.equal(result.autopilot.stopped,'finished',JSON.stringify(result));
  assert.equal(f.launches.length,1); assert.equal(f.drives(),1);
}
// Another handler must preserve the live child's lease and refuse duplicates.
for (const autopilot of [false,true]) {
  const f = fixture();
  agents.writeAgentLease(agents.agentLeaseFile(f.runDir), {schemaVersion:1,sessionId:'ses-alive',provider:'opencode',databasePath:'saved.db',runId:'run',candidateId:'candidate',attemptId:2,pid:123,childPid:456,startedAt:new Date(Date.now()-3*60*60*1000).toISOString()});
  f.bindings.isPidAlive = () => true;
  f.bindings.iterativeAutopilot.isEnabled = () => autopilot;
  const result = await f.run({projectName:'fixture'});
  assert.equal(result.ok,false); assert.equal(result.waiting===true,autopilot);
  assert.match(result.error,/AGENT_IN_PROGRESS/);
  assert.equal(f.lease()?.sessionId,'ses-alive'); assert.equal(f.launches.length,0);
}
// A recovered request error must not restart a completed paid repair, even
// when verbose tool output has pushed the earlier error out of the capture tail.
for (const verbose of [false,true]) {
  const f=fixture();
  f.setChild(`
    console.log(JSON.stringify({type:'error',sessionID:'ses-recovered',error:{message:'unclassified request failure'}}));
    if (${verbose}) for(let n=0;n<24;n++) console.log(JSON.stringify({type:'tool_use',part:{type:'tool',output:'x'.repeat(65536)}}));
    require('fs').writeFileSync('input.js','repaired');
    console.log(JSON.stringify({type:'text',sessionID:'ses-recovered',part:{text:'FEEDBACK_KIND: READY_FOR_VERIFY'}}));
    console.log(JSON.stringify({type:'step_finish',sessionID:'ses-recovered',part:{type:'step-finish',reason:'stop',sessionID:'ses-recovered',messageID:'message'}}));
  `);
  const result=await f.autonomous(undefined,{projectName:'fixture',autopilot:true});
  assert.equal(result.autopilot.stopped,'finished',JSON.stringify(result));
  assert.equal(f.launches.length,1);assert.equal(f.drives(),1);
  assert.equal(f.waits.length,0);assert.equal(f.lease(),undefined);
}
console.log('agent-launch-lifecycle: production handler + real child wait/resume/cancel/autopilot/recovered error OK');
