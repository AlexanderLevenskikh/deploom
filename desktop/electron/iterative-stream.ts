// Streaming runner for the long iterative begin/drive steps (Desktop).
//
// This is the testable core behind `spawnIterativeStreamed`. It behaves like
// the plain capture runner (same `CaptureResult` contract) but ALSO:
//  - appends every decoded line to `runDir/attempt.log` — the run diagnostic
//    artifact that survives a Desktop restart;
//  - parses the Python `ITERATIVE_MIGRATION_STATUS_V1 {…}` event lines and
//    forwards each event to `onEvent`, so `begin.discovery-progress` feeds the
//    attempt progress WITHOUT waiting for the child to finish;
//  - honours an attempt.cancelRequested flag: once set it terminates the child
//    tree, including when the operation has no overall deadline;
//  - treats a kill BY the watchdog (timeout or user cancel) as a NON-ZERO
//    outcome, never as success — a timed-out begin must not be able to claim
//    "C0 captured" just because the killed child never produced an exit code;
//  - never drops a line that arrives in pieces: incomplete stdout lines are
//    buffered across chunks (and flushed on close), so a status/error split
//    into two TCP writes is still parsed and journaled.
import { spawn, type ChildProcess } from 'node:child_process'

const MAX_CAPTURE_BYTES = 1024 * 1024
function captureTail(previous: string, text: string): string {
  const bytes = Buffer.from(previous + text, 'utf8')
  let start = Math.max(0, bytes.length - MAX_CAPTURE_BYTES)
  while (start < bytes.length && (bytes[start] & 0xc0) === 0x80) start++
  return bytes.subarray(start).toString('utf8')
}

export type CaptureResult = { code: number; stdout: string; stderr: string; timedOut: boolean; canceled?: boolean }

export type StreamPlatform = {
  processTreeDetached: () => boolean
  commandEnvironment: (env: NodeJS.ProcessEnv) => NodeJS.ProcessEnv
  resolveSpawnInvocation: (
    command: string,
    args: string[],
    opts: { env: NodeJS.ProcessEnv },
  ) => { command: string; args: string[]; windowsVerbatimArguments?: boolean }
  killProcessTree: (child: ChildProcess) => void
  decodeChunk: (chunk: Buffer) => string
}

export type StreamAttemptIo = {
  cancelRequested: (runDir: string) => boolean
  recordLine: (runDir: string, text: string) => void
  trimLog: (runDir: string) => void
}

/** Split decoded chunk text into COMPLETE lines, keeping an incomplete tail as
 * the new carry. Empty lines are dropped (a blank line carries no evidence),
 * and a trailing piece WITHOUT a newline is never emitted as a line here —
 * it stays buffered until the next chunk or the close flush. */
export function collectStreamedLines(carry: string, chunk: string): { carry: string; lines: string[] } {
  const parts = `${carry}${chunk}`.split(/\r?\n/)
  const tail = parts.pop() ?? ''
  return { carry: tail, lines: parts.filter((line) => line.length > 0) }
}

function parseEventLine(line: string, onEvent?: (payload: Record<string, any>) => void): void {
  const match = line.match(/ITERATIVE_MIGRATION_STATUS_V1 (\{.*\})/)
  if (!match) return
  try {
    const payload = JSON.parse(match[1]) as Record<string, any>
    if (payload && typeof payload === 'object') onEvent?.(payload)
  } catch {
    // a non-JSON status line is not progress evidence
  }
}

