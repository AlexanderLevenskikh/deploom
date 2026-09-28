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

// R6 review P1#2: the durable state carries the episode PHASE so a relaunched
// app can distinguish "the agent was still running" (resume the same session)
// from "the agent finished and we were awaiting the authoritative
// re-verification" (go straight back to re-verifying, no new dispatch). The
// durable attemptsUsed credits each dispatch attempt BEFORE the agent runs, so
// restarts can never re-run the episode more than maxCycles times in total.
export type RepairDispatchPhase = 'agent-running' | 'verification-pending'

export type RepairDispatchState = {
  schemaVersion: 1
  workspaceId: string
  project: string
  cycle: number
  requests: RepairRequest[]
  attemptsUsed: number
  phase?: RepairDispatchPhase
  agentSessionId?: string
  lastTerminalRunId?: string
  // R6 review P1#1: the isolated repair checkout is the work surface of the
  // episode. The original checkout is never mutated by the agent; every
  // authoritative re-verification runs against the repair checkout through the
  // repair settings file, with the generator's fail-closed repair-capture
  // authorization anchored to repairSourceCommit.
  repairCheckoutPath?: string
  repairSourceCommit?: string
  repairSettingsPath?: string
  dispatchedAt?: string
  completedAt?: string
  outcome?: RepairDispatchOutcome
}

function projectToken(project: string): string {
  return createHash('sha1').update(String(project)).digest('hex').slice(0, 16)
}

export function repairCheckoutDirName(project: string): string {
  return `repair-checkout-${projectToken(project)}`
}

