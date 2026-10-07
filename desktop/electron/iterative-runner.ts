// Desktop step-driver for the iterative migration (increment 1).
//
// The Desktop drives ONE durable run directory through the Python CLI steps.
// All truth lives in the Python-owned durable state; this module never keeps
// state of its own, so a relaunched app resumes the exact same place
// (restart-safe, no attempt is re-spent). `status` (iterative_migration.py)
// emits the full state as JSON and writes <runDir>/status.json; the driver
// parses that and DECIDES the next Python step from BOTH run.phase and
// candidate.stage (the durable state files stay the single source of truth):
//
//   READY             -> plan-next (or audit, then finish, when the policy is
//                        satisfied / the run is terminal — R8: a satisfied
//                        policy crosses the independent audit first, UNKNOWN
//                        audit evidence never yields COMPLETE); an in-flight
//                        candidate is resumed by its stage, a consumed one
//                        (VERIFYING/ACCEPTED/REJECTED leftover) is skipped
//   BOOTSTRAP_REPAIR  -> bootstrap-materialize (no trial yet) / agent (GATE,
//                        decision.bootstrap) once the version-neutral trial
//                        exists
//   PLANNING          -> materialize
//   MATERIALIZING     -> materialize (PLANNED) / precheck (MATERIALIZED) [R2]
//   PRECHECK          -> precheck (MATERIALIZED) / verify-exact (PRECHECKED)
//   VERIFYING         -> verify-exact (R4: re-entrant on the VERIFYING stage)
//   REPAIRING         -> agent (agent works in the isolated trial on the
//                        repair request bytes, then apply-feedback re-checks
//                        its JSON)
//   TERMINAL          -> finish
//
// The agent step is a GATE: the driver tells the caller exactly which repair
// request must be answered and where the isolated trial lives, but executing
// the agent is the caller's job (injected), so the same module drives both a
// scripted fake and the real agent harness.
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

export type IterativeStep =
  | 'begin'
  | 'resume'
  | 'verify-bootstrap'
  | 'bootstrap-materialize'
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
  bootstrap?: boolean
  /** A recoverable infrastructure stop: the cause (registry/auth/offline/…)
   * may change and the user may retry afterwards; this is never COMPLETE. */
  infraBlocked?: boolean
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

// P2 (#4): the durable terminal verdict. Python writes a structured
// `run.terminalOutcome.outcome` (COMPLETE / BLOCKED_BASELINE / PARTIAL_VERIFIED /
// NO_VERIFIED_UPGRADE) and mirrors it in `run.terminal`; earlier coordination
// phases also mark `run.terminal` with transient names (NO_ACTIONABLE /
// SCOPE_EXHAUSTED / POLICY_SATISFIED_NEEDS_AUDIT). The structured outcome wins,
// the plain terminal string is the fallback.
function terminalReason(run: Record<string, any>): string {
  const structured = (run.terminalOutcome ?? null) as Record<string, any> | null
  return String(structured?.outcome ?? run.terminal ?? '')
}

function terminalSatisfied(run: Record<string, any>): boolean {
  const reason = terminalReason(run)
  return reason === 'COMPLETE'
}

/**
 * Decide the next step from the durable status payload. Pure: no fs besides
 * the caller-provided targets, so the decision table is unit-checkable.
 *
 * The phase is the run's top-level state machine; candidate.stage is the
 * sub-state of the in-flight candidate. Both come from the SAME durable files
 * (run.json + candidate.json), so a restart resumes exactly where Python
 * stopped:
 *
 *   PLANNING/MATERIALIZING+PLANNED          -> materialize (resume)
 *   MATERIALIZING/PRECHECK? + MATERIALIZED  -> precheck   (R2: never re-materialize)
 *   PRECHECK/REPAIRING? + PRECHECKED        -> verify-exact
 *   VERIFYING (+VERIFYING/REPAIRING)        -> verify-exact (R4: idempotent resume)
 *   REPAIRING/BOOTSTRAP_REPAIR (+requests)  -> agent (GATE)
 *   READY + in-flight candidate             -> resume by stage (or plan-next when
 *                                              the candidate is an accepted leftover)
 */
