// Desktop step-driver for the iterative migration (increment 1).
//
// The Desktop drives ONE durable run directory through the Python CLI steps.
// All truth lives in the Python-owned durable state; this module never keeps
// state of its own, so a relaunched app resumes the exact same place
// (restart-safe, no attempt is re-spent). `status` (iterative_migration.py)
// emits the full state as JSON and writes <runDir>/status.json; the driver
// parses that and DECIDES the next Python step:
//
//   READY          -> plan-next (or finish when the policy is satisfied /
//                     the run is terminal)
//   BOOTSTRAP_REPAIR-> verify-bootstrap
//   PLANNING       -> materialize          MATERIALIZING -> materialize (resume)
//   PRECHECK       -> precheck             VERIFYING     -> verify-exact
//   REPAIRING      -> agent                (agent works in the isolated trial
//                                           on the repair request bytes, then
//                                           apply-feedback re-checks its JSON)
//   TERMINAL       -> finish
//
// The agent step is a GATE: the driver tells the caller exactly which repair
// request must be answered and where the isolated trial lives, but executing
// the agent is the caller's job (injected), so the same module drives both a
// scripted fake and the real agent harness.
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

export type IterativeStep =
  | 'begin'
  | 'verify-bootstrap'
  | 'plan-next'
  | 'materialize'
  | 'precheck'
  | 'verify-exact'
  | 'apply-feedback'
  | 'finish'
  | 'audit'

export type IterativeDecision = {
  step: IterativeStep | 'agent' | null
  phase: string
  reason: string
  repairRequests?: Array<{ requestId: string; summary: string }>
  satisfied?: boolean
}

export const ITERATIVE_STATUS_FILENAME = 'status.json'

export function iterativeStatusInvocation(
  runDir: string,
  scriptPath: string,
  python: string = 'python',
): { command: string; args: string[] } {
  // --run-dir is a TOP-LEVEL argument of the Python CLI, before the subcommand.
  return { command: python, args: [scriptPath, '--run-dir', runDir, 'status'] }
}

export function iterativeStepInvocation(
  runDir: string,
  step: Exclude<IterativeStep, 'agent'>,
  scriptPath: string,
  python: string = 'python',
): { command: string; args: string[] } {
  // --run-dir is a TOP-LEVEL argument of the Python CLI, before the subcommand.
  return { command: python, args: [scriptPath, '--run-dir', runDir, step] }
}

export function parseIterativeStatusPayload(text: string): Record<string, any> | undefined {
  // `status` prints the stable payload JSON directly; the envelope line (when
  // streaming stdout/stderr) carries the statusPath for re-reading.
  const envelope = text.match(/ITERATIVE_MIGRATION_STATUS_V1 (\{.*\})/)
  if (envelope) {
    try {
      const parsed = JSON.parse(envelope[1]) as Record<string, any>
      if (parsed.event === 'status.ready' && typeof parsed.statusPath === 'string') return undefined
    } catch {
      // fall through to direct JSON parsing below
    }
  }
  const trimmed = text.trim()
  if (!trimmed.startsWith('{')) return undefined
  try {
    const parsed = JSON.parse(trimmed) as Record<string, any>
    return parsed && typeof parsed === 'object' && parsed.run ? parsed : undefined
  } catch {
    return undefined
  }
}

export function readIterativeStatus(runDir: string): Record<string, any> | undefined {
  const path = join(runDir, ITERATIVE_STATUS_FILENAME)
  if (!existsSync(path)) return undefined
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as Record<string, any>
    return parsed && typeof parsed === 'object' && parsed.run ? parsed : undefined
  } catch {
    return undefined
  }
}

function planSatisfied(targets: Record<string, string>, assignment: Record<string, string> | undefined): boolean {
  const names = Object.keys(targets ?? {})
  if (names.length === 0) return false
  return names.every((name) => `${assignment?.[name] ?? ''}` === `${targets[name]}`)
}