export function repairSettingsFileName(project: string): string {
  return `repair-settings-${projectToken(project)}.json`
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
    const attemptsUsed = typeof value.attemptsUsed === 'number' && Number.isInteger(value.attemptsUsed) && value.attemptsUsed >= 0
      ? value.attemptsUsed
      : 0
    const phase = value.phase === 'agent-running' || value.phase === 'verification-pending' ? value.phase : undefined
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
      attemptsUsed,
      ...(phase ? { phase } : {}),
      ...(typeof value.agentSessionId === 'string' && value.agentSessionId ? { agentSessionId: value.agentSessionId } : {}),
      ...(typeof value.lastTerminalRunId === 'string' && value.lastTerminalRunId ? { lastTerminalRunId: value.lastTerminalRunId } : {}),
      ...(typeof value.repairCheckoutPath === 'string' && value.repairCheckoutPath ? { repairCheckoutPath: value.repairCheckoutPath } : {}),
      ...(typeof value.repairSourceCommit === 'string' && value.repairSourceCommit ? { repairSourceCommit: value.repairSourceCommit } : {}),
      ...(typeof value.repairSettingsPath === 'string' && value.repairSettingsPath ? { repairSettingsPath: value.repairSettingsPath } : {}),
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
  // R6 review P1#1: the episode's durable artifacts are located by the paths the
  // state file itself recorded (the repair checkout may live inside stateDir and
  // the repair settings file next to the ORIGINAL settings -- not inside
  // stateDir). Read them before removing the state file.
  let recordedCheckout: string | undefined
  let recordedSettings: string | undefined
  try {
    if (existsSync(path)) {
      const parsed = JSON.parse(readFileSync(path, 'utf8')) as {
        repairCheckoutPath?: unknown
        repairSettingsPath?: unknown
      }
      if (typeof parsed.repairCheckoutPath === 'string' && parsed.repairCheckoutPath) recordedCheckout = parsed.repairCheckoutPath
      if (typeof parsed.repairSettingsPath === 'string' && parsed.repairSettingsPath) recordedSettings = parsed.repairSettingsPath
    }
  } catch {
    /* best-effort bookkeeping cleanup never fails the stage */
  }
  try {
    if (existsSync(path)) rmSync(path, { force: true })
  } catch {
    /* best-effort bookkeeping cleanup never fails the stage */
  }
  // R6 review P1#1: an episode that is over has no need for its isolated repair
  // checkout or its repair settings file; remove them best-effort. The repair's
  // durable legacy is the sealed snapshot the authoritative verifier published,
  // not this disposable clone.
  try {
    const checkoutDir = recordedCheckout ?? join(stateDir, repairCheckoutDirName(project))
    if (existsSync(checkoutDir)) rmSync(checkoutDir, { recursive: true, force: true })
  } catch {
    /* best-effort cleanup */
  }
  try {
    const settingsFile = recordedSettings ?? join(stateDir, repairSettingsFileName(project))
    if (existsSync(settingsFile)) rmSync(settingsFile, { force: true })
  } catch {
    /* best-effort cleanup */
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
    // A restart resumes the episode's durable attempt/phase accounting instead
    // of silently giving it a fresh budget.
    attemptsUsed: resumed ? previous.attemptsUsed : 0,
    phase: resumed ? previous.phase : undefined,
    agentSessionId: resumed ? previous.agentSessionId : undefined,
    lastTerminalRunId: input.envelope.runId || previous?.lastTerminalRunId,
    ...(resumed && previous.repairCheckoutPath ? { repairCheckoutPath: previous.repairCheckoutPath } : {}),
    ...(resumed && previous.repairSourceCommit ? { repairSourceCommit: previous.repairSourceCommit } : {}),
    ...(resumed && previous.repairSettingsPath ? { repairSettingsPath: previous.repairSettingsPath } : {}),
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
  // R6 review P1#1: the source repo and branch the episode's repair checkout is
  // cloned from (never mutated itself), plus the injected creator of the
  // isolated repair checkout + repair settings file.
  originalPath: string
  sourceBranch: string
  maxCycles?: number
  onEvent: (line: string) => void
  ensureRepairCheckout: (input: {
    originalPath: string
    sourceBranch: string
    stateDir: string
    project: string
  }) => Promise<{ path: string; sourceCommit: string; settingsPath: string }>
  dispatchRepairAgent: (input: {
    attempt: number
    requests: RepairRequest[]
    resumeSessionId?: string
    state: RepairDispatchState
    // R6 review P1#2: the agent reports its session id while it is still
    // running (first output line, not process exit). The dispatcher persists it
    // durably at that moment so a mid-agent crash resumes the SAME session.
    onSessionId?: (sessionId: string) => void
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
 *   isolated repair checkout (P1#1) -> repair agent start/resume ->
 *   durable checkpoint of dispatch state (phase + session id as soon as they
 *   appear, P1#2) -> FRESH authoritative re-verification -> repairReturnPoint ->
 *   repaired/blocked/exhausted outcome persisted.
 *
 * The re-verification is deliberately a NEW Fresh Baseline run (never a
 * checkpoint resume): a repair changed the source identity, and the previous
 * run's already-verified artifacts are preserved on disk independently of it.
 * The re-verification runs against the ISOLATED repair checkout (repair
 * settings file + repair-capture authorization), never against the original
 * source checkout.
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

  // R6 review P1#1: the work surface of the episode is an isolated repair
  // checkout cloned once from the ORIGINAL project at the exact source commit
  // the episode was opened against. Created once, reused across restarts (the
  // agent's previous edits live inside it). The pinned source commit anchors
  // the generator's fail-closed repair-capture authorization.
  if (!state.repairCheckoutPath || !existsSync(state.repairCheckoutPath)) {
    const checkout = await input.ensureRepairCheckout({
      originalPath: input.originalPath,
      sourceBranch: input.sourceBranch,
      stateDir: input.stateDir,
      project: input.project,
    })
    state = {
      ...state,
      repairCheckoutPath: checkout.path,
      repairSourceCommit: checkout.sourceCommit,
      repairSettingsPath: checkout.settingsPath,
    }
    writeRepairDispatchState(input.stateDir, state)
  }
  if (!state.repairSourceCommit || !state.repairSettingsPath) {
    throw new Error('BASELINE_REPAIR_EPISODE_INCOMPLETE: repair checkout created without pinned commit/settings file')
  }

  // R6 review P1#2: a relaunched app restores the durable phase. Three
  // start-of-iteration states are distinguished:
  //   idle                -> begin a NEW attempt (attemptsUsed + 1), credited
  //                           BEFORE the agent runs; refused when the budget is
  //                           exhausted.
  //   agent-running       -> the CURRENT attempt's agent did not finish: RESUME
  //                           it with the same session id WITHOUT consuming a
  //                           new attempt -- even when attemptsUsed has already
  //                           reached maxCycles, the in-flight attempt is given
  //                           its chance to finish.
  //   verification-pending-> the CURRENT attempt's agent finished; RESUME its
  //                           authoritative re-verification (attempt =
  //                           attemptsUsed, not attemptsUsed + 1 -- no new
  //                           attempt begins).
  while (state.phase === 'agent-running' || state.phase === 'verification-pending' || state.attemptsUsed < maxCycles) {
    const resumingAgent = state.phase === 'agent-running'
    const resumingVerification = state.phase === 'verification-pending'
    const attempt = resumingAgent || resumingVerification ? state.attemptsUsed : state.attemptsUsed + 1

    if (!resumingAgent && !resumingVerification) {
      // A new attempt: credit it and persist the phase BEFORE the agent runs,
      // so a crash mid-agent cannot re-run the episode more than maxCycles
      // times across restarts (durable attemptsUsed).
      state = {
        ...state,
        phase: 'agent-running',
        attemptsUsed: attempt,
        dispatchedAt: new Date().toISOString(),
      }
      writeRepairDispatchState(input.stateDir, state)
    }

    if (!resumingVerification) {
      const resume = state.agentSessionId ? `; resume session=${state.agentSessionId}` : ''
      input.onEvent(
        `Baseline repair dispatch ${state.cycle}/${maxCycles} (attempt ${attempt})${resumingAgent ? ' [resume после restart текущей попытки]' : ''}: ${state.requests.length} repair-запрос(ов) в открытом handoff${resume}.`,
      )
      const agent = await input.dispatchRepairAgent({
        attempt,
        requests: state.requests,
        resumeSessionId: state.agentSessionId,
        state,
        onSessionId: (sessionId) => {
          // Persist the id the moment it is known -- while the agent is still
          // running -- so a crash after this point resumes the SAME session.
          state = { ...state, agentSessionId: sessionId, phase: 'agent-running' }
          writeRepairDispatchState(input.stateDir, state)
        },
      })
      state = {
        ...state,
        ...(agent.sessionId ? { agentSessionId: agent.sessionId } : {}),
        dispatchedAt: new Date().toISOString(),
      }
      if (state.agentSessionId !== agent.sessionId) writeRepairDispatchState(input.stateDir, state)
      if (agent.status === 'blocked') {
        input.onEvent(`Repair agent blocked: ${agent.reason}`)
        state = { ...state, completedAt: new Date().toISOString(), outcome: 'blocked' }
        writeRepairDispatchState(input.stateDir, state)
        return { outcome: 'blocked', state, cyclesUsed: attempt }
      }

      input.onEvent(
        `Repair agent завершил (${agent.status}); запускаю свежую авторитетную повторную верификацию Baseline.`,
      )
    }
    // Persist the phase before the re-verification so a crash mid-re-verify
    // restores straight to it (no new dispatch) on the next launch.
    state = { ...state, phase: 'verification-pending' }
    writeRepairDispatchState(input.stateDir, state)
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
    // The credited attempt is consumed. A follow-up attempt (same episode,
    // same cycle) dispatches the agent again from the same repair checkout.
    state = { ...state, phase: undefined }
    writeRepairDispatchState(input.stateDir, state)
  }

  state = { ...state, completedAt: new Date().toISOString(), outcome: 'exhausted' }
  writeRepairDispatchState(input.stateDir, state)
  return { outcome: 'exhausted', state, cyclesUsed: Math.max(state.attemptsUsed, maxCycles) }
}
