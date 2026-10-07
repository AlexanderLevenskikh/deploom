/** Electron-owned continuation: survives renderer remounts, waits through
 * provider outages, never restarts an
 * inconclusive stage, and stops at the authoritative supervisor's boundaries. */
type Scope = { workspaceId?: string; projectName: string; autopilot?: boolean }
type Input = Scope & Record<string, unknown>
type Outcome = Record<string, unknown> & { ok?: boolean }
type Step = 'begin' | 'drive' | 'agent' | 'status' | 'cancel'
type Handler = (event: unknown, input: Input) => Promise<Outcome>
export type AutopilotSummary = { stopped: string; error?: string }

type Retry = { count: number; retryAt: number; error: string }
type Persistence = {
  normalizeScope?: (scope: Scope) => Scope
  setEnabled?: (scope: Scope, enabled: boolean) => void
  readRetry?: (scope: Scope) => Retry | undefined
  writeRetry?: (scope: Scope, retry?: Retry) => void
  retryDelayMs?: (count: number) => number
  onStopped?: (scope: Scope, summary: AutopilotSummary) => void
  waitForWriter?: (scope: Scope) => number | undefined
}

export function createIterativeAutopilot(key: (scope: Scope) => string, persistence: Persistence = {}) {
  const handlers = new Map<Step, Handler>()
  const sessions = new Map<string, { canceled: boolean; enabled: boolean }>()
  const statusReads = new Map<string, Promise<Outcome>>()
  async function waitUntil(retryAt: number, session: { canceled: boolean; enabled: boolean }) {
    while (Date.now() < retryAt && session.enabled && !session.canceled) {
      await new Promise(resolve => setTimeout(resolve, Math.min(250, retryAt - Date.now())))
    }
  }
  const invoke = async (step: Step, input: Input): Promise<Outcome> => {
    const handler = handlers.get(step)
    if (!handler) throw new Error(`AUTOPILOT_HANDLER_MISSING: ${step}`)
    const session = sessions.get(key(input))
    if (session && step !== 'status') persistence.setEnabled?.(input, step !== 'begin' && session.enabled && !session.canceled)
    let retry = persistence.readRetry?.(input)
    if (retry && session?.enabled) {
      await waitUntil(retry.retryAt, session)
      if (!session.enabled || session.canceled) return { ok: false, stopped: session.canceled ? 'canceled' : 'paused' }
    }
    let count = retry?.count ?? 0
    while (true) {
      if (step === 'drive' && session?.enabled) {
        let deadline = persistence.waitForWriter?.(input)
        while (deadline !== undefined && session.enabled && !session.canceled) {
          await waitUntil(deadline, session)
          deadline = persistence.waitForWriter?.(input)
        }
        if (!session.enabled || session.canceled) return { ok: false, stopped: session.canceled ? 'canceled' : 'paused' }
      }
      const result = await handler(undefined, input)
      // Only the owning handler can mark a failed read as safe to retry.
      // Agent retries are limited to explicitly marked reads before dispatch.
      // Never replay begin, paid repairs or arbitrary mutating step failures.
      if (!['drive', 'status', 'agent'].includes(step) || result.retryable !== true || !session?.enabled || session.canceled) {
        if (result.ok === true && ['drive', 'status', 'agent'].includes(step)) persistence.writeRetry?.(input)
        return result
      }
      if (count >= 3) return { ...result, retryable: false, error: `AUTOPILOT_RETRY_EXHAUSTED: ${String(result.error || 'status unavailable')}` }
      retry = { count: ++count, retryAt: Date.now() + (persistence.retryDelayMs?.(count) ?? 5_000 * 2 ** (count - 1)), error: String(result.error || 'status unavailable') }
      persistence.writeRetry?.(input, retry)
      if (step === 'drive') input = { ...input, retryInfra: result.retryInfraPending === true, discardCandidate: result.discardCandidatePending === true }
      await waitUntil(retry.retryAt, session)
      if (!session.enabled || session.canceled) return result
    }
  }
  async function resumeWaitingAgent(outcome: Outcome, input: Input, session: { canceled: boolean; enabled: boolean }): Promise<Outcome> {
    while (outcome.waiting === true && session.enabled && !session.canceled) {
      const retryAt = Number(outcome.retryAt)
      if (!Number.isFinite(retryAt)) return { ok: false, error: 'AGENT_WAIT_DEADLINE_MISSING' }
      await waitUntil(retryAt, session)
      if (!session.enabled || session.canceled) return outcome
      outcome = await invoke('agent', input)
    }
    return outcome
  }
  async function continueRun(first: Step, input: Input, session: { canceled: boolean; enabled: boolean }) {
    const initiallyEnabled = session.enabled
    let outcome = await invoke(first, input)
    if (first === 'agent') outcome = await resumeWaitingAgent(outcome, input, session)
    persistence.setEnabled?.(input, session.enabled && !session.canceled)
    let initial = outcome
    // Explicit infrastructure retry is one user action, never an autopilot loop.
    input = { ...input, retryInfra: false, discardCandidate: false, resumeTerminal: false }
    if (!session.enabled && !initiallyEnabled) return initial
    const stopped = (reason: string, error?: string): Outcome => {
      const summary = { stopped: reason, ...(error ? { error } : {}) }
      persistence.onStopped?.(input, summary)
      return { ...initial, ...(error ? { ok: false, error } : {}), autopilot: summary }
    }
    if (session.canceled) return stopped('canceled')
    if (!session.enabled || outcome.waiting === true) return stopped('paused')
    let recoveredRepair = false
    if (first === 'agent' && outcome.ok !== true && /^FORBIDDEN_MUTATION:/.test(String(outcome.error ?? ''))) {
      outcome = await invoke('drive', { ...input, discardCandidate: true })
      initial = outcome
      recoveredRepair = true
      if (session.canceled) return stopped('canceled')
    }
    if (outcome.paused === true) return stopped('paused')
    if (outcome.ok !== true) return stopped('error', String(outcome.error || 'AUTOPILOT_STEP_FAILED'))
    if (first === 'begin') {
      if (outcome.checked) {
        if ((outcome.checked as { ok?: boolean }).ok !== true) return stopped('check-blocked')
        if (session.canceled) return stopped('canceled')
        outcome = await invoke('begin', { ...input, checkOnly: false, restart: false, discovery: { mode: 'auto' } })
        if (session.canceled) return stopped('canceled')
        if (outcome.ok !== true) return stopped('error', String(outcome.error || 'AUTOPILOT_BEGIN_FAILED'))
      }
      if (outcome.noTargets) return stopped('no-targets')
    }
    // Agent budgets and exact-request closure remain authoritative in Python.
    // Do not retry an identical gate after a nominally successful agent call.
    const gates = new Set<string>()
    let pendingDrive = first === 'drive' || recoveredRepair ? outcome : undefined
    while (!session.canceled && session.enabled) {
      const drive = pendingDrive ?? await invoke('drive', input)
      pendingDrive = undefined
      if (session.canceled) return stopped('canceled')
      if (!session.enabled) return stopped('paused')
      if (drive.ok !== true) return stopped('error', String(drive.error || 'AUTOPILOT_DRIVE_FAILED'))
      if (drive.stopped !== 'agent-gate') return stopped(String(drive.stopped || 'paused'))
      const status = await invoke('status', input)
      if (session.canceled) return stopped('canceled')
      if (!session.enabled) return stopped('paused')
      if (status.ok !== true) return stopped('error', String(status.error || 'AUTOPILOT_STATUS_FAILED'))
      const requests = drive.repairRequests as Array<{ requestId?: string }> | undefined
      const gate = JSON.stringify({ runId: drive.runId, candidateId: drive.candidateId, attemptId: drive.repairAttemptId, phase: drive.phase, requests: requests?.map(r => r.requestId).sort(), decision: status.decision })
      if (gates.has(gate)) return stopped('agent-gate', 'AUTOPILOT_REPEATED_REPAIR_GATE')
      gates.add(gate)
      const agent = await resumeWaitingAgent(await invoke('agent', input), input, session)
      if (session.canceled) return stopped('canceled')
      if (!session.enabled) return stopped('paused')
      if (agent.ok !== true) {
        if (/^FORBIDDEN_MUTATION:/.test(String(agent.error ?? ''))) {
          // Reject the trial instead of repeating paid repair on disputed bytes.
          // Python keeps the checkpoint and schedules other cohorts; the gate
          // set above still prevents an identical repair loop.
          pendingDrive = await invoke('drive', { ...input, discardCandidate: true })
          continue
        }
        if (agent.paused === true) return stopped('paused')
        return stopped('error', String(agent.error || 'AUTOPILOT_AGENT_FAILED'))
      }
    }
    return stopped(session.canceled ? 'canceled' : 'paused')
  }
  return {
    hasSession(scope: Scope) { return sessions.has(key(scope)) },
    isEnabled(scope: Scope): boolean | undefined { return sessions.get(key(scope))?.enabled },
    setEnabled(scope: Scope, enabled: boolean) {
      const session = sessions.get(key(scope))
      if (session) session.enabled = enabled
      persistence.setEnabled?.(scope, enabled)
      return { ok: true, active: Boolean(session?.enabled) }
    },
    register<E, I extends Scope, R extends object>(step: Step, handler: (event: E, input: I) => Promise<R>) {
      handlers.set(step, handler as unknown as Handler)
      return async (event: E, input: I): Promise<R> => {
        input = { ...input, ...persistence.normalizeScope?.(input) }
        const id = key(input)
        const session = sessions.get(id)
        if (step === 'status') {
          let pending = statusReads.get(id)
          if (!pending) {
            pending = handler(event, input) as Promise<Outcome>
            statusReads.set(id, pending)
          }
          try {
            const result = await pending
            return { ...result, autopilotActive: sessions.get(id)?.enabled ?? (result.autopilotActive === true) } as R
          } finally { if (statusReads.get(id) === pending) statusReads.delete(id) }
        }
        if (step === 'cancel') {
          if (session) session.canceled = true
          persistence.setEnabled?.(input, false)
          return handler(event, input)
        }
        if (session) return { ok: false, error: 'AUTOPILOT_IN_PROGRESS' } as R
        const own = { canceled: false, enabled: input.autopilot === true }
        sessions.set(id, own)
        try {
          return await continueRun(step, { ...input, autopilot: false } as Input, own) as R
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          persistence.onStopped?.(input, { stopped: 'error', error: message })
          return { ok: false, error: message, autopilot: { stopped: 'error', error: message } } as R
        } finally {
          try { persistence.setEnabled?.(input, false) } finally { sessions.delete(id) }
        }
      }
    },
  }
}
