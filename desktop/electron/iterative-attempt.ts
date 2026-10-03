// Durable attempt journal for the iterative begin/drive steps (Desktop).
//
// The Desktop starts a long begin/drive by writing an attempt RECORD BEFORE it
// spawns Python: `runDir/attempt.json` (state, attemptId, stage, progress,
// heartbeat, targetSource, last error) plus `runDir/attempt.log` (capped
// stdout/stderr of the child). The renderer polls/streams this record, so the
// very first click is observable immediately and a Desktop restart restores
// both the running state and the reason the attempt stopped. Cancelling writes
// `cancelRequested` into the record; the streaming runner checks it between
// chunks and terminates the child WITHOUT deleting any durable run state
// (begin/drive never touch checkpoints, so the last verified checkpoint is
// always preserved on retry/resume).
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { dirname, join } from 'node:path'

export type IterativeAttemptStatus = 'starting' | 'running' | 'done' | 'failed' | 'canceled'
export type IterativeTargetSource = 'none' | 'roadmap' | 'discovery'

export type IterativeDiscoveryBudget = {
  parallelism: number
  timeoutSeconds: number
  maxPackages: number
}

export type IterativeAttemptRecord = {
  attemptId: string
  projectName: string
  // P2 (#2): the attempt is scoped to the WORKSPACE, not just the project name —
  // the durable run dir is already per (workspace.path, project); carrying the
  // workspace id end-to-end stops the live event stream and the UI from
  // cross-writing two workspaces that contain the same project name.
  workspaceId?: string
  status: IterativeAttemptStatus
  stage: 'preflight' | 'begin' | 'drive' | 'agent' | 'none'
  phase?: string
  startedAt: number
  finishedAt?: number
  lastHeartbeatAt: number
  targetSource: IterativeTargetSource
  discovery?: IterativeDiscoveryBudget
  targetsCount?: number
  packageProgress?: { processed: number; total: number }
  discoveryCompleted?: boolean
  discoverySkipped?: number
  stepsDone: string[]
  reason?: string
  lastError?: string
  lastStep?: string
  runCreated: boolean
  cancelRequested?: boolean
}

export function attemptFilePath(runDir: string): string {
  return join(runDir, 'attempt.json')
}

export function attemptLogPath(runDir: string): string {
  return join(runDir, 'attempt.log')
}

const MAX_LOG_LINES = 800
const MAX_LOG_BYTES = 128 * 1024

/** Atomically write the attempt record (write + rename in the same dir). */
export function writeAttempt(runDir: string, record: IterativeAttemptRecord): IterativeAttemptRecord {
  mkdirSync(runDir, { recursive: true })
  const target = attemptFilePath(runDir)
  const tmp = join(dirname(target), `.attempt-${randomUUID()}.tmp`)
  writeFileSync(tmp, JSON.stringify(record, null, 2), 'utf8')
  renameSync(tmp, target)
  return record
}

export function readAttempt(runDir: string): IterativeAttemptRecord | undefined {
  const target = attemptFilePath(runDir)
  if (!existsSync(target)) return undefined
  try {
    const parsed = JSON.parse(readFileSync(target, 'utf8')) as IterativeAttemptRecord
    if (!parsed || typeof parsed !== 'object' || !parsed.attemptId) return undefined
    return parsed
  } catch {
    return undefined
  }
}

/** Read-modify-write the attempt record. Every written patch updates the
 * heartbeat, so a stalled child is visible in the UI instead of a silent
 * freeze. */
export function updateAttempt(runDir: string, patch: Partial<IterativeAttemptRecord>): IterativeAttemptRecord | undefined {
  const current = readAttempt(runDir)
  if (!current) return undefined
  return writeAttempt(runDir, { ...current, ...patch, lastHeartbeatAt: Date.now() })
}

/** Start a NEW attempt (fresh attemptId) and a fresh log. Only safe when no
 * durable run exists yet (run.json absent) — otherwise use `resumeAttempt`. */
