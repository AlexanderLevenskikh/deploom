import type { FlowAction, TargetLevel, WorkspaceDetails } from './types'

export type AutopilotPolicyState = {
  projectName: string
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
  const closure = details.targetClosure
  if (completed.has('audit') && !closure) return 'generate'
  // A backend autonomy plateau is a durable planning-epoch fact, not merely a
  // log line. After generate+audit refresh the deterministic evidence, never
  // re-enter the same agent/planner path. Best-effort release remains allowed
  // only when targetClosure explicitly proves it safe. A fresh baseline clears
  // the plateau and starts a new planning epoch.
  if (completed.has('audit') && closure && !closure.reached && run?.autonomyPlateau) {
    if (!closure.bestEffortReleaseEligible) return undefined
    for (const action of AUTOPILOT_ORDER) {
      if (action === 'push-workspace' && !state.publish) continue
      if (['preflight', 'baseline', 'agent', 'generate', 'audit'].includes(action)) continue
      if (!completed.has(action)) return action
    }
    return undefined
  }
  // Keep goal-seeking only while the deterministic roadmap still has work the
  // executor can actually perform. An unmet mathematical target with zero
  // executable actions is a best-effort/handoff state, not a reason to bounce
  // forever between audit and agent.
  if (completed.has('audit') && closure && !closure.reached && closure.remainingPackages.length > 0) return 'agent'
  if (completed.has('audit') && closure && !closure.reached && closure.remainingPackages.length === 0 && !closure.bestEffortReleaseEligible) return undefined
  for (const action of AUTOPILOT_ORDER) {
    if (action === 'push-workspace' && !state.publish) continue
    if (!completed.has(action)) return action
  }
  return undefined
}

export function goalSeekingStopReason(details: WorkspaceDetails, state: AutopilotPolicyState): string | undefined {
  const run = details.teamState?.projects[state.projectName]
  const completed = new Set(run?.completedActions ?? [])
  const closure = details.targetClosure
  if (completed.has('audit') && closure && !closure.reached && run?.autonomyPlateau && !closure.bestEffortReleaseEligible) {
    return `Supervisor уже доказал plateau текущего planning epoch: ${run.autonomyPlateau.reason}`
  }
  const requiresMoreWork = closure && !closure.reached && closure.remainingPackages.length > 0
  if (!completed.has('audit') || !requiresMoreWork) return undefined
  // Progress means the *project* changed, not merely that Planner proposed a
  // different residual package set. The old signature included remaining
  // packages/blocker names, so a planner could cycle through many candidate
  // combinations while merged HEAD and health stayed identical and each idea
  // looked like "material progress". Tie the budget to cumulative Git + health
  // facts instead. A compatibility/source repair that commits to merged gets a
  // new signature even when lag percentage has not moved yet.
  const factsCommit = details.migrationProgress?.factsCommit || details.migrationProgress?.factsRef || '?'
  const signature = `${state.target}:commit=${factsCommit}:lag=${closure.lagOk ?? '?'}/${closure.total ?? '?'}:critical=${closure.critical ?? '?'}:high=${closure.high ?? '?'}`
  const count = (state.goalSignatures[signature] ?? 0) + 1
  state.goalSignatures[signature] = count
  state.goalCycles += 1
  if (count > 1) return `после полного Supervisor→migration→audit цикла не изменились ни cumulative merged commit, ни health: ${closure.lagOk ?? '?'} из ${closure.total ?? '?'}, Critical=${closure.critical ?? '?'}. Перебирать другой residual без фактического Git/health-прогресса больше не буду`
  if (state.goalCycles > MAX_AUTOPILOT_GOAL_CYCLES) return `исчерпан budget ${MAX_AUTOPILOT_GOAL_CYCLES} goal-циклов с фактическим Git/health-прогрессом`
  return undefined
}