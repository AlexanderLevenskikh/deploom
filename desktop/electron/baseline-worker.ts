import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { delimiter, dirname, join } from 'node:path'
import { commandEnvironment, processTreeDetached, resolveSpawnInvocation } from './process-launcher.js'

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
  closing: boolean
  closed: boolean
  idleTimer?: ReturnType<typeof setTimeout>
  forceTimer?: ReturnType<typeof setTimeout>
  retire?: (message: string, mode: 'graceful' | 'force') => void
}

export class BaselineWorkerPool {
  private readonly records = new Map<string, WorkerRecord>()

  constructor(
    private readonly pythonCommand: string,
    private readonly workerPath: string,
    private readonly terminateTree: (child: ChildProcessWithoutNullStreams) => void,
    private readonly idleTimeoutMs = 20 * 60_000,
    private readonly gracefulShutdownTimeoutMs = 5_000,
  ) {}

  private start(key: string, cwd: string): WorkerRecord {
    const vendor = join(dirname(this.workerPath), 'vendor')
    const env = commandEnvironment({
      ...process.env,
      PYTHONUNBUFFERED: '1',
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
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
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')

    const record: WorkerRecord = {
      child,
      carry: '',
      pending: new Map(),
      alive: true,
      closing: false,
      closed: false,
    }
    this.records.set(key, record)

    const rejectPending = (message: string) => {
      for (const request of record.pending.values()) {
        if (!request.settled) {
          request.settled = true
          request.reject(new Error(message))
        }
      }
      record.pending.clear()
    }

    const beginRetirement = (
      message: string,
      mode: 'graceful' | 'force',
    ) => {
      if (record.closing || record.closed) return
      record.alive = false
      record.closing = true
      if (record.idleTimer) clearTimeout(record.idleTimer)
      rejectPending(message)

      if (mode === 'force') {
        this.terminateTree(record.child)
        return
      }

      try {
        record.child.stdin.end()
      } catch {
        this.terminateTree(record.child)
        return
      }
      record.forceTimer = setTimeout(() => {
        if (!record.closed) this.terminateTree(record.child)
      }, this.gracefulShutdownTimeoutMs)
    }
    record.retire = beginRetirement

    const scheduleIdle = () => {
      if (!record.alive || record.pending.size > 0) return
      if (record.idleTimer) clearTimeout(record.idleTimer)
      record.idleTimer = setTimeout(() => {
        if (record.pending.size !== 0 || !record.alive) return
        beginRetirement('BASELINE_WORKER_IDLE_SHUTDOWN', 'graceful')
      }, this.idleTimeoutMs)
    }

    child.stdin.on('error', error => {
      beginRetirement(
        `BASELINE_WORKER_STDIN_FAILED: ${error.message}`,
        'force',
      )
    })
    child.on('error', error => {
      beginRetirement(`BASELINE_WORKER_START_FAILED: ${error.message}`, 'force')
    })
    child.on('close', code => {
      record.closed = true
      record.alive = false
      record.closing = false
      if (record.idleTimer) clearTimeout(record.idleTimer)
      if (record.forceTimer) clearTimeout(record.forceTimer)
      rejectPending(`BASELINE_WORKER_EXITED: code=${code ?? -1}`)
      if (this.records.get(key) === record) this.records.delete(key)
    })
    child.stderr.on('data', (text: string) => {
      if (!text) return
      for (const request of record.pending.values()) {
        request.onOutput('stderr', text)
      }
    })
    child.stdout.on('data', (text: string) => {
      record.carry += text
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
          beginRetirement(
            `BASELINE_WORKER_PROTOCOL_INVALID: ${line.slice(0, 500)}`,
            'force',
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
          const reusable = message?.reusable !== false
          request.settled = true
          request.resolve({
            code,
            ...(message?.error ? { error: String(message.error) } : {}),
          })
          record.pending.delete(id)
          if (reusable) {
            scheduleIdle()
          } else {
            beginRetirement(
              String(
                message?.retirementReason
                  || 'BASELINE_WORKER_RETIRED_AFTER_UNSAFE_REQUEST'
              ),
              'force',
            )
          }
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
    if (record && (record.closing || (!record.alive && !record.closed))) {
      return {
        child: record.child,
        result: Promise.reject(new Error('BASELINE_WORKER_RETIRING')),
      }
    }
    if (!record || record.closed || record.child.killed) {
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
    const activeRecord = record
    const payload = `${JSON.stringify({
      id,
      cwd: request.cwd,
      argv: request.argv,
      env: request.env ?? {},
    })}\n`
    const retireWriteFailure = (error: unknown) => {
      const normalized = error instanceof Error
        ? error
        : new Error(String(error))
      activeRecord.retire?.(
        `BASELINE_WORKER_STDIN_FAILED: ${normalized.message}`,
        'force',
      )
    }
    try {
      activeRecord.child.stdin.write(payload, 'utf8', error => {
        if (error) retireWriteFailure(error)
      })
    } catch (error) {
      retireWriteFailure(error)
    }
    return { child: activeRecord.child, result }
  }

  dispose(kill: (child: ChildProcessWithoutNullStreams) => void): void {
    for (const [key, record] of this.records) {
      this.records.delete(key)
      record.alive = false
      record.closing = true
      if (record.idleTimer) clearTimeout(record.idleTimer)
      if (record.forceTimer) clearTimeout(record.forceTimer)
      kill(record.child)
    }
  }
}
