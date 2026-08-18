export type FlowAction = 'preflight' | 'sync-tool' | 'baseline' | 'generate' | 'generate-all' | 'audit' | 'agent' | 'recover' | 'release' | 'commit-state' | 'push-workspace'
export type FlowRunStatus = 'running' | 'passed' | 'failed' | 'paused'

export type FlowProgressState = {
  lastAction: string
  status: FlowRunStatus
  target?: string
  completedActions?: string[]
}

export const FLOW_ACTION_ORDER: FlowAction[] = ['preflight', 'baseline', 'agent', 'generate', 'audit', 'release', 'commit-state', 'push-workspace']

export function updateFlowProgress(
  previous: FlowProgressState | undefined,
  action: FlowAction,
  status: FlowRunStatus,
  target?: string,
): FlowProgressState & { completedActions: string[] } {
  const previousActions = previous?.completedActions ?? []
  const completedActions = new Set(previousActions)
  const preservePosition = (action === 'recover' && Boolean(previous)) || (action === 'preflight' && previousActions.some((completed) => completed !== 'preflight'))

  if (status === 'running' && !preservePosition) {
    const actionIndex = FLOW_ACTION_ORDER.indexOf(action)
    if (actionIndex >= 0) {
      for (const downstream of FLOW_ACTION_ORDER.slice(actionIndex)) completedActions.delete(downstream)
    }
  }
  if (status === 'passed' && action !== 'recover') completedActions.add(action)
  if (status === 'paused') return {
    lastAction: preservePosition ? previous!.lastAction : action,
    status: preservePosition ? previous!.status : status,
    target: target ?? previous?.target,
    completedActions: [...completedActions],
  }
  if (status === 'failed') {
    const actionIndex = FLOW_ACTION_ORDER.indexOf(action)
    if (actionIndex >= 0) {
      for (const invalidated of FLOW_ACTION_ORDER.slice(actionIndex)) completedActions.delete(invalidated)
    }
  }

  return {
    lastAction: preservePosition ? previous!.lastAction : action,
    status: preservePosition ? previous!.status : status,
    target: target ?? previous?.target,
    completedActions: [...completedActions],
  }
}
export function restoreVerifiedAgentCompletion(previous: FlowProgressState): FlowProgressState & { completedActions: string[] } {
  const completedActions = new Set(previous.completedActions ?? [])
  completedActions.add('agent')
  return {
    ...previous,
    ...(previous.lastAction === 'agent' && previous.status === 'failed' ? { status: 'passed' as const } : {}),
    completedActions: [...completedActions],
  }
}
