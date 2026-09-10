import fs from 'node:fs'
import ts from 'typescript'

const types = fs.readFileSync(new URL('../src/types.ts', import.meta.url), 'utf8')
const dialog = fs.readFileSync(new URL('../src/components/BaselineIntentDialog.tsx', import.meta.url), 'utf8')
const main = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const preload = fs.readFileSync(new URL('../electron/preload.cts', import.meta.url), 'utf8')
const normalizerSource = fs.readFileSync(new URL('../src/data/baselineIntent.ts', import.meta.url), 'utf8')
const progressiveSource = fs.readFileSync(new URL('../src/progressive-policy.ts', import.meta.url), 'utf8')

const mustContain = (text, needle, label) => { if (!text.includes(needle)) throw new Error(`${label}: missing ${JSON.stringify(needle)}`) }
const mustNotContain = (text, needle, label) => { if (text.includes(needle)) throw new Error(`${label}: forbidden ${JSON.stringify(needle)}`) }

for (const sentinel of [
  "export type BaselineControlMode = 'AUTONOMOUS' | 'CONFIRM_SIGNIFICANT'",
  'budgetMinutes?: number',
  'acceptancePolicy?: AcceptancePolicy',
]) mustContain(types, sentinel, 'Baseline intent v2 types')

for (const sentinel of [
  "controlMode === 'AUTONOMOUS'",
  "controlMode === 'CONFIRM_SIGNIFICANT'",
  "text('Acceptance policy', 'Acceptance policy')",
  'maxKnownCritical: 0', 'maxKnownHigh', 'budgetMinutes', 'schemaVersion: 2',
]) mustContain(dialog, sentinel, 'progressive Baseline UI')
mustNotContain(dialog, 'setExecutionMode', 'legacy execution mode must not drive UI')
mustNotContain(dialog, 'schemaVersion: 1,', 'dialog must emit v2')
mustNotContain(dialog, 'preferredFreshnessPct', 'unimplemented freshness preference must stay out of happy-path UI')
mustContain(dialog, "text('Бюджет Baseline / планирования', 'Baseline / planning budget')", 'budget scope must be honest')

for (const sentinel of [
  'DEPLOOM_BASELINE_CONTROL_MODE',
  'DEPLOOM_BASELINE_BUDGET_MINUTES',
  'DEPLOOM_ACCEPTANCE_POLICY_JSON',
  "executionMode: controlMode === 'AUTONOMOUS' ? 'BACKGROUND' : 'FAST'",
  'flow:baseline-intent-plan',
]) mustContain(main, sentinel, 'main-process adapter')
mustContain(preload, 'flow:baseline-intent-plan', 'preload bridge')

// Runtime-normalize legacy and new intent without loading the React app.
const progressiveJs = ts.transpileModule(progressiveSource, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
const progressiveUrl = 'data:text/javascript;base64,' + Buffer.from(progressiveJs).toString('base64')
let normalizerJs = ts.transpileModule(normalizerSource, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
normalizerJs = normalizerJs.replace("'../progressive-policy'", JSON.stringify(progressiveUrl)).replace('"../progressive-policy"', JSON.stringify(progressiveUrl))
const normalizer = await import('data:text/javascript;base64,' + Buffer.from(normalizerJs).toString('base64'))
const fresh = normalizer.freshBaselineIntent()
if (fresh.schemaVersion !== 2 || fresh.controlMode !== 'AUTONOMOUS' || fresh.budgetMinutes !== 30) throw new Error('progressive defaults drifted')
if (fresh.acceptancePolicy.maxKnownCritical !== 0 || fresh.acceptancePolicy.maxKnownHigh !== 1) throw new Error('default acceptance must be C0/H1')
const legacyBackground = normalizer.normalizeBaselineIntentPlan({ candidates: [], intent: { schemaVersion: 1, policies: {}, executionMode: 'BACKGROUND' } }).intent
if (legacyBackground.controlMode !== 'AUTONOMOUS') throw new Error('legacy BACKGROUND migration failed')
const legacyFast = normalizer.normalizeBaselineIntentPlan({ candidates: [], intent: { schemaVersion: 1, policies: {}, executionMode: 'FAST' } }).intent
if (legacyFast.controlMode !== 'CONFIRM_SIGNIFICANT') throw new Error('legacy FAST migration failed')
const missingLegacyMode = normalizer.normalizeBaselineIntentPlan({ candidates: [], intent: { schemaVersion: 2, policies: {} } }).intent
if (missingLegacyMode.controlMode !== 'AUTONOMOUS') throw new Error('missing intent mode must default to AUTONOMOUS')

console.log('Baseline progressive intent contract OK')
