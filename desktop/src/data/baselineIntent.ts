import type { BaselineDecision, BaselineIntent, BaselineIntentPlan } from '../types'
import { DEFAULT_ACCEPTANCE_POLICY, normalizeAcceptancePolicy, normalizeBudgetMinutes } from '../progressive-policy'

export const BASELINE_DECISION_MARKER = 'DEPLOOM_BASELINE_DECISION_V1 '

export function freshBaselineIntent(): BaselineIntent {
  return {
    schemaVersion: 2,
    policies: {},
    controlMode: 'AUTONOMOUS',
    budgetMinutes: 30,
    acceptancePolicy: { ...DEFAULT_ACCEPTANCE_POLICY },
    extraIterations: 0,
    decisionGrantIterations: 0,
    // Legacy transport fields remain normalized for the Python compatibility
    // adapter. They are not user-facing product modes anymore.
    searchMode: 'AUTO',
    executionMode: 'BACKGROUND',
    proofMode: 'VERIFIED',
    deferredCohorts: [],
  }
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
    if ((parsed.schemaVersion !== 1 && parsed.schemaVersion !== 2) || !parsed.reason) return undefined
    return parsed
  } catch {
    return undefined
  }
}

export function normalizeBaselineIntentPlan(plan: BaselineIntentPlan): BaselineIntentPlan {
  const raw = plan.intent ?? freshBaselineIntent()
  const legacyAutonomous = raw.executionMode === 'BACKGROUND'
  const controlMode = raw.controlMode === 'AUTONOMOUS' || raw.controlMode === 'CONFIRM_SIGNIFICANT'
    ? raw.controlMode
    : legacyAutonomous ? 'AUTONOMOUS' : 'CONFIRM_SIGNIFICANT'
  return {
    candidates: [...plan.candidates].sort((a, b) => a.name.localeCompare(b.name)),
    intent: {
      schemaVersion: 2,
      policies: { ...(raw.policies ?? {}) },
      controlMode,
      budgetMinutes: normalizeBudgetMinutes(raw.budgetMinutes, 30),
      acceptancePolicy: normalizeAcceptancePolicy(raw.acceptancePolicy),
      extraIterations: Math.max(0, Number(raw.extraIterations ?? 0) || 0),
      decisionGrantIterations: 0,
      searchMode: raw.searchMode ?? 'AUTO',
      executionMode: controlMode === 'AUTONOMOUS' ? 'BACKGROUND' : 'FAST',
      proofMode: raw.proofMode === 'DRAFT' ? 'DRAFT' : 'VERIFIED',
      deferredCohorts: [...(raw.deferredCohorts ?? [])],
    },
  }
}