function readTargets(runDir: string): Record<string, string> {
  const configPath = join(runDir, 'run-config.json')
  if (!existsSync(configPath)) return {}
  try {
    const config = JSON.parse(readFileSync(configPath, 'utf8')) as Record<string, any>
    const raw = config.targets
    if (typeof raw !== 'object' || raw === null) return {}
    const targets: Record<string, string> = {}
    for (const [name, version] of Object.entries(raw)) targets[name] = `${version}`
    return targets
  } catch {
    return {}
  }
}

/** Decide the next step from the durable status payload. Pure: no fs besides
 * the caller-provided targets, so the decision table is unit-checkable. */
export function decideNextStep(
  runDir: string,
  payload: Record<string, any>,
): IterativeDecision {
  const run = (payload.run ?? {}) as Record<string, any>
  const checkpoint = (payload.activeCheckpoint ?? {}) as Record<string, any>
  const candidate = (payload.candidate ?? undefined) as Record<string, any> | undefined
  const phase = String(run.phase ?? '')
  const assignment = (checkpoint.fullAssignment ?? undefined) as Record<string, string> | undefined
  const repairRequests = Array.isArray(payload.openRepairRequests) ? payload.openRepairRequests : []
  const repairSummaries = repairRequests
    .map((item) => {
      const request = item as Record<string, any>
      const summary = String(request.summary ?? request.requestId ?? '')
      return summary ? { requestId: String(request.requestId ?? ''), summary } : undefined
    })
    .filter((item): item is { requestId: string; summary: string } => Boolean(item))

  switch (phase) {
    case '':
      return { step: 'begin', phase, reason: 'NO_RUN: run.json is absent; capture C0' }
    case 'BOOTSTRAP_REPAIR':
      return { step: 'verify-bootstrap', phase, reason: 'C0 needs a control re-verify after bootstrap repair' }
    case 'PLANNING':
      return { step: 'materialize', phase, reason: 'candidate is planned; materialize exact versions into the isolated trial' }
    case 'MATERIALIZING':
      return { step: 'materialize', phase, reason: 'materialization was interrupted; resume from durable candidate state' }
    case 'PRECHECK':
      return { step: 'precheck', phase, reason: 'run the diagnostic check pass in the materialized trial' }
    case 'REPAIRING': {
      // The trial needs agent work on the repair-request bytes. This is a GATE:
      // the decision names the exact request(s); executing the agent is the
      // caller's job. When no repair request is open the agent step must still
      // produce feedback for apply-feedback to accept.
      return {
        step: 'agent',
        phase,
        reason: repairSummaries.length > 0
          ? `agent repair required on ${repairSummaries.length} request(s) in the isolated trial`
          : 'trial re-verification required after repair',
        repairRequests: repairSummaries,
      }
    }
    case 'VERIFYING':
      return { step: 'verify-exact', phase, reason: 'authoritative full verify of the same exact candidate on repaired bytes' }
    case 'TERMINAL':
      return { step: 'finish', phase, reason: String(run.terminal ?? ''), satisfied: false }
    case 'READY': {
      if (run.terminal) {
        return { step: 'finish', phase, reason: String(run.terminal), satisfied: false }
      }
      if (candidate && candidate.stage && candidate.stage !== 'ACCEPTED') {
        return { step: 'precheck', phase, reason: `in-flight candidate stage ${String(candidate.stage)}` }
      }
      const satisfied = planSatisfied(readTargets(runDir), assignment)
      if (satisfied) {
        return { step: 'finish', phase, reason: 'policy satisfied; finalize migration reports', satisfied: true }
      }
      return { step: 'plan-next', phase, reason: 'remaining targets are actionable; select the next cohort' }
    }
    default: {
      // Unknown phase: never invent a step; surface the raw phase for triage.
      return { step: null, phase, reason: `UNKNOWN_PHASE: ${phase}` }
    }
  }
}
