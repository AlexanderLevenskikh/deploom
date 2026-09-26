// A05: when a worker is retiring, the run() handle must refer to the process
// that ACTUALLY executes the request — a fresh worker — not the dying one.
// The old code returned the retiring child, so cancel/timeout (killProcessTree
// by pid) killed a dead process while the fresh worker kept running the
// command and its mutations. Also: cancelling before the fresh worker spawns
// must prevent it from starting at all.
import { EventEmitter } from 'node:events'
import { BaselineWorkerPool } from '../dist-electron/baseline-worker.js'

// Case 1: retirement in progress -> the handle tracks the FRESH worker that
// will execute the request, not the retiring one.
{
  const pool = new BaselineWorkerPool('', '', () => {})
  const oldChild = new EventEmitter()
  oldChild.pid = 101
  const newChild = new EventEmitter()
  newChild.pid = 202
  newChild.killed = false
  newChild.kill = () => { newChild.killed = true }
  pool.records.set('fixture', { child: oldChild, alive: false, closing: true, closed: false, pending: new Map(), carry: '' })
  const productionRun = pool.run.bind(pool)
  let spawnedFresh = false
  pool.run = () => {
    spawnedFresh = true
    return { child: newChild, result: Promise.resolve({ code: 0 }) }
  }
  const handle = productionRun('fixture', { cwd: process.cwd(), argv: [], onOutput() {} })
  oldChild.emit('close', 0) // retirement completes -> fresh worker spawns
  if (!spawnedFresh) throw new Error('A05: fresh worker must run the request after retirement')
  if (handle.child === oldChild) throw new Error('A05: the handle must never be the retiring child')
  if (handle.child.pid !== 202) throw new Error(`A05: handle.pid must track the executing fresh worker, got ${handle.child.pid}`)
  await handle.result
  handle.child.kill('SIGKILL')
  if (!newChild.killed) throw new Error('A05: killing the handle must kill the fresh worker')
}

// Case 2: cancel BEFORE retirement closes -> the pending fresh spawn is
// skipped, the request rejects instead of silently running unkillable.
{
  const pool = new BaselineWorkerPool('', '', () => {})
  const oldChild = new EventEmitter()
  oldChild.pid = 101
  pool.records.set('fixture', { child: oldChild, alive: false, closing: true, closed: false, pending: new Map(), carry: '' })
  const productionRun = pool.run.bind(pool)
  let spawnedFresh = false
  pool.run = () => {
    spawnedFresh = true
    throw new Error('must not spawn')
  }
  const handle = productionRun('fixture', { cwd: process.cwd(), argv: [], onOutput() {} })
  handle.child.kill('SIGKILL') // cancel while still waiting for retirement close
  oldChild.emit('close', 0)
  if (spawnedFresh) throw new Error('A05: cancelling before retirement must prevent the fresh worker from spawning')
  let outcome = 'resolved'
  await handle.result.catch(() => { outcome = 'rejected' })
  if (outcome !== 'rejected') throw new Error('A05: a cancelled pending request must reject, not resolve')
}

console.log('Baseline worker retirement handle OK')
