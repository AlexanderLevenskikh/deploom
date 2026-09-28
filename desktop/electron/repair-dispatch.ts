import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  extractRepairRequiredEnvelope,
  repairHandoffStateRelativeDir,
  type RepairRequest,
  type RepairRequiredEnvelope,
} from './repair-handoff.js'

// R5 (2026-09-28), acceptance item 3: Baseline repair dispatch / return.
//
// The Desktop Executor owns the DISPATCH side of the source/config repair
// chain and (deliberately) none of the RESOLUTION side:
//
//   Baseline REPAIR_REQUIRED terminal -> repair agent start/resume ->
//   source/config change -> FRESH authoritative re-verification ->
//   request closed by the verifier (project-green exact tuple) -> checkpoint.
//
// A repair request is never closed by package-name matching or by "no new
// regressions": the only valid close is the authoritative Baseline verifier
// resolving the EXACT request (project/mode/fingerprint) via a fresh
// project-green verify of the current snapshot (generator-side repair-resolved
// event). The Desktop observes that close through the durable handoff file and
// records the outcome in its own dispatch bookkeeping.
//
// This module is deliberately DI-shaped: the production adapter (main.ts)
// injects the real repair agent and the real Baseline CLI; integration tests
// inject fake executables for the identical call path.
//
// Restart safety: the dispatch state file
// (<settings base>/.dependency-roadmap/state/repair-dispatch-<token>.json) is
// durable. A relaunched app that sees a still-open state resumes the SAME
// repair episode (and, when a session id was captured, the same agent session)
// instead of starting from scratch.

export const DEFAULT_MAX_BASELINE_REPAIR_CYCLES = 3

export type RepairDispatchOutcome = 'repaired' | 'blocked' | 'exhausted'

export type RepairDispatchState = {
  schemaVersion: 1
  workspaceId: string
  project: string
  cycle: number
  requests: RepairRequest[]
  agentSessionId?: string
  lastTerminalRunId?: string
  dispatchedAt?: string
  completedAt?: string
  outcome?: RepairDispatchOutcome
}

function projectToken(project: string): string {
  return createHash('sha1').update(String(project)).digest('hex').slice(0, 16)
}

export function repairDispatchStateFileName(project: string): string {
  return `repair-dispatch-${projectToken(project)}.json`
}

export function repairDispatchStateRelativePath(project: string): string {
  return [...repairHandoffStateRelativeDir, repairDispatchStateFileName(project)].join('/')
}

export function repairDispatchStatePath(stateDir: string, project: string): string {
  return join(stateDir, repairDispatchStateFileName(project))
}

function isOpen(state: RepairDispatchState): boolean {
  return !state.outcome
}

export function readRepairDispatchState(stateDir: string, project: string): RepairDispatchState | undefined {
  const path = repairDispatchStatePath(stateDir, project)
  if (!existsSync(path)) return undefined
  try {
    const value = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
    if (value.schemaVersion !== 1) return undefined
    if (value.project !== project) return undefined
    const cycle = typeof value.cycle === 'number' ? value.cycle : 0
    if (!Number.isInteger(cycle) || cycle < 1) return undefined
    const rawRequests = Array.isArray(value.requests) ? value.requests : []
    const requests: RepairRequest[] = []
    for (const item of rawRequests) {
      const record = item as Record<string, unknown>
      if (typeof record !== 'object' || record === null) continue
      const fingerprint = String(record.fingerprint ?? '').trim()
      if (!fingerprint) continue
      const assignment: Record<string, string> = {}
      const rawAssignment = (record.assignment ?? {}) as Record<string, unknown>
      if (typeof rawAssignment === 'object' && rawAssignment !== null) {
        for (const [name, version] of Object.entries(rawAssignment)) assignment[String(name)] = String(version)
      }
      requests.push({
        requestId: String(record.requestId ?? ''),
        project: String(record.project ?? ''),
        mode: String(record.mode ?? ''),
        assignment,
        fingerprint,
        snapshotIdentity: String(record.snapshotIdentity ?? ''),
        failingCommands: Array.isArray(record.failingCommands)
          ? (record.failingCommands as Array<{ command?: unknown; exitCode?: unknown }>)
            .map((failure) => ({ command: String(failure.command ?? ''), exitCode: Number(failure.exitCode ?? 0) }))
            .filter((failure) => Boolean(failure.command))
          : [],
        diagnosticsTail: String(record.diagnosticsTail ?? ''),
        reason: String(record.reason ?? 'project'),
        ...(typeof record.disposition === 'string' ? { disposition: record.disposition } : {}),
      })
    }
    return {
      schemaVersion: 1,
      workspaceId: String(value.workspaceId ?? ''),
      project,
      cycle,
      requests,
      ...(typeof value.agentSessionId === 'string' && value.agentSessionId ? { agentSessionId: value.agentSessionId } : {}),
      ...(typeof value.lastTerminalRunId === 'string' && value.lastTerminalRunId ? { lastTerminalRunId: value.lastTerminalRunId } : {}),
      ...(typeof value.dispatchedAt === 'string' ? { dispatchedAt: value.dispatchedAt } : {}),
      ...(typeof value.completedAt === 'string' ? { completedAt: value.completedAt } : {}),
      ...(typeof value.outcome === 'string' ? { outcome: value.outcome as RepairDispatchOutcome } : {}),
    }
  } catch {
    return undefined
  }
}

