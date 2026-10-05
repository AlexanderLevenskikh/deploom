/** Durable continuation intent, bound to the existing Python run identity.
 * This record grants scheduling authority only, never verification authority. */
import { readFileSync, writeFileSync, renameSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { join } from 'node:path'

export type AutopilotRetry = { count: number; retryAt: number; error: string }
export type IterativeAutopilotState = { schemaVersion: 1; runId: string; enabled: boolean; resume: boolean; retry?: AutopilotRetry }

/** A living Python writer still owns its exact transaction after Desktop exits.
 * Never delete or age out its lock here; Python owns reclaim and validation. */
export function liveRunWriterPid(runDir: string, isAlive: (pid: number) => boolean): number | undefined {
  try {
    const lock = JSON.parse(readFileSync(join(runDir, 'run.lock'), 'utf8'))
    return Number.isInteger(lock.pid) && lock.pid > 0 && isAlive(lock.pid) ? lock.pid : undefined
  } catch { return undefined }
}

function runIdentity(runDir: string): string | undefined {
  try {
    const run = JSON.parse(readFileSync(join(runDir, 'run.json'), 'utf8'))
    return typeof run.runId === 'string' && run.runId ? run.runId : undefined
  } catch { return undefined }
}

export function readAutopilotState(runDir: string): IterativeAutopilotState | undefined {
  try {
    const state = JSON.parse(readFileSync(join(runDir, 'autopilot.json'), 'utf8'))
    if (state.schemaVersion !== 1 || typeof state.runId !== 'string' || !state.runId || state.runId !== runIdentity(runDir) || typeof state.enabled !== 'boolean' || typeof state.resume !== 'boolean' || (state.resume && !state.enabled)) return undefined
    if (state.retry && (!Number.isInteger(state.retry.count) || state.retry.count < 1 || !Number.isFinite(state.retry.retryAt) || typeof state.retry.error !== 'string')) return undefined
    return state
  } catch { return undefined }
}

export function writeAutopilotState(runDir: string, enabled: boolean, retry?: AutopilotRetry, resume = enabled): void {
  const runId = runIdentity(runDir)
  if (!runId) return // Begin has not created a durable run yet.
  const state: IterativeAutopilotState = { schemaVersion: 1, runId, enabled, resume: enabled && resume, ...(retry ? { retry } : {}) }
  const target = join(runDir, 'autopilot.json')
  const temporary = join(runDir, `.autopilot-${randomUUID()}.tmp`)
  writeFileSync(temporary, JSON.stringify(state, null, 2), 'utf8')
  renameSync(temporary, target)
}