export function decideNextStep(
  runDir: string,
  payload: Record<string, any>,
): IterativeDecision {
  const run = (payload.run ?? {}) as Record<string, any>
  const checkpoint = (payload.activeCheckpoint ?? {}) as Record<string, any>
  const candidate = (payload.candidate ?? undefined) as Record<string, any> | undefined
  const phase = String(run.phase ?? '')
  const candidateStage = candidate ? String(candidate.stage ?? '') : ''
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
    case 'BOOTSTRAP_REPAIR': {
      // C0 failed control verification. The bootstrap repair is a two-stage
      // AGENT GATE over the C0 bytes (version-neutral): first materialize the
      // isolated trial (bootstrap-materialize), then dispatch the repair agent
      // in THAT trial BEFORE verify-bootstrap re-runs the control. The durable
      // run.bootstrapRefs says whether the trial exists yet.
      const bootstrapRefs = (run.bootstrapRefs ?? null) as Record<string, string> | null
      if (!bootstrapRefs?.workspaceRoot) {
        return {
          step: 'bootstrap-materialize',
          phase,
          reason: 'bootstrap C0 repair needs an isolated version-neutral trial; materialize it first',
        }
      }
      return {
        step: 'agent',
        phase,
        reason: repairSummaries.length > 0
          ? `bootstrap C0 repair required on ${repairSummaries.length} request(s) in the isolated trial`
          : 'bootstrap C0 repair required in the isolated trial',
        repairRequests: repairSummaries,
        bootstrap: true,
      }
    }
    case 'PLANNING':
      return { step: 'materialize', phase, reason: 'candidate is planned; materialize exact versions into the isolated trial' }
    case 'MATERIALIZING': {
      // R2: a candidate that is already MATERIALIZED must go to precheck, not
      // re-materialize (Python refuses a MATERIALIZED candidate with
      // CANDIDATE_STAGE_NOT_PLANNED).
      if (candidateStage === 'MATERIALIZED') {
        return { step: 'precheck', phase, reason: 'candidate is materialized; run the diagnostic check pass in the trial' }
      }
      // B1: a failed materialize records a durable structured attemptResult on
      // the candidate. While it is retryable the same candidate is re-invoked
      // (bounded by Python's per-sequence infra budget); a concluded / non
      // retryable result must NEVER re-materialize — that was the endless
      // hidden-install loop. Python moves a concluded candidate out of PLANNED,
      // this guard is a belt-and-braces stop.
      const attempt = (candidate?.attemptResult ?? null) as Record<string, any> | null
      if (attempt && attempt.retryable !== true) {
        return {
          step: null,
          phase,
          reason: `MATERIALIZE_CONCLUDED: ${String(attempt.code ?? attempt.classification ?? '')} — не совместимость; восстановите причину и повторите`,
          infraBlocked: attempt.recoverable === true,
        }
      }
      return { step: 'materialize', phase, reason: 'candidate is planned; materialize exact versions into the isolated trial' }
    }
    case 'PRECHECK': {
      // A passed precheck leaves phase=PRECHECK + stage=PRECHECKED; the next
      // step is the authoritative cumulative verify, never a precheck re-run
      // (precheck requires stage MATERIALIZED).
      return candidateStage === 'PRECHECKED'
        ? { step: 'verify-exact', phase, reason: 'precheck passed; run the authoritative cumulative verify' }
        : { step: 'precheck', phase, reason: 'run the diagnostic check pass in the materialized trial' }
    }
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
      // R4: verify-exact is safely re-entrant on the VERIFYING stage (Python
      // accepts PRECHECKED/REPAIRING/VERIFYING and the acceptance is an
      // idempotent transaction keyed by the durable candidate identity), so a
      // kill/restart in the middle of the verify resumes the SAME candidate.
      return { step: 'verify-exact', phase, reason: 'authoritative full verify of the same exact candidate on repaired bytes' }
    case 'TERMINAL': {
      // P2 (#4): a TERMINAL phase is NOT automatically a failure. The durable
      // run records a structured terminalOutcome (COMPLETE / BLOCKED_BASELINE /
      // PARTIAL_VERIFIED / NO_VERIFIED_UPGRADE) and a mirrored run.terminal; a
      // COMPLETE migration is satisfied (the UI shows the verified result), the
      // other terminals are honest partial/blocked (never "проверено").
      return {
        step: 'finish',
        phase,
        reason: terminalReason(run),
        satisfied: terminalSatisfied(run),
      }
    }
    case 'READY': {
      if (run.terminal) {
        return {
          step: 'finish',
          phase,
          reason: terminalReason(run),
          satisfied: terminalSatisfied(run),
        }
      }
      // B1: a completed infrastructure stop is durable on the run. The
      // coordinator STOPS with evidence instead of looping plan-next; the
      // user retries (plan-next --retry-infra) after the cause changed.
      const infraBlocked = (run.infraBlocked ?? null) as Record<string, any> | null
      if (infraBlocked) {
        return {
          step: null,
          phase,
          reason: `INFRA_BLOCKED: ${String(infraBlocked.reason ?? '')}; verified checkpoint preserved — повторите после восстановления причины`,
          infraBlocked: true,
        }
      }
      if (candidate && candidateStage) {
        // READY means the pointer already moved (phase was normalised after
        // accept/reject). A VERIFYING/REJECTED/ACCEPTED leftover is the accepted
        // candidate awaiting cleanup by the next Python step; a PLAN/MATERIAL/
        // PRECHECKED/REPAIRING leftover is genuinely in-flight work to resume.
        switch (candidateStage) {
          case 'PLANNED':
            return { step: 'materialize', phase, reason: 'in-flight candidate is planned; resume materialization' }
          case 'MATERIALIZED':
            return { step: 'precheck', phase, reason: 'in-flight candidate is materialized; run the diagnostic check pass' }
          case 'PRECHECKED':
            return { step: 'verify-exact', phase, reason: 'in-flight candidate passed precheck; run the authoritative verify' }
          case 'REPAIRING':
            return {
              step: 'agent',
              phase,
              reason: repairSummaries.length > 0
                ? `in-flight candidate needs repair on ${repairSummaries.length} request(s)`
                : 'in-flight candidate needs trial re-verification after repair',
              repairRequests: repairSummaries,
            }
          case 'VERIFYING':
          case 'ACCEPTED':
          case 'REJECTED':
            // Candidate was consumed by the acceptance/rejection transaction;
            // the next action is a fresh planning for the remaining targets.
            break
          default:
            break
        }
      }
      const satisfied = planSatisfied(readTargets(runDir), assignment)
      if (satisfied) {
        // R8: a satisfied policy is finalized only AFTER the independent audit
        // of the exact accepted checkpoint (PASS/FAIL recorded in
        // checkpoint.audit). The first decision is `audit`; after the audit has
        // written its evidence the decision is `finish`. UNKNOWN audit evidence
        // never yields a COMPLETE, so a satisfied policy always crosses the
        // audit first.
        const audit = (checkpoint.audit ?? {}) as Record<string, any>
        const auditStatus = String(audit.status ?? '')
        if (auditStatus !== 'PASS' && auditStatus !== 'FAIL') {
          return { step: 'audit', phase, reason: 'policy satisfied; run the independent audit of the accepted checkpoint before finalizing', satisfied: true }
        }
        return { step: 'finish', phase, reason: 'policy satisfied and audit recorded; finalize migration reports', satisfied: true }
      }
      return { step: 'plan-next', phase, reason: 'remaining targets are actionable; select the next cohort' }
    }
    default: {
      // Unknown phase: never invent a step; surface the raw phase for triage.
      return { step: null, phase, reason: `UNKNOWN_PHASE: ${phase}` }
    }
  }
}
