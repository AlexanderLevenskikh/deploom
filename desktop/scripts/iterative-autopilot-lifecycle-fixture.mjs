// Exercise the production drive and startup recovery with real Node children,
// real journal/continuation files and fixture-only Python endpoints.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import ts from 'typescript';
import { createIterativeAutopilot } from '../dist-electron/iterative-autopilot.js';
import { liveRunWriterPid, readAutopilotState, writeAutopilotState } from '../dist-electron/iterative-autopilot-state.js';
import * as attempts from '../dist-electron/iterative-attempt.js';
import * as runner from '../dist-electron/iterative-runner.js';
import { parseIterativeFailure } from '../dist-electron/iterative-scenario.js';
import { existsSync } from 'node:fs';

const source = readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8');
const start = source.indexOf('  const runIterativeDriveWithAutopilot =');
const end = source.indexOf("  ipcMain.handle('flow:iterative:drive'", start);
assert.ok(start >= 0 && end > start);
const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
const productionDrive = new Function('bindings', `with(bindings) { ${compile(source.slice(start,end))}; return runIterativeDriveWithAutopilot; }`);
const recoveryStart = source.indexOf('  queueMicrotask(() => {', source.indexOf("  ipcMain.handle('flow:iterative:agent'"));
const recoveryEnd = source.indexOf('\n  })', recoveryStart) + '\n  })'.length;
assert.ok(recoveryStart >= 0 && recoveryEnd > recoveryStart);
const productionRecovery = new Function('bindings', `with(bindings) { ${compile(source.slice(recoveryStart,recoveryEnd))} }`);

async function child(script, timeout = 10_000) {
  return new Promise((resolve, reject) => {
    const process = spawn(globalThis.process.execPath, ['-e', script], {windowsHide:true});
    let stdout = '', stderr = '', timedOut = false;
    process.stdout.on('data', data => { stdout += data; });
    process.stderr.on('data', data => { stderr += data; });
    process.on('error', reject);
    const timer = setTimeout(() => { timedOut = true; process.kill(); }, timeout);
    process.on('close', code => { clearTimeout(timer); resolve({code:timedOut ? -1 : code,stdout,stderr,timedOut}); });
  });
}
function fixture(failures = 0, critical = false, infra = false) {
  const runDir = mkdtempSync(join(tmpdir(), 'iterative-autopilot-lifecycle-'));
  const workspace = {id:'w',path:runDir}, project = {name:'demo'};
  const scope = {workspaceId:'w',projectName:'demo',autopilot:true};
  const run = {runId:'run-1',phase:infra ? 'READY' : 'TERMINAL', ...(infra ? {infraBlocked:{reason:'verification retries exhausted'}} : {terminal:'COMPLETE'})};
  const payload = {run};
  writeFileSync(join(runDir,'run.json'),JSON.stringify(run));
  attempts.startAttempt(runDir,project.name,'none',undefined,0,workspace.id);
  let reads = 0, mutations = 0;
  const options = {
    setEnabled: (_scope, enabled) => {
      const previous = readAutopilotState(runDir);
      writeAutopilotState(runDir,enabled,enabled && previous?.enabled === false ? undefined : previous?.retry);
    },
    readRetry: () => readAutopilotState(runDir)?.retry,
    writeRetry: (_scope, retry) => writeAutopilotState(runDir,true,retry),
    retryDelayMs: () => 1,
  };
  let auto;
  const bindings = {
    ...attempts,...runner,parseIterativeFailure, existsSync,join,writeFileSync,
    loadState: () => ({workspaces:[workspace]}),findWorkspace: () => workspace,findProject: () => project,
    iterativeTaskRunDir: () => runDir,readProjects: () => [project],stepLockKey: () => 'w:demo',
    iterativeStepInFlight: new Set(),publishIterativeAttempt: () => {},resolveExecutable: () => 'fixture-python',bundledToolDir: () => runDir,
    refreshIterativeTaskArtifact: async () => ({status:'current'}),
    spawnCapture: async () => {
      reads++;
      if (reads <= failures) return critical ? child("process.stderr.write('SOURCE_IDENTITY_MISMATCH'); process.exit(1)") : child('setTimeout(() => {}, 10000)',10);
      return child(`process.stdout.write(${JSON.stringify(JSON.stringify(payload))})`);
    },
    spawnIterativeStreamed: async () => { mutations++; return child('process.exit(0)'); },
    iterativeStreamIo: () => ({}),iterativeStreamPlatform: {},
  };
  const newOwner = () => { auto = createIterativeAutopilot(()=>'w:demo',options); bindings.iterativeAutopilot = auto; return productionDrive(bindings); };
  return {runDir,scope,bindings,drive:newOwner(),newOwner,auto:()=>auto,reads:()=>reads,mutations:()=>mutations,options};
}

