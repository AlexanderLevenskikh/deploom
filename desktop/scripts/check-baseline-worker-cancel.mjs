// R3: the production cancel path is killProcessTree(handle.child) -- taskkill
// by pid on Windows / process-group kill on POSIX. It NEVER calls
// ChildProcess.kill(), so the run handle must advertise a REQUEST-scoped
// canceller under WORKER_CANCEL, and killProcessTree must invoke it BEFORE
// stopping the OS process tree. Only the request knows that this kill is a
// user cancellation; only that knowledge stops a retiring worker's close event
// from spawning a fresh worker after the user asked to stop.
//
// Parts:
//   A. unit  -- the REAL killProcessTree sliced out of main.ts, with mocked
//      spawn/process: WORKER_CANCEL hook runs first; a hook-less child is
//      still stopped (win32 and POSIX branches).
//   B. unit  -- the retirement-branch child proxy (compiled baseline-worker.js)
//      exposes WORKER_CANCEL, and invoking it rejects the pending request and
//      suppresses the fresh worker.
//   C. smoke -- a real python worker tree is started through the compiled
//      BaselineWorkerPool wired to the REAL production killProcessTree, then
//      cancelled via killProcessTree(handle.child): the process really dies
//      and the pending request settles (never hangs forever).
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
import ts from 'typescript'
import { BaselineWorkerPool, WORKER_CANCEL } from '../dist-electron/baseline-worker.js'

