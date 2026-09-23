// H5: the Desktop must not silently replace the declared FAST/DEEP product-mode
// budgets (engine defaults: fast=300s/2 attempts, deep=3600s/12) with the flat
// automatic override. Only an explicitly chosen per-run budget or a saved
// legacy budget supplies the override; a plain default (no budget anywhere)
// leaves the engine's mode budgets in effect.

export interface BaselineBudgetOverride {
  automaticBudgetSeconds?: number
  maxExpensiveAttempts?: number
}

export interface BaselineBudgetInput {
  /** Explicit budget for THIS run (raw field present on the action input). */
  explicitBudgetMinutes?: number
  /** Whether the persisted intent carries its own budget (legacy saved). */
  persistedHasBudgetMinutes: boolean
  /** The clamped budgetMinutes the baseline action computed. */
  clampedBudgetMinutes: number
}

export function baselineBudgetOverride(input: BaselineBudgetInput): BaselineBudgetOverride {
  const chosen = Number.isFinite(Number(input.explicitBudgetMinutes)) || Boolean(input.persistedHasBudgetMinutes)
  if (!chosen) return {}
  const minutes = Math.max(5, Math.min(240, Math.round(Number(input.clampedBudgetMinutes) || 30) || 30))
  return {
    automaticBudgetSeconds: minutes * 60,
    maxExpensiveAttempts: Math.max(2, Math.min(8, Math.ceil(minutes / 10))),
  }
}
