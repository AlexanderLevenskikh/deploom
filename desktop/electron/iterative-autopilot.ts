/** Electron-owned continuation: survives renderer remounts, never restarts an
 * inconclusive stage, and stops at the authoritative supervisor's boundaries. */
type Scope = { workspaceId?: string; projectName: string; autopilot?: boolean }
type Input = Scope & Record<string, unknown>
type Outcome = Record<string, unknown> & { ok?: boolean }
type Step = 'begin' | 'drive' | 'agent' | 'status' | 'cancel'
type Handler = (event: unknown, input: Input) => Promise<Outcome>
export type AutopilotSummary = { stopped: string; error?: string }

export function createIterativeAutopilot(key: (scope: Scope) => string) {
  const handlers = new Map<Step, Handler>()
  const sessions = new Map<string, { canceled: boolean }>()
  const invoke = (step: Step, input: Input) => {
    const handler = handlers.get(step)
    if (!handler) throw new Error(`AUTOPILOT_HANDLER_MISSING: ${step}`)
    return handler(undefined, input)
  }
  async function continueRun(first: Step, input: Input, session: { canceled: boolean }) {
    let outcome = await invoke(first, input)
    const initial = outcome
    const stopped = (reason: string, error?: string): Outcome => ({ ...initial, autopilot: { stopped: reason, ...(error ? { error } : {}) } })
    if (session.canceled) return stopped('canceled')
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
    let pendingDrive = first === 'drive' ? outcome : undefined
    while (!session.canceled) {
      const drive = pendingDrive ?? await invoke('drive', input)
      pendingDrive = undefined
      if (session.canceled) return stopped('canceled')
      if (drive.ok !== true) return stopped('error', String(drive.error || 'AUTOPILOT_DRIVE_FAILED'))
      if (drive.stopped !== 'agent-gate') return stopped(String(drive.stopped || 'paused'))
      const status = await invoke('status', input)
      if (session.canceled) return stopped('canceled')
      if (status.ok !== true) return stopped('error', String(status.error || 'AUTOPILOT_STATUS_FAILED'))
      const requests = drive.repairRequests as Array<{ requestId?: string }> | undefined
      const gate = JSON.stringify({ phase: drive.phase, requests: requests?.map(r => r.requestId).sort(), decision: status.decision })
      if (gates.has(gate)) return stopped('agent-gate', 'AUTOPILOT_REPEATED_REPAIR_GATE')
      gates.add(gate)
      const agent = await invoke('agent', input)
      if (session.canceled) return stopped('canceled')
      if (agent.ok !== true) return stopped('error', String(agent.error || 'AUTOPILOT_AGENT_FAILED'))
    }
    return stopped('canceled')
  }
  return {
    register<E, I extends Scope, R extends object>(step: Step, handler: (event: E, input: I) => Promise<R>) {
      handlers.set(step, handler as unknown as Handler)
      return async (event: E, input: I): Promise<R> => {
        const id = key(input)
        const session = sessions.get(id)
        if (step === 'status') {
          const result = await handler(event, input)
          return { ...result, autopilotActive: sessions.has(id) } as R
        }
        if (step === 'cancel') {
          if (session) session.canceled = true
          return handler(event, input)
        }
        if (session) return { ok: false, error: 'AUTOPILOT_IN_PROGRESS' } as R
        if (!input.autopilot) return handler(event, input)
        const own = { canceled: false }
        sessions.set(id, own)
        try {
          return await continueRun(step, { ...input, autopilot: false } as Input, own) as R
        } catch (error) {
          return { ok: false, error: error instanceof Error ? error.message : String(error), autopilot: { stopped: 'error' } } as R
        } finally {
          sessions.delete(id)
        }
      }
    },
  }
}