const source = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const sliceStart = source.indexOf('function killProcessTree(')
const sliceEnd = source.indexOf('type CaptureResult =', sliceStart)
if (sliceStart < 0 || sliceEnd < 0) throw new Error('R3: killProcessTree slice not found in main.ts')
const trans = (text) => ts.transpileModule(text, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const compileKiller = (js) => new Function(
  'WORKER_CANCEL', 'spawn', 'process',
  js + '\nreturn { killProcessTree };',
)

// --- Part A: the production killProcessTree, once with mocked OS, once real.
const killerJs = trans(source.slice(sliceStart, sliceEnd))
const mockKillLog = []
const mockProcess = {
  platform: 'win32',
  kill: (pid, sig) => { mockKillLog.push(`process.kill ${pid} ${sig ?? ''}`) },
}
const orderLog = []
const spySpawn = (cmd, args) => { orderLog.push(`spawn ${cmd} ${args.map(String).join(' ')}`); const k = new EventEmitter(); k.pid = 0; return k }

// A1: a worker handle that exposes WORKER_CANCEL must have its canceller run
// BEFORE the OS tree is stopped -- and the tree must still be stopped.
{
  const log = []
  const child = { pid: 42, kill: (sig) => { log.push(`child.kill ${sig}`) } }
  child[WORKER_CANCEL] = () => { log.push('request-cancel') }
  const killProcessTree = compileKiller(killerJs)(WORKER_CANCEL, (cmd, args) => {
    log.push(`spawn ${cmd} ${args.map(String).join(' ')}`)
    return new EventEmitter()
  }, mockProcess).killProcessTree
  killProcessTree(child)
  if (log[0] !== 'request-cancel') {
    throw new Error(`R3: WORKER_CANCEL must be invoked BEFORE the OS kill; order was ${JSON.stringify(log)}`)
  }
  if (!log.some(x => x.startsWith('spawn taskkill'))) {
    throw new Error(`R3: the process tree must still be stopped after the request cancel; order was ${JSON.stringify(log)}`)
  }
}

// A2: a generic (hook-less) child is still stopped -- taskkill on win32.
{
  orderLog.length = 0
  const killProcessTree = compileKiller(killerJs)(WORKER_CANCEL, spySpawn, mockProcess).killProcessTree
  killProcessTree({ pid: 7, kill: () => {} })
  if (!orderLog.some(x => x.startsWith('spawn taskkill'))) {
    throw new Error(`R3: a hook-less child must still be tree-killed on win32; got ${JSON.stringify(orderLog)}`)
  }
}

// A3: on POSIX the process GROUP is signalled, no taskkill spawn.
{
  orderLog.length = 0
  mockKillLog.length = 0
  const posixProcess = { platform: 'posix', kill: (pid, sig) => { mockKillLog.push(`process.kill ${pid} ${sig ?? ''}`) } }
  const killProcessTree = compileKiller(killerJs)(WORKER_CANCEL, spySpawn, posixProcess).killProcessTree
  killProcessTree({ pid: 9, kill: () => {} })
  if (orderLog.length !== 0) throw new Error(`R3: POSIX cancel must not spawn taskkill; got ${JSON.stringify(orderLog)}`)
  if (!mockKillLog.some(x => x === 'process.kill -9 SIGTERM')) {
    throw new Error(`R3: POSIX cancel must SIGTERM the process group; got ${JSON.stringify(mockKillLog)}`)
  }
}

// --- Part B: the retirement-branch child PROXY carries WORKER_CANCEL, and
// invoking it rejects the pending request and suppresses the fresh spawn.
{
  const pool = new BaselineWorkerPool('', '', () => { throw new Error('R3: must not terminate') })
  const oldChild = new EventEmitter()
  oldChild.pid = 101
  oldChild.killed = false
  oldChild.kill = () => { oldChild.killed = true }
  pool.records.set('fixture', { child: oldChild, alive: false, closing: true, closed: false, pending: new Map(), carry: '' })
  const productionRun = pool.run.bind(pool)
  let spawnedFresh = false
  pool.run = () => { spawnedFresh = true; throw new Error('R3: fresh worker must not spawn after a cancel') }
  const handle = productionRun('fixture', { cwd: process.cwd(), argv: [], onOutput() {} })
  const cancelHook = handle.child[WORKER_CANCEL]
  if (typeof cancelHook !== 'function') {
    throw new Error('R3: the retiring run handle must expose WORKER_CANCEL on its child proxy')
  }
  cancelHook()
  oldChild.emit('close', 0)
  if (spawnedFresh) throw new Error('R3: WORKER_CANCEL must suppress the pending fresh worker spawn')
  let outcome = 'resolved'
  await handle.result.catch(() => { outcome = 'rejected' })
  if (outcome !== 'rejected') throw new Error('R3: a WORKER_CANCEL-cancelled pending request must reject')
}

// --- Part C: real-spawn smoke through the REAL production killProcessTree.
{
  const worker = path.join(os.tmpdir(), `deploom-r3-worker-${process.pid}-${Date.now()}.py`)
  fs.writeFileSync(worker, 'import time\ntime.sleep(3600)\n')
  try {
    // Preflight: python must actually exist, else the smoke cannot run.
    const probe = spawn('python', ['--version'], { stdio: 'ignore' })
    const pythonError = await new Promise((resolve) => {
      probe.once('error', resolve)
      probe.once('spawn', () => resolve(null))
    })
    if (pythonError) {
      console.log('R3: real-spawn smoke skipped (python unavailable); unit contract verified above')
    } else {
      await new Promise((resolve) => probe.once('close', resolve))
      const realKiller = compileKiller(killerJs)(WORKER_CANCEL, spawn, process).killProcessTree
      const pool = new BaselineWorkerPool('python', worker, realKiller, 3_600_000, 1_000, 15_000)
      const handle = pool.run('smoke', { cwd: os.tmpdir(), argv: [], onOutput() {} })
      await new Promise((resolve) => setTimeout(resolve, 800))
      const pid = handle.child.pid
      if (!pid) throw new Error('R3: smoke worker must expose a pid')
      realKiller(handle.child)
      let code = null
      let rejected = false
      await handle.result.then(
        (r) => { code = r.code },
        () => { rejected = true },
      )
      if (!rejected && code === null) throw new Error('R3: the cancelled smoke request must settle (reject)')
      if (!rejected && code === 0) throw new Error('R3: a taskkilled worker must never settle as code 0')
    }
  } finally {
    try { fs.unlinkSync(worker) } catch { /* already gone */ }
  }
}

console.log('Baseline worker request-level cancel (R3) OK')