export function writeRepairDispatchState(stateDir: string, state: RepairDispatchState): string {
  mkdirSync(stateDir, { recursive: true })
  const path = repairDispatchStatePath(stateDir, state.project)
  writeFileSync(path, `${JSON.stringify(state, null, 2)}\n`, 'utf8')
  return path
}

export function clearRepairDispatchState(stateDir: string, project: string): void {
  const path = repairDispatchStatePath(stateDir, project)
  try {
    if (existsSync(path)) rmSync(path, { force: true })
  } catch {
    /* best-effort bookkeeping cleanup never fails the stage */
  }
}

/** A repair-required terminal is the only Desktop-side trigger for dispatch. */
export function routeNeedsRepair(envelope: RepairRequiredEnvelope | undefined): boolean {
  return Boolean(envelope && envelope.terminalStatus === 'REPAIR_REQUIRED')
}

/**
 * Episode bookkeeping. A still-OPEN previous state means a restart: the same
 * episode continues (same cycle, session id carried for resume). A closed/
 * absent previous starts a new episode. The targeted request set is taken from
 * the newest terminal envelope when it carries requests, otherwise inherited.
 */
export function nextRepairCycleState(input: {
  previous: RepairDispatchState | undefined
  envelope: RepairRequiredEnvelope
  workspaceId: string
  project: string
}): RepairDispatchState {
  const previous = input.previous
  const resumed = previous !== undefined && isOpen(previous)
  const requests = input.envelope.requests.length ? input.envelope.requests : (previous?.requests ?? [])
  return {
    schemaVersion: 1,
    workspaceId: input.workspaceId,
    project: input.project,
    cycle: resumed ? previous.cycle : (previous?.cycle ?? 0) + 1,
    requests,
    agentSessionId: resumed ? previous.agentSessionId : undefined,
    lastTerminalRunId: input.envelope.runId || previous?.lastTerminalRunId,
    dispatchedAt: new Date().toISOString(),
  }
}

/**
 * The one request identity that may close a repair ticket: project + mode +
 * assignment fingerprint (the exact tuple). Name/version/snapshot/commands are
 * part of the tuple; anything less would be the name-matching the R5 review
 * rejected.
 */
export function sameRepairTuple(left: RepairRequest, right: RepairRequest): boolean {
  if (left.project !== right.project || left.mode !== right.mode) return false
  if (left.fingerprint && right.fingerprint) return left.fingerprint === right.fingerprint
  const leftPairs = Object.entries(left.assignment).sort()
  const rightPairs = Object.entries(right.assignment).sort()
  if (leftPairs.length !== rightPairs.length) return false
  return leftPairs.every(([name, version], index) => rightPairs[index]?.[0] === name && rightPairs[index]?.[1] === version)
}

/**
 * The return-point check after a fresh authoritative re-verification. Repaired
 * only when (a) the run no longer ends REPAIR_REQUIRED and (b) EVERY request
 * this episode targeted is gone from the durable open set. A surviving request
 * of the same name but a different version/snapshot (different fingerprint) is
 * a DIFFERENT ticket and keeps the episode open.
 */
export function repairReturnPoint(
  state: RepairDispatchState,
  currentOpenRequests: readonly RepairRequest[],
  reVerificationEnvelope: RepairRequiredEnvelope | undefined,
): { repaired: boolean; stillOpen: RepairRequest[] } {
  const targetedGone = state.requests.every(
    (target) => !currentOpenRequests.some((open) => sameRepairTuple(open, target)),
  )
  const stillOpen = currentOpenRequests.filter((open) =>
    state.requests.some((target) => sameRepairTuple(open, target)),
  )
  return { repaired: targetedGone && !routeNeedsRepair(reVerificationEnvelope), stillOpen }
}

