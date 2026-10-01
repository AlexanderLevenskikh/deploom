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
//    tree, so a user cancel never waits out a 20-minute begin;
//  - treats a kill BY the watchdog (timeout or user cancel) as a NON-ZERO
//    outcome, never as success — a timed-out begin must not be able to claim
//    "C0 captured" just because the killed child never produced an exit code;
//  - never drops a line that arrives in pieces: incomplete stdout lines are
//    buffered across chunks (and flushed on close), so a status/error split
//    into two TCP writes is still parsed and journaled.
import { spawn, type ChildProcess } from 'node:child_process'

export type CaptureResult = { code: number; stdout: string; stderr: string; timedOut: boolean }

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
): Promise<CaptureResult> {
  return new Promise((resolvePromise) => {
    let stdout = ''
    let stderr = ''
    let stdoutCarry = ''
    let settled = false
    let timedOut = false
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
    const timer = setTimeout(() => { timedOut = true; platform.killProcessTree(child) }, timeoutMs)
    const cancelCheck = setInterval(() => {
      if (io.cancelRequested(runDir)) {
        timedOut = true
        platform.killProcessTree(child)
      }
    }, 500)

    const flushCarry = (): void => {
      if (!stdoutCarry) return
      const line = stdoutCarry
      stdoutCarry = ''
      stdout += `${line}\n`
      try { io.recordLine(runDir, `${line}\n`) } catch { /* log is best-effort */ }
      parseEventLine(line, onEvent)
    }

    child.stdout.on('data', (chunk: Buffer) => {
      const { carry, lines } = collectStreamedLines(stdoutCarry, platform.decodeChunk(chunk))
      stdoutCarry = carry
      for (const line of lines) {
        stdout += `${line}\n`
        try { io.recordLine(runDir, `${line}\n`) } catch { /* log is best-effort */ }
        parseEventLine(line, onEvent)
      }
    })
    child.stderr.on('data', (chunk: Buffer) => {
      const text = platform.decodeChunk(chunk)
      try { io.recordLine(runDir, text) } catch { /* best-effort */ }
      stderr += text
    })
    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      clearInterval(cancelCheck)
      resolvePromise({ code: 1, stdout, stderr: `${stderr}${error.message}`, timedOut })
    })
    child.on('close', (code) => {
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
      const effectiveCode = timedOut ? 1 : (code ?? 0)
      resolvePromise({ code: effectiveCode, stdout, stderr, timedOut })
    })
  })
}
