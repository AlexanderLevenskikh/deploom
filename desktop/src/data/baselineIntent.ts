import type { BaselineDecision, BaselineIntent, BaselineIntentPlan } from '../types'

export const BASELINE_DECISION_MARKER = 'DEPLOOM_BASELINE_DECISION_V1 '

export function freshBaselineIntent(): BaselineIntent {
  return { schemaVersion: 1, policies: {}, extraIterations: 0, decisionGrantIterations: 0 }
}

export function parseBaselineDecision(message: string | undefined): BaselineDecision | undefined {
  if (!message) return undefined
  const normalized = message.replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
  const markerIndex = normalized.lastIndexOf(BASELINE_DECISION_MARKER)
  if (markerIndex < 0) return undefined
  const tail = normalized.slice(markerIndex + BASELINE_DECISION_MARKER.length)
  const start = tail.indexOf('{')
  if (start < 0) return undefined
  const line = tail.slice(start).split(/\r?\n/, 1)[0]
  const end = line.lastIndexOf('}')
  if (end < 0) return undefined
  try {
    const parsed = JSON.parse(line.slice(0, end + 1)) as BaselineDecision
    if (parsed.schemaVersion !== 1 || !parsed.reason) return undefined
    return parsed
  } catch {
    return undefined
  }
}

export function normalizeBaselineIntentPlan(plan: BaselineIntentPlan): BaselineIntentPlan {
  return {
    candidates: [...plan.candidates].sort((a, b) => a.name.localeCompare(b.name)),
    intent: {
      schemaVersion: 1,
      policies: { ...(plan.intent?.policies ?? {}) },
      extraIterations: Math.max(0, Number(plan.intent?.extraIterations ?? 0) || 0),
      decisionGrantIterations: 0,
    },
  }
}
