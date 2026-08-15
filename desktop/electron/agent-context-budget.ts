import { Buffer } from 'node:buffer'

// Product admission is intentionally stricter than the provider/model context
// limit. The old check only bounded application-owned input text. Stage 2 also
// reserves explicit headroom for provider/system/tool history and model output,
// so a 32k-style completion reservation can never silently consume the same
// window as the application prompt.
export const AGENT_PROVIDER_CONTEXT_LIMIT_TOKENS = 131_072
export const AGENT_MAX_INPUT_TOKENS = 30_000
export const AGENT_CONTEXT_SAFETY_MARGIN_TOKENS = 2_000
export const AGENT_OUTPUT_RESERVE_TOKENS = 8_000
export const AGENT_PROVIDER_HISTORY_RESERVE_TOKENS = 24_000

export type AgentContextBudgetAssessment = {
  estimatedInputTokens: number
  maxInputTokens: number
  safetyMarginTokens: number
  outputReserveTokens: number
  providerHistoryReserveTokens: number
  providerContextLimitTokens: number
  effectiveInputCeilingTokens: number
  ok: boolean
}

// Conservative byte-based estimator. Admission should err toward a smaller
// prompt; exact provider tokenization is not required for the safety boundary.
export function estimateAgentInputTokens(parts: readonly (string | undefined)[]): number {
  const bytes = parts.reduce((total, part) => total + (part ? Buffer.byteLength(part, 'utf8') : 0), 0)
  return Math.ceil(bytes / 3)
}

export function assessAgentContextBudget(
  parts: readonly (string | undefined)[],
  maxInputTokens = AGENT_MAX_INPUT_TOKENS,
  safetyMarginTokens = AGENT_CONTEXT_SAFETY_MARGIN_TOKENS,
  options: {
    providerContextLimitTokens?: number
    outputReserveTokens?: number
    providerHistoryReserveTokens?: number
  } = {},
): AgentContextBudgetAssessment {
  const estimatedInputTokens = estimateAgentInputTokens(parts)
  const providerContextLimitTokens = options.providerContextLimitTokens ?? AGENT_PROVIDER_CONTEXT_LIMIT_TOKENS
  const outputReserveTokens = options.outputReserveTokens ?? AGENT_OUTPUT_RESERVE_TOKENS
  const providerHistoryReserveTokens = options.providerHistoryReserveTokens ?? AGENT_PROVIDER_HISTORY_RESERVE_TOKENS
  const providerOwnedCeiling = Math.max(0, providerContextLimitTokens - outputReserveTokens - providerHistoryReserveTokens - safetyMarginTokens)
  const effectiveInputCeilingTokens = Math.min(maxInputTokens, providerOwnedCeiling)
  return {
    estimatedInputTokens,
    maxInputTokens,
    safetyMarginTokens,
    outputReserveTokens,
    providerHistoryReserveTokens,
    providerContextLimitTokens,
    effectiveInputCeilingTokens,
    ok: estimatedInputTokens <= effectiveInputCeilingTokens,
  }
}

export function assertAgentContextBudget(parts: readonly (string | undefined)[], label = 'agent task'): AgentContextBudgetAssessment {
  const assessment = assessAgentContextBudget(parts)
  if (!assessment.ok) {
    throw new Error(
      `AGENT_CONTEXT_BUDGET_EXCEEDED: ${label}: estimated application input ${assessment.estimatedInputTokens} > `
      + `${assessment.effectiveInputCeilingTokens} effective input ceiling. Provider context=${assessment.providerContextLimitTokens}, `
      + `output reserve=${assessment.outputReserveTokens}, provider/system/tool-history reserve=${assessment.providerHistoryReserveTokens}, `
      + `safety=${assessment.safetyMarginTokens}. DepLoom must split/compact/start fresh before launching the model.`,
    )
  }
  return assessment
}