export function spawnIterativeStreamed(
  runDir: string,
  command: string,
  args: string[],
  cwd: string,
  timeoutMs: number,
  io: StreamAttemptIo,
  platform: StreamPlatform,
  onEvent?: (payload: Record<string, any>) => void,
  options?: { stdin?: string; onLine?: (line: string) => void; onSpawn?: (pid: number) => void },
): Promise<CaptureResult> {
  return new Promise((resolvePromise) => {
    let stdout = ''
    let stderr = ''
    let stdoutCarry = ''
    let settled = false
    let timedOut = false
    let canceled = false
    let observerFailed = false
    const observe = (callback: (() => void) | undefined): void => {
      try { callback?.() } catch (error) {
        observerFailed = true
        stderr += `\nAGENT_SESSION_PERSIST_FAILED: ${String(error)}`
        platform.killProcessTree(child)
      }
    }
    // B6: the attempt.log byte cap must hold DURING a long/chatty child, not
    // only at close — trim throttled, so a runaway stdout/stderr stream cannot
    // balloon the diagnostic artifact past MAX_LOG_BYTES before the step ends.
    let recordsSinceTrim = 0
    const trimEveryRecords = 200
    const maybeTrim = (): void => {
      recordsSinceTrim += 1
      if (recordsSinceTrim >= trimEveryRecords) {
        recordsSinceTrim = 0
        try { io.trimLog(runDir) } catch { /* best-effort */ }
      }
    }
    const commandEnv = platform.commandEnvironment(process.env)
    const invocation = platform.resolveSpawnInvocation(command, args, { env: commandEnv })
    const child = spawn(invocation.command, invocation.args, {
      cwd,
      shell: false,
      detached: platform.processTreeDetached(),
      windowsHide: true,
      windowsVerbatimArguments: invocation.windowsVerbatimArguments,
      env: commandEnv,
    })
    // Zero disables only the overall watchdog; cancellation remains active.
    child.stdin.on('error', () => { /* EPIPE from an early child exit */ })
    child.stdin.end(options?.stdin)
    if (child.pid) observe(() => options?.onSpawn?.(child.pid as number))
    const timer = timeoutMs > 0
      ? setTimeout(() => { timedOut = true; platform.killProcessTree(child) }, timeoutMs)
      : undefined
    const cancelCheck = setInterval(() => {
      if (!canceled && io.cancelRequested(runDir)) {
        canceled = true
        platform.killProcessTree(child)
      }
    }, 500)

    const flushCarry = (): void => {
      if (!stdoutCarry) return
      const line = stdoutCarry
      stdoutCarry = ''
      stdout = captureTail(stdout, `${line}\n`)
      try { io.recordLine(runDir, `${line}\n`) } catch { /* log is best-effort */ }
      maybeTrim()
      parseEventLine(line, onEvent)
      observe(() => options?.onLine?.(line))
    }

    child.stdout.on('data', (chunk: Buffer) => {
      const { carry, lines } = collectStreamedLines(stdoutCarry, platform.decodeChunk(chunk))
      stdoutCarry = carry
      // Bound the captured tail once per input chunk, not once per line.
      // Per-line copies repeatedly encoded a 1 MiB tail during chatty output.
      if (lines.length) stdout = captureTail(stdout, `${lines.join('\n')}\n`)
      // Batch ordinary output within this chunk to avoid opening and trimming
      // both journal files for every small line. Status events stay separate
      // so durable phase updates and their ordering remain unchanged.
      let plain = ''
      const flushPlain = (): void => {
        if (!plain) return
        try { io.recordLine(runDir, plain) } catch { /* log is best-effort */ }
        plain = ''
        maybeTrim()
      }
      for (const line of lines) {
        observe(() => options?.onLine?.(line))
        if (line.includes('ITERATIVE_MIGRATION_STATUS_V1 ')) {
          flushPlain()
          try { io.recordLine(runDir, `${line}\n`) } catch { /* log is best-effort */ }
          maybeTrim()
          parseEventLine(line, onEvent)
        } else {
          plain += `${line}\n`
          if (plain.length >= 64 * 1024) flushPlain()
        }
      }
      flushPlain()
      // Journal oversized plain lines verbatim without retaining an unlimited carry.
      if (Buffer.byteLength(stdoutCarry, 'utf8') > MAX_CAPTURE_BYTES) {
        stdout = captureTail(stdout, stdoutCarry)
        try { io.recordLine(runDir, stdoutCarry) } catch { /* best-effort */ }
        stdoutCarry = ''
        maybeTrim()
      }
    })
    child.stderr.on('data', (chunk: Buffer) => {
      const text = platform.decodeChunk(chunk)
      try { io.recordLine(runDir, text) } catch { /* best-effort */ }
      maybeTrim()
      stderr = captureTail(stderr, text)
    })
    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      clearInterval(cancelCheck)
      resolvePromise({ code: 1, stdout, stderr: `${stderr}${error.message}`, timedOut, canceled })
    })
    child.on('close', (code, signal) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      clearInterval(cancelCheck)
      // Flush any unterminated line so a trailing status/error is neither lost
      // from the journal nor missed as an event.
      flushCarry()
      try { io.trimLog(runDir) } catch { /* best-effort */ }
      // A kill by the watchdog leaves no exit code from the child's point of
      // view; reporting 0 here would let a timed-out begin claim success. Any
      // reason WE terminated the tree is therefore a non-zero outcome.
      const effectiveCode = timedOut || canceled || observerFailed || signal ? 1 : (code ?? 1)
      resolvePromise({ code: effectiveCode, stdout, stderr, timedOut, canceled })
    })
  })
}
