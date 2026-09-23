import fs from 'node:fs'
import { baselineBudgetOverride } from "../dist-electron/baseline-budget.js";

// H5: a plain default (neither an explicit per-run budget nor a saved legacy
// budget) must NOT replace the declared FAST/DEEP mode budgets with the flat
// override -- the engine stays on fast=300s/2, deep=3600s/12.
const fresh = baselineBudgetOverride({ explicitBudgetMinutes: undefined, persistedHasBudgetMinutes: false, clampedBudgetMinutes: 30 });
if (Object.keys(fresh).length !== 0) throw new Error(`A plain default must keep the declared mode budgets: ${JSON.stringify(fresh)}`);

// An explicitly chosen per-run budget supplies the override for both modes.
const explicit = baselineBudgetOverride({ explicitBudgetMinutes: 60, persistedHasBudgetMinutes: false, clampedBudgetMinutes: 60 });
if (explicit.automaticBudgetSeconds !== 3600 || explicit.maxExpensiveAttempts !== 6) throw new Error(`Explicit user budget must override: ${JSON.stringify(explicit)}`);

// A saved legacy budget is honoured too (backward compatibility).
const legacy = baselineBudgetOverride({ explicitBudgetMinutes: undefined, persistedHasBudgetMinutes: true, clampedBudgetMinutes: 30 });
if (legacy.automaticBudgetSeconds !== 1800 || legacy.maxExpensiveAttempts !== 3) throw new Error(`Saved legacy budget must override: ${JSON.stringify(legacy)}`);

// The override is still clamped to the product range (5..240 minutes).
const clamped = baselineBudgetOverride({ explicitBudgetMinutes: 5, persistedHasBudgetMinutes: false, clampedBudgetMinutes: 5 });
if (clamped.automaticBudgetSeconds !== 300 || clamped.maxExpensiveAttempts !== 2) throw new Error(`Budget must clamp to the product range: ${JSON.stringify(clamped)}`);

// The Desktop action must actually WIRE the helper into the baseline env:
// the automatic-budget keys are emitted only through the conditional spread.
const mainSource = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8');
const mustContain = (text, needle, label) => { if (!text.includes(needle)) throw new Error(`${label}: missing ${JSON.stringify(needle)}`) };
mustContain(mainSource, "from './baseline-budget.js'", "main.ts import");
mustContain(mainSource, 'baselineIntentHasPersistedBudgetMinutes(workspace, project.name)', 'persisted-budget detection');
mustContain(mainSource, 'budgetOverride.automaticBudgetSeconds !== undefined', 'conditional budget spread');
if ((mainSource.match(/DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS:/g) || []).length !== 1) {
  throw new Error('main.ts must emit DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS only inside the conditional override spread');
}

console.log("Budget priority gate OK");
