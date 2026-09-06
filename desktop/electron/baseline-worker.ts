import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { delimiter, dirname, join } from 'node:path'
import { commandEnvironment, decodeProcessOutputChunk, processTreeDetached, resolveSpawnInvocation } from './process-launcher.js'

export type BaselineWorkerResult = {
  code: number
  error?: string
}

type PendingRequest = {
  id: string
  settled: boolean
  resolve: (value: BaselineWorkerResult) => void
  reject: (error: Error) => void
  onOutput: (stream: 'stdout' | 'stderr', text: string) => void
}

type WorkerRecord = {
  child: ChildProcessWithoutNullStreams
  carry: string
  pending: Map<string, PendingRequest>
  alive: boolean
  idleTimer?: ReturnType<typeof setTimeout>
}

export class BaselineWorkerPool {
  private readonly records = new Map<string, WorkerRecord>()

  constructor(
    private readonly pythonCommand: string,
    private readonly workerPath: string,
    private readonly idleTimeoutMs = 20 * 60_000,
  ) {}

  private start(key: string, cwd: string): WorkerRecord {
    const vendor = join(dirname(this.workerPath), 'vendor')
    const env = commandEnvironment({
      ...process.env,
      PYTHONUNBUFFERED: '1',
      PYTHONPATH: [vendor, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      FORCE_COLOR: '0',
    })
    const invocation = resolveSpawnInvocation(
      this.pythonCommand,
      [this.workerPath],
      { env },
    )
    const child = spawn(invocation.command, invocation.args, {
      cwd,
      shell: false,
      detached: processTreeDetached(),
      windowsHide: true,
      windowsVerbatimArguments: invocation.windowsVerbatimArguments,
      env,
    })
    const record: WorkerRecord = {
      child,
      carry: '',
      pending: new Map(),
      alive: true,
    }
    this.records.set(key, record)

    const failPending = (message: string) => {
      if (!record.alive) return
      record.alive = false
      if (record.idleTimer) clearTimeout(record.idleTimer)
      for (const request of record.pending.values()) {
        if (!request.settled) {
          request.settled = true
          request.reject(new Error(message))
        }
      }
      record.pending.clear()
      if (this.records.get(key) === record) this.records.delete(key)
    }

    const scheduleIdle = () => {
      if (!record.alive || record.pending.size > 0) return
      if (record.idleTimer) clearTimeout(record.idleTimer)
      record.idleTimer = setTimeout(() => {
        if (record.pending.size !== 0 || !record.alive) return
        record.alive = false
        if (this.records.get(key) === record) this.records.delete(key)
        record.child.kill()
      }, this.idleTimeoutMs)
    }

    child.on('error', error => {
      failPending(`BASELINE_WORKER_START_FAILED: ${error.message}`)
    })
    child.on('close', code => {
      failPending(`BASELINE_WORKER_EXITED: code=${code ?? -1}`)
    })
    child.stderr.on('data', chunk => {
      const text = decodeProcessOutputChunk(chunk as Buffer)
      if (!text) return
      for (const request of record.pending.values()) {
        request.onOutput('stderr', text)
      }
    })
    child.stdout.on('data', chunk => {
      record.carry += decodeProcessOutputChunk(chunk as Buffer)
      while (true) {
        const newline = record.carry.indexOf('\n')
        if (newline < 0) break
        const line = record.carry.slice(0, newline)
        record.carry = record.carry.slice(newline + 1)
        if (!line.trim()) continue
        let message: any
        try {
          message = JSON.parse(line)
        } catch {
          failPending(
            `BASELINE_WORKER_PROTOCOL_INVALID: ${line.slice(0, 500)}`,
          )
          return
        }
        if (message?.type === 'ready') continue
        const id = typeof message?.id === 'string' ? message.id : ''
        const request = record.pending.get(id)
        if (!request) continue
        if (message?.type === 'stream') {
          const stream: 'stdout' | 'stderr' =
            message?.stream === 'stderr' ? 'stderr' : 'stdout'
          request.onOutput(stream, String(message?.data ?? ''))
          continue
        }
        if (message?.type === 'complete') {
          const code = Number.isFinite(Number(message?.code))
            ? Number(message.code)
            : 4
          request.settled = true
          request.resolve({
            code,
            ...(message?.error ? { error: String(message.error) } : {}),
          })
          record.pending.delete(id)
          scheduleIdle()
        }
      }
    })
    return record
  }

  run(
    key: string,
    request: {
      cwd: string
      argv: string[]
      env?: NodeJS.ProcessEnv
      onOutput: (stream: 'stdout' | 'stderr', text: string) => void
    },
  ): {
    child: ChildProcessWithoutNullStreams
    result: Promise<BaselineWorkerResult>
  } {
    let record = this.records.get(key)
    if (!record || !record.alive || record.child.killed) {
      record = this.start(key, request.cwd)
    }
    if (record.idleTimer) {
      clearTimeout(record.idleTimer)
      record.idleTimer = undefined
    }

    const id = randomUUID()
    let resolveRequest!: (value: BaselineWorkerResult) => void
    let rejectRequest!: (error: Error) => void
    const result = new Promise<BaselineWorkerResult>((resolve, reject) => {
      resolveRequest = resolve
      rejectRequest = reject
    })
    record.pending.set(id, {
      id,
      settled: false,
      resolve: resolveRequest,
      reject: rejectRequest,
      onOutput: request.onOutput,
    })
    record.child.stdin.write(`${JSON.stringify({
      id,
      cwd: request.cwd,
      argv: request.argv,
      env: request.env ?? {},
    })}\n`, 'utf8')
    return { child: record.child, result }
  }

  dispose(kill: (child: ChildProcessWithoutNullStreams) => void): void {
    for (const [key, record] of this.records) {
      this.records.delete(key)
      record.alive = false
      if (record.idleTimer) clearTimeout(record.idleTimer)
      kill(record.child)
    }
  }
}
