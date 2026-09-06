import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { BaselineWorkerPool } from '../dist-electron/baseline-worker.js'

const python = process.env.DEPLOOM_TEST_PYTHON || 'python'
const root = mkdtempSync(join(tmpdir(), 'deploom-worker-hardening-'))
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

async function protocolCorruption() {
  let kills = 0
  const pool = new BaselineWorkerPool(
    python,
    fakeWorker,
    child => { kills += 1; child.kill() },
    60_000,
    500,
  )
  const first = pool.run('same', {
    cwd: root,
    argv: ['protocol-corrupt'],
    onOutput: () => {},
  })
  let rejected = false
  try { await first.result } catch { rejected = true }
  if (!rejected) throw new Error('protocol corruption was not rejected')
  await delay(80)
  if (kills < 1) throw new Error('protocol corruption did not terminate worker tree')

  const second = pool.run('same', {
    cwd: root,
    argv: ['ok'],
    onOutput: () => {},
  })
  try {
    await second.result
  } catch (error) {
    if (!String(error).includes('BASELINE_WORKER_RETIRING')) throw error
  }
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
    cwd: root,
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
  if (forcedKills !== 0) {
    throw new Error(`idle worker needed force kill unexpectedly: ${forcedKills}`)
  }
  pool.dispose(child => child.kill())
}

try {
  await protocolCorruption()
  await gracefulIdle()
  console.log('Baseline worker lifecycle hardening contracts OK')
} finally {
  rmSync(root, { recursive: true, force: true })
}
