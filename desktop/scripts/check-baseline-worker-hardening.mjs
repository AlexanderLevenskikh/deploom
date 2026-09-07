import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { BaselineWorkerPool } from '../dist-electron/baseline-worker.js'

const python = process.env.DEPLOOM_TEST_PYTHON || 'python'
const root = mkdtempSync(join(tmpdir(), 'deploom-worker-hardening-'))
const workerCwd = process.cwd()
const fakeWorker = join(root, 'fake-worker.py')
writeFileSync(fakeWorker, String.raw`
import atexit
import json
import sys
import time

marker = ""

@atexit.register
def done():
    if marker:
        with open(marker, "w", encoding="utf-8") as stream:
            stream.write("clean")

sys.stdout.write('{"type":"ready","pid":1,"encoding":"utf-8"}\n')
sys.stdout.flush()
for raw in sys.stdin:
    request = json.loads(raw)
    argv = request.get("argv") or []
    mode = argv[0] if argv else "ok"
    if mode == "protocol-corrupt":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
        while True:
            time.sleep(1)
    marker = str((request.get("env") or {}).get("MARKER") or "")
    sys.stdout.write(json.dumps({
        "type":"complete","id":request["id"],"code":0,"reusable":True
    }, separators=(",", ":")) + "\n")
    sys.stdout.flush()
`, 'utf8')

async function waitForChildExit(child, label) {
  const deadline = Date.now() + 3000
  while (
    Date.now() < deadline
    && child.exitCode === null
    && child.signalCode === null
  ) {
    await delay(20)
  }
  if (child.exitCode === null && child.signalCode === null) {
    throw new Error(`${label} process did not exit`)
  }
}

async function protocolCorruption() {
  let kills = 0
  const pool = new BaselineWorkerPool(
    python,
    fakeWorker,
    child => { kills += 1; child.kill() },
    60_000,
    500,
  )
  const first = pool.run('protocol', {
    cwd: workerCwd,
    argv: ['protocol-corrupt'],
    onOutput: () => {},
  })
  let rejected = false
  try { await first.result } catch { rejected = true }
  if (!rejected) throw new Error('protocol corruption was not rejected')
  if (kills < 1) throw new Error('protocol corruption did not terminate worker tree')
  await waitForChildExit(first.child, 'protocol corruption worker')
  pool.dispose(child => child.kill())
}

async function stdinStreamErrorRetiresWorker() {
  let kills = 0
  const pool = new BaselineWorkerPool(
    python,
    fakeWorker,
    child => { kills += 1; child.kill() },
    60_000,
    500,
  )
  const first = pool.run('stdin-stream', {
    cwd: workerCwd,
    argv: ['ok'],
    onOutput: () => {},
  })
  const result = await first.result
  if (result.code !== 0) throw new Error(`stdin stream setup failed: ${result.code}`)

  first.child.stdin.emit('error', new Error('synthetic stdin stream failure'))
  await delay(20)
  if (kills < 1) throw new Error('stdin stream error did not retire worker tree')
  await waitForChildExit(first.child, 'stdin stream failure worker')
  pool.dispose(child => child.kill())
}

async function stdinWriteCallbackFailureRetiresWorker() {
  let kills = 0
  const pool = new BaselineWorkerPool(
    python,
    fakeWorker,
    child => { kills += 1; child.kill() },
    60_000,
    500,
  )
  const first = pool.run('stdin-callback', {
    cwd: workerCwd,
    argv: ['ok'],
    onOutput: () => {},
  })
  const result = await first.result
  if (result.code !== 0) throw new Error(`stdin callback setup failed: ${result.code}`)

  first.child.stdin.write = (...args) => {
    const callback = args.at(-1)
    if (typeof callback === 'function') {
      queueMicrotask(() => callback(new Error('synthetic stdin callback failure')))
    }
    return true
  }

  const second = pool.run('stdin-callback', {
    cwd: workerCwd,
    argv: ['never-written'],
    onOutput: () => {},
  })
  let rejected = false
  try {
    await Promise.race([
      second.result,
      delay(2000).then(() => {
        throw new Error('stdin callback failure request did not settle')
      }),
    ])
  } catch (error) {
    if (String(error).includes('did not settle')) throw error
    if (!String(error).includes('BASELINE_WORKER_STDIN_FAILED')) throw error
    rejected = true
  }
  if (!rejected) throw new Error('stdin callback failure was not rejected')
  if (kills < 1) throw new Error('stdin callback failure did not retire worker tree')
  await waitForChildExit(first.child, 'stdin callback failure worker')
  pool.dispose(child => child.kill())
}

async function stdinWriteThrowRetiresWorker() {
  let kills = 0
  const pool = new BaselineWorkerPool(
    python,
    fakeWorker,
    child => { kills += 1; child.kill() },
    60_000,
    500,
  )
  const first = pool.run('stdin-throw', {
    cwd: workerCwd,
    argv: ['ok'],
    onOutput: () => {},
  })
  const result = await first.result
  if (result.code !== 0) throw new Error(`stdin throw setup failed: ${result.code}`)

  first.child.stdin.write = () => {
    throw new Error('synthetic stdin write throw')
  }

  const second = pool.run('stdin-throw', {
    cwd: workerCwd,
    argv: ['never-written'],
    onOutput: () => {},
  })
  let rejected = false
  try {
    await Promise.race([
      second.result,
      delay(2000).then(() => {
        throw new Error('stdin write throw request did not settle')
      }),
    ])
  } catch (error) {
    if (String(error).includes('did not settle')) throw error
    if (!String(error).includes('BASELINE_WORKER_STDIN_FAILED')) throw error
    rejected = true
  }
  if (!rejected) throw new Error('stdin write throw was not rejected')
  if (kills < 1) throw new Error('stdin write throw did not retire worker tree')
  await waitForChildExit(first.child, 'stdin write throw worker')
  pool.dispose(child => child.kill())
}

async function gracefulIdle() {
  let forcedKills = 0
  const marker = join(root, 'idle-clean.txt')
  const pool = new BaselineWorkerPool(
    python,
    fakeWorker,
    child => { forcedKills += 1; child.kill() },
    50,
    800,
  )
  const request = pool.run('idle', {
    cwd: workerCwd,
    argv: ['ok'],
    env: { MARKER: marker },
    onOutput: () => {},
  })
  const result = await request.result
  if (result.code !== 0) throw new Error(`fake worker failed: ${result.code}`)

  const deadline = Date.now() + 3000
  while (Date.now() < deadline && !existsSync(marker)) await delay(25)
  if (!existsSync(marker) || readFileSync(marker, 'utf8') !== 'clean') {
    throw new Error('idle worker did not run Python atexit cleanup')
  }
  await waitForChildExit(request.child, 'graceful idle worker')
  if (forcedKills !== 0) {
    throw new Error(`idle worker needed force kill unexpectedly: ${forcedKills}`)
  }
  pool.dispose(child => child.kill())
}

try {
  await protocolCorruption()
  await stdinStreamErrorRetiresWorker()
  await stdinWriteCallbackFailureRetiresWorker()
  await stdinWriteThrowRetiresWorker()
  await gracefulIdle()
  console.log('Baseline worker lifecycle hardening contracts OK')
} finally {
  rmSync(root, {
    recursive: true,
    force: true,
    maxRetries: 20,
    retryDelay: 50,
  })
}
