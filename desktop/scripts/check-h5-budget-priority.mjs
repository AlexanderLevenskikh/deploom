import fs from 'node:fs'
import { baselineBudgetOverride } from "../dist-electron/baseline-budget.js";
import { handleDialogBudget, normalizeBudgetField } from "../dist-electron/baseline-intent.js";

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

// N1: the DIALOG must not turn a plain Fast/Deep choice into an explicit
// budget. handleDialogBudget returns NOTHING for an untouched field on a
// default plan (edited=false, planExplicit=false), so buildIntent carries no
// budgetMinutes at all and the saved intent stays implicit.
const untouchDefault = handleDialogBudget({ edited: false, planExplicit: false, shownMinutes: 30, planBudgetMinutes: 30 });
if (Object.keys(untouchDefault).length !== 0) throw new Error(`An untouched budget field must not become an override: ${JSON.stringify(untouchDefault)}`);
const editedDefault = handleDialogBudget({ edited: true, planExplicit: false, shownMinutes: 30, planBudgetMinutes: 30 });
if (editedDefault.budgetMinutes !== 30 || editedDefault.budgetMinutesExplicit !== true) throw new Error(`Editing the field must carry an explicit override: ${JSON.stringify(editedDefault)}`);
const reopenExplicit = handleDialogBudget({ edited: false, planExplicit: true, shownMinutes: 45, planBudgetMinutes: 45 });
if (reopenExplicit.budgetMinutes !== 45 || reopenExplicit.budgetMinutesExplicit !== true) throw new Error(`Reopening an explicitly saved intent must keep the override: ${JSON.stringify(reopenExplicit)}`);

// N1: normalizing a RAW intent must never read explicitness out of the
// default 30: only the flag or a non-default migration value counts.
const norm = (raw) => normalizeBudgetField(raw);
if (norm({ schemaVersion: 2, budgetMinutes: 30 }).budgetMinutesExplicit) throw new Error('v2 default 30 must stay implicit');
if (!norm({ schemaVersion: 2, budgetMinutes: 45 }).budgetMinutesExplicit) throw new Error('v2 non-default 30 must migrate explicit');
if (!norm({ schemaVersion: 2, budgetMinutes: 30, budgetMinutesExplicit: true }).budgetMinutesExplicit) throw new Error('explicit flag must win');
if (norm({ schemaVersion: 1, budgetMinutes: 30 }).budgetMinutesExplicit !== true) throw new Error('legacy v1 intent with a budget must be explicit');
if (norm({ schemaVersion: 2 }).budgetMinutes !== 30 || norm({ schemaVersion: 2 }).budgetMinutesExplicit) throw new Error('missing budget must normalize to implicit default 30');
if (norm({ schemaVersion: 2, budgetMinutes: 300 }).budgetMinutes !== 240) throw new Error('budget must clamp to 5..240');

// N1 source contract: the dialog uses the SAME shared module (single source
// of truth), the renderer preferences preserve the explicitness flag, and
// main.ts derives the override from the normalized raw INTENT (not from the
// normalized-fallback value).
const dialogSource = fs.readFileSync(new URL('../src/components/BaselineIntentDialog.tsx', import.meta.url), 'utf8');
const dataSource = fs.readFileSync(new URL('../src/data/baselineIntent.ts', import.meta.url), 'utf8');
mustContain(dialogSource, "from '../../electron/baseline-intent'", 'dialog shared-module import');
mustContain(dialogSource, 'handleDialogBudget', 'dialog uses handleDialogBudget');
mustContain(dialogSource, 'budgetEdited', 'dialog has budget-edited state');
mustContain(dialogSource, 'baseline-budget-applied', 'dialog shows the applied budget');
mustContain(dataSource, 'budgetMinutesExplicit', 'prepare plan preserves the explicit flag');
mustContain(mainSource, 'normalizeBudgetField(input.baselineIntent)', 'raw input explicitness from the shared normalizer');

console.log("Budget priority gate OK");
