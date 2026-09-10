import type { FlowAction, TargetLevel, WorkspaceDetails } from './types'

export type AutopilotPolicyState = {
  projectName: string
  /** Compatibility-only planning target. It no longer gates FLOW completion. */
  target: TargetLevel
  publish: boolean
  goalSignatures: Record<string, number>
  goalCycles: number
}

export const MAX_AUTOPILOT_GOAL_CYCLES = 8
export const AUTOPILOT_ORDER: FlowAction[] = ['preflight', 'baseline', 'agent', 'generate', 'audit', 'release', 'commit-state', 'push-workspace']

export function nextAutopilotAction(details: WorkspaceDetails, state: AutopilotPolicyState): FlowAction | undefined {
  const run = details.teamState?.projects[state.projectName]
  const completed = new Set(run?.completedActions ?? [])
  const verdict = details.acceptanceVerdict

  if (completed.has('audit')) {
    // Audit is the acceptance boundary. Missing/stale/incomplete evidence is
    // UNKNOWN and therefore fail-closed; regenerating the roadmap cannot turn
    // missing audit authority into a pass.
    if (!verdict || verdict.status === 'UNKNOWN') return undefined

    if (verdict.accepted) {
      for (const action of AUTOPILOT_ORDER) {
        if (action === 'push-workspace' && !state.publish) continue
        if (['preflight', 'baseline', 'agent', 'generate', 'audit'].includes(action)) continue
        if (!completed.has(action)) return action
      }
      return undefined
    }

    // A remediation plateau belongs to this planning epoch. Never bypass it
    // with a different freshness/Yellow combination; a fresh Baseline is the
    // explicit operation that starts a new epoch.
    if (run?.autonomyPlateau) return undefined

    // Hard acceptance failure is the only reason to reopen migration after an
    // audit. The backend Supervisor may create a security-remediation residual
    // even when the old health plan has zero executable rows.
    return 'agent'
  }

  for (const action of AUTOPILOT_ORDER) {
    if (action === 'push-workspace' && !state.publish) continue
    if (!completed.has(action)) return action
  }
  return undefined
}

// Historical export name retained to keep the hook/API transition small. The
// policy is acceptance-remediation now, not health-goal seeking.
export function goalSeekingStopReason(details: WorkspaceDetails, state: AutopilotPolicyState): string | undefined {
  const run = details.teamState?.projects[state.projectName]
  const completed = new Set(run?.completedActions ?? [])
  const verdict = details.acceptanceVerdict
  if (!completed.has('audit') || !verdict || verdict.accepted) return undefined
  if (verdict.status === 'UNKNOWN') return `acceptance evidence is UNKNOWN: ${verdict.reasons.join('; ')}`
  if (run?.autonomyPlateau) return `Supervisor уже доказал plateau текущего planning epoch: ${run.autonomyPlateau.reason}`

  // Candidate/package/freshness churn is not progress. Only a different
  // cumulative merged commit or a different authoritative security verdict
  // earns another autonomous remediation cycle.
  const factsCommit = details.migrationProgress?.factsCommit || details.migrationProgress?.factsRef || '?'
  const signature = `commit=${factsCommit}:critical=${verdict.critical ?? '?'}:high=${verdict.high ?? '?'}`
  const count = (state.goalSignatures[signature] ?? 0) + 1
  state.goalSignatures[signature] = count
  state.goalCycles += 1
  if (count > 1) return `после полного remediation→verification→audit цикла не изменились ни cumulative merged commit, ни acceptance verdict: Critical=${verdict.critical ?? '?'}, High=${verdict.high ?? '?'}. Другой freshness/residual candidate сам по себе прогрессом не считается`
  if (state.goalCycles > MAX_AUTOPILOT_GOAL_CYCLES) return `исчерпан budget ${MAX_AUTOPILOT_GOAL_CYCLES} acceptance-remediation циклов с фактическим Git/security-прогрессом`
  return undefined
}
