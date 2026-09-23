// N1: explicit-vs-default budget handling shared by the renderer dialog, the
// Desktop action and the persistence round-trip. A plain Fast/Deep choice must
// keep the declared product-mode budgets (fast=300s/2, deep=3600s/12); the
// explicit override exists only when the user actually chose a budget.

export interface NormalizedBudgetField {
  budgetMinutes: number
  budgetMinutesExplicit: boolean
}

/**
 * Extract {budgetMinutes, budgetMinutesExplicit} from a raw intent object.
 *
 * Explicitness rules ("a saved default is not a user choice"):
 * - a stored `budgetMinutesExplicit` flag is authoritative;
 * - legacy v1 intents always carried a real budget -> explicit when present;
 * - a v2 file written by the pre-fix dialog had no flag, so only a value that
 *   differs from the implicit 30 was truly chosen -> explicit (migration);
 * - otherwise (no flag + default 30, or no budget at all) -> NOT explicit and
 *   the engine keeps its mode defaults.
 */
export function normalizeBudgetField(raw: unknown): NormalizedBudgetField {
  const record = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const rawMinutes = record.budgetMinutes
  const hasFlag = 'budgetMinutesExplicit' in record
  const flag = record.budgetMinutesExplicit === true
  const minutesPresent = rawMinutes !== undefined && rawMinutes !== null && String(rawMinutes).trim() !== ''
  const schemaVersion = record.schemaVersion
  const parsed = Number(rawMinutes ?? 30)
  const budgetMinutes = Number.isFinite(parsed) ? Math.max(5, Math.min(240, Math.round(parsed))) : 30
  let budgetMinutesExplicit: boolean
  if (hasFlag) budgetMinutesExplicit = flag
  else if (schemaVersion === 1 && minutesPresent) budgetMinutesExplicit = true
  else if (minutesPresent && budgetMinutes !== 30) budgetMinutesExplicit = true
  else budgetMinutesExplicit = false
  return { budgetMinutes, budgetMinutesExplicit }
}

export interface DialogBudgetInput {
  edited: boolean
  planExplicit: boolean
  shownMinutes: number
  planBudgetMinutes?: number
}

/**
 * What the dialog should actually put into the built intent. An untouched
 * budget field on a default plan carries NO budget at all, so saving the run
 * can never turn the implicit default into a "saved legacy budget"; editing
 * the field (or reopening an explicitly saved intent) carries the number plus
 * the explicitness flag.
 */
export function handleDialogBudget(input: DialogBudgetInput): { budgetMinutes?: number; budgetMinutesExplicit?: boolean } {
  if (!input.edited && !input.planExplicit) return {}
  const parsed = Number(input.shownMinutes)
  const shown = Number.isFinite(parsed) ? Math.max(5, Math.min(240, Math.round(parsed))) : (input.planBudgetMinutes ?? 30)
  return { budgetMinutes: shown, budgetMinutesExplicit: true }
}

export function attemptsForMinutes(minutes: number): number {
  return Math.max(2, Math.min(8, Math.ceil(Math.max(5, Math.min(240, Math.round(minutes) || 30)) / 10)))
}