for (const failures of [1,3]) {
  const f=fixture(failures);
  const result=await f.drive(undefined,f.scope);
  assert.equal(result.autopilot.stopped,'finished',JSON.stringify(result));
  assert.equal(f.reads(),failures+1); assert.equal(f.mutations(),1);
  assert.equal(readAutopilotState(f.runDir).enabled,false);
}
{
  const f=fixture(10), result=await f.drive(undefined,f.scope);
  assert.match(result.autopilot.error,/AUTOPILOT_RETRY_EXHAUSTED/);
  assert.equal(result.ok,false); assert.equal(f.reads(),4); assert.equal(f.mutations(),0);
  assert.equal(readAutopilotState(f.runDir).enabled,false);
}
{
  const f=fixture(); const read=f.bindings.spawnCapture; let calls=0;
  f.bindings.spawnCapture=async (...args)=> ++calls<=2 ? child("process.stdout.write('partial status')") : read(...args);
  assert.equal((await f.drive(undefined,f.scope)).autopilot.stopped,'finished');
  assert.equal(calls,3); assert.equal(f.mutations(),1);
}
for (const code of ['TRIAL_LOCKED','INVALID_INPUT']) {
  const f=fixture(); const mutate=f.bindings.spawnIterativeStreamed; let calls=0;
  const envelope='ITERATIVE_MIGRATION_FAILURE_V1 '+JSON.stringify({code,summary:'RUN_LOCK_BUSY: fixture writer'});
  f.bindings.spawnIterativeStreamed=async(...args)=>++calls===1 ? child(`process.stdout.write(${JSON.stringify(envelope)});process.exit(5)`) : mutate(...args);
  const result=await f.drive(undefined,f.scope);
  assert.equal(result.autopilot.stopped,code==='TRIAL_LOCKED'?'finished':'error');
  assert.equal(calls,code==='TRIAL_LOCKED'?2:1);
}
for (const kind of ['critical','infra']) {
  const f=fixture(kind==='critical'?10:0,kind==='critical',kind==='infra');
  const result=await f.drive(undefined,f.scope);
  assert.equal(result.ok,false); assert.equal(f.reads(),1); assert.equal(f.mutations(),0);
  assert.equal(readAutopilotState(f.runDir).enabled,false);
}
{
  // A crash retains the retry budget, even on its last retry.
  const f=fixture(10);
  writeAutopilotState(f.runDir,true,{count:3,retryAt:Date.now()-1,error:'STATUS_TIMEOUT'});
  const result=await f.newOwner()(undefined,f.scope);
  assert.match(result.autopilot.error,/AUTOPILOT_RETRY_EXHAUSTED/);
  assert.equal(f.reads(),1);
}
{
  // Restore after a repair completed but before the next drive, without any UI.
  const f=fixture(); writeAutopilotState(f.runDir,true);
  const recovered=[];
  productionRecovery({...f.bindings,readAutopilotState,writeAutopilotState,queueMicrotask:callback=>callback(),runIterativeDriveWithAutopilot:(...args)=>{ const result=f.newOwner()(...args); recovered.push(result); return result; }});
  assert.equal(recovered.length,1); assert.equal((await recovered[0]).autopilot.stopped,'finished'); assert.equal(f.mutations(),1);
  // Disabled, canceled and foreign run identities cannot be resurrected.
  for (const kind of ['disabled','canceled','foreign','preference']) {
    writeAutopilotState(f.runDir,kind!=='disabled',undefined,kind!=='preference');
    attempts.updateAttempt(f.runDir,{cancelRequested:kind==='canceled'});
    if(kind==='foreign') writeFileSync(join(f.runDir,'autopilot.json'),JSON.stringify({schemaVersion:1,runId:'other',enabled:true,resume:true}));
    productionRecovery({...f.bindings,readAutopilotState,writeAutopilotState,queueMicrotask:callback=>callback(),runIterativeDriveWithAutopilot:()=>{throw Error('Unexpected resurrection');}});
  }
}
// A real Python-equivalent writer must finish before restart dispatches a step.
for (const canceled of [false,true]) {
  const f=fixture();
  const writer=spawn(process.execPath,['-e','setTimeout(()=>{},10000)'],{windowsHide:true});
  const writerClosed=new Promise(resolve=>writer.once('close',resolve));
  writeFileSync(join(f.runDir,'run.lock'),JSON.stringify({pid:writer.pid,owner:'fixture'}));
  const isAlive=pid=>{try{process.kill(pid,0);return true;}catch{return false;}};
  f.options.waitForWriter=()=>liveRunWriterPid(f.runDir,isAlive) ? Date.now()+10 : undefined;
  const pending=f.newOwner()(undefined,f.scope);
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(f.reads(),0); assert.equal(f.mutations(),0);
  if(canceled) f.auto().setEnabled(f.scope,false);
  writer.kill(); await writerClosed;
  assert.equal((await pending).autopilot.stopped,canceled?'paused':'finished');
  assert.equal(f.mutations(),canceled?0:1);
}
for (const stop of ['pause','cancel']) {
  const f=fixture(); let calls=0;
  writeAutopilotState(f.runDir,true,{count:1,retryAt:Date.now()+60_000,error:'STATUS_TIMEOUT'});
  const auto=createIterativeAutopilot(()=> 'w:demo',f.options);
  const drive=auto.register('drive',async()=>{calls++;return{ok:true,stopped:'finished'};});
  const cancel=auto.register('cancel',async()=>({ok:true}));
  const pending=drive(undefined,f.scope);
  await new Promise(resolve=>setTimeout(resolve,10));
  if(stop==='pause') auto.setEnabled(f.scope,false); else await cancel(undefined,f.scope);
  assert.equal((await pending).autopilot.stopped,stop==='pause'?'paused':'canceled');
  assert.equal(calls,0); assert.equal(readAutopilotState(f.runDir).enabled,false);
}
{
  // Reused request labels on different candidates are real progress, not a loop.
  const auto=createIterativeAutopilot(()=> 'w:demo'); let calls=0, repairs=0;
  auto.register('status',async()=>({ok:true,decision:{step:'agent'}}));
  auto.register('agent',async()=>{repairs++;return{ok:true};});
  const drive=auto.register('drive',async()=>++calls<3 ? {ok:true,stopped:'agent-gate',runId:'run',candidateId:`candidate-${calls}`,repairRequests:[{requestId:'same-label'}]} : {ok:true,stopped:'finished'});
  assert.equal((await drive(undefined,{projectName:'demo',autopilot:true})).autopilot.stopped,'finished');
  assert.equal(repairs,2);
}
{
  // Disabling during a status retry at a repair gate is a pause, not an error.
  const pausing=createIterativeAutopilot(()=> 'w:demo',{retryDelayMs:()=>60_000}); let repairs=0;
  pausing.register('status',async()=>({ok:false,retryable:true,error:'STATUS_TIMEOUT'}));
  pausing.register('agent',async()=>{repairs++;return{ok:true};});
  const pauseDrive=pausing.register('drive',async()=>({ok:true,stopped:'agent-gate',repairRequests:[{requestId:'pause-status'}]}));
  const pending=pauseDrive(undefined,{projectName:'demo',autopilot:true});
  await new Promise(resolve=>setTimeout(resolve,20)); pausing.setEnabled({projectName:'demo'},false);
  assert.equal((await pending).autopilot.stopped,'paused'); assert.equal(repairs,0);
}
{
  // Freeze the default workspace before the first step; later UI selection is
  // not authority to move this mission to another workspace.
  let selected='original'; const scopes=[]; let drives=0;
  const bound=createIterativeAutopilot(scope=>scope.workspaceId??selected,{normalizeScope:scope=>({...scope,workspaceId:scope.workspaceId??selected})});
  bound.register('status',async()=>({ok:true,decision:{step:'agent'}}));
  bound.register('agent',async(_event,input)=>{scopes.push(input.workspaceId);return{ok:true};});
  const boundDrive=bound.register('drive',async(_event,input)=>{scopes.push(input.workspaceId);selected='another';return++drives===1?{ok:true,stopped:'agent-gate',repairRequests:[{requestId:'bound'}]}:{ok:true,stopped:'finished'};});
  assert.equal((await boundDrive(undefined,{projectName:'demo',autopilot:true})).autopilot.stopped,'finished');
  assert.deepEqual(scopes,['original','original','original']);
}
{
  // A one-shot infra retry already consumed before a failed read is not replayed.
  const auto=createIterativeAutopilot(()=> 'w:demo',{retryDelayMs:()=>1}); const flags=[];
  const drive=auto.register('drive',async(_event,input)=>{flags.push(input.retryInfra===true);return flags.length===1 ? {ok:false,retryable:true,retryInfraPending:false,error:'STATUS_TIMEOUT'} : {ok:true,stopped:'finished'};});
  assert.equal((await drive(undefined,{projectName:'demo',autopilot:true,retryInfra:true})).autopilot.stopped,'finished');
  assert.deepEqual(flags,[true,false]);
}
console.log('autopilot lifecycle: production drive, durable restart, identity, bounded reads, cancel, critical and exhausted verification boundaries OK');