export function startAttempt(
  runDir: string,
  projectName: string,
  targetSource: IterativeTargetSource,
  discovery?: IterativeDiscoveryBudget,
  targetsCount?: number,
  workspaceId?: string,
): IterativeAttemptRecord {
  const now = Date.now()
  // A NEW attempt means a fresh diagnostic log: a previous child's output must
  // never leak into the next attempt's journal.
  clearAttemptLog(runDir)
  return writeAttempt(runDir, {
    attemptId: randomUUID(),
    projectName,
    workspaceId,
    status: 'starting',
    stage: 'preflight',
    startedAt: now,
    lastHeartbeatAt: now,
    targetSource,
    discovery,
    targetsCount,
    packageProgress: { processed: 0, total: 0 },
    stepsDone: [],
    runCreated: false,
  })
}

/** Rebuild the journal after a Desktop restart: keep the ORIGINAL attemptId
 * (the source of truth for the run) and just flip status back to running. */
export function resumeAttempt(runDir: string, projectName: string, workspaceId?: string): IterativeAttemptRecord | undefined {
  const current = readAttempt(runDir)
  if (!current) return startAttempt(runDir, projectName, 'none', undefined, 0, workspaceId)
  return updateAttempt(runDir, { status: 'running', stage: 'drive', lastError: undefined, cancelRequested: false })
}

export function requestCancel(runDir: string): void {
  updateAttempt(runDir, { cancelRequested: true })
}

export function clearCancelRequest(runDir: string): void {
  const current = readAttempt(runDir)
  if (current) updateAttempt(runDir, { cancelRequested: false })
}

/** Capped append of a child output line into the run diagnostic artifact. */
export function recordAttemptLog(runDir: string, line: string): void {
  const target = attemptLogPath(runDir)
  mkdirSync(dirname(target), { recursive: true })
  appendFileSync(target, line, 'utf8')
}

/** Trim the log to the cap after writing (cheap; only called when size grows). */
export function trimAttemptLog(runDir: string): void {
  const target = attemptLogPath(runDir)
  if (!existsSync(target)) return
  try {
    if (statSync(target).size <= MAX_LOG_BYTES) return
  } catch {
    return
  }
  try {
    const text = readFileSync(target, 'utf8')
    const lines = text.split(/\r?\n/)
    const kept = lines.slice(-MAX_LOG_LINES).join('\n')
    writeFileSync(target, kept, 'utf8')
  } catch {
    /* best-effort trim */
  }
}

/** Cap-safe log tail for the UI (never ships the whole artifact). */
export function readAttemptLogTail(runDir: string, maxBytes = 16 * 1024): string {
  const target = attemptLogPath(runDir)
  if (!existsSync(target)) return ''
  try {
    const text = readFileSync(target, 'utf8')
    if (Buffer.byteLength(text, 'utf8') <= maxBytes) return text
    return `…${text.slice(-maxBytes)}\n`
  } catch {
    return ''
  }
}

export function clearAttemptLog(runDir: string): void {
  const target = attemptLogPath(runDir)
  if (existsSync(target)) rmSync(target, { force: true })
}

// Pure begin-plan decision, kept exported so the Electron contract check can
// pin the exact "no silent discovery" behaviour without spawning anything.
export type BeginPlan =
  | { kind: 'already-exists' }
  | { kind: 'in-progress' }
  | { kind: 'no-targets' } // roadmap has no concrete versions; nothing is run
  | { kind: 'discovery' } // explicit user-requested bounded discovery
  | { kind: 'start' } // concrete roadmap targets: normal begin

export function beginPlan(input: {
  runPresent: boolean
  stepInFlight: boolean
  targetsCount: number
  discoveryMode: 'auto' | 'none'
}): BeginPlan {
  if (input.runPresent) return { kind: 'already-exists' }
  if (input.stepInFlight) return { kind: 'in-progress' }
  if (input.targetsCount === 0 && input.discoveryMode !== 'auto') return { kind: 'no-targets' }
  if (input.targetsCount === 0 && input.discoveryMode === 'auto') return { kind: 'discovery' }
  return { kind: 'start' }
}