export type RepairDispatchRunInput = {
  envelope: RepairRequiredEnvelope
  previous: RepairDispatchState | undefined
  stateDir: string
  workspaceId: string
  project: string
  maxCycles?: number
  onEvent: (line: string) => void
  dispatchRepairAgent: (input: {
    attempt: number
    requests: RepairRequest[]
    resumeSessionId?: string
    state: RepairDispatchState
  }) => Promise<{ status: 'repaired' | 'partial' | 'blocked'; reason: string; sessionId?: string }>
  runReVerification: (input: { attempt: number }) => Promise<{ output: string }>
  readOpenRequests: () => RepairRequest[]
}

export type RepairDispatchRunResult = {
  outcome: RepairDispatchOutcome
  state: RepairDispatchState
  cyclesUsed: number
}

/**
 * The R5 acceptance chain, driven by injected executors:
 *
 *   REPAIR_REQUIRED terminal -> nextRepairCycleState (restart-aware) ->
 *   repair agent start/resume -> checkpoint of dispatch state ->
 *   FRESH authoritative re-verification -> repairReturnPoint ->
 *   repaired/blocked/exhausted outcome persisted.
 *
 * The re-verification is deliberately a NEW Fresh Baseline run (never a
 * checkpoint resume): a repair changed the source identity, and the previous
 * run's already-verified artifacts are preserved on disk independently of it.
 */
export async function runBaselineRepairCycles(input: RepairDispatchRunInput): Promise<RepairDispatchRunResult> {
  const maxCycles = input.maxCycles ?? DEFAULT_MAX_BASELINE_REPAIR_CYCLES
  let state = nextRepairCycleState({
    previous: input.previous,
    envelope: input.envelope,
    workspaceId: input.workspaceId,
    project: input.project,
  })
  writeRepairDispatchState(input.stateDir, state)

  for (let attempt = 1; attempt <= maxCycles; attempt += 1) {
    const resume = state.agentSessionId ? `; resume session=${state.agentSessionId}` : ''
    input.onEvent(
      `Baseline repair dispatch ${state.cycle}/${maxCycles} (attempt ${attempt}): ${state.requests.length} repair-запрос(ов) в открытом handoff${resume}.`,
    )
    const agent = await input.dispatchRepairAgent({
      attempt,
      requests: state.requests,
      resumeSessionId: state.agentSessionId,
      state,
    })
    state = {
      ...state,
      ...(agent.sessionId ? { agentSessionId: agent.sessionId } : {}),
      dispatchedAt: new Date().toISOString(),
    }
    writeRepairDispatchState(input.stateDir, state)

    if (agent.status === 'blocked') {
      input.onEvent(`Repair agent blocked: ${agent.reason}`)
      state = { ...state, completedAt: new Date().toISOString(), outcome: 'blocked' }
      writeRepairDispatchState(input.stateDir, state)
      return { outcome: 'blocked', state, cyclesUsed: attempt }
    }

    input.onEvent(
      `Repair agent завершил (${agent.status}); запускаю свежую авторитетную повторную верификацию Baseline.`,
    )
    const reVerification = await input.runReVerification({ attempt })
    const envelopeAfter = extractRepairRequiredEnvelope(reVerification.output)
    const currentOpen = input.readOpenRequests()
    const returnPoint = repairReturnPoint(state, currentOpen, envelopeAfter)
    if (returnPoint.repaired) {
      input.onEvent(
        'Повторная верификация проекта зелёная: repair-запросы закрыты авторитетным verifier (project-green точный tuple); checkpoint сохранён.',
      )
      state = { ...state, completedAt: new Date().toISOString(), outcome: 'repaired' }
      writeRepairDispatchState(input.stateDir, state)
      return { outcome: 'repaired', state, cyclesUsed: attempt }
    }
    input.onEvent(
      `Повторная верификация всё ещё требует repair (terminal=${envelopeAfter?.terminalStatus ?? '-'}, открыто запросов=${currentOpen.length}, целей эпизода открыто=${returnPoint.stillOpen.length}).`,
    )
  }

  state = { ...state, completedAt: new Date().toISOString(), outcome: 'exhausted' }
  writeRepairDispatchState(input.stateDir, state)
  return { outcome: 'exhausted', state, cyclesUsed: maxCycles }
}
