import { readFileSync } from 'node:fs'

const types = readFileSync(new URL('../src/types.ts', import.meta.url), 'utf8')
const flow = readFileSync(new URL('../src/components/FlowWorkspace.tsx', import.meta.url), 'utf8')
const dialog = readFileSync(new URL('../src/components/BaselineIntentDialog.tsx', import.meta.url), 'utf8')
const hook = readFileSync(new URL('../src/hooks/useDependencyFlow.ts', import.meta.url), 'utf8')
const main = readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const preload = readFileSync(new URL('../electron/preload.cts', import.meta.url), 'utf8')
const generator = readFileSync(new URL('../../dependency_live_roadmap_generator.py', import.meta.url), 'utf8')

for (const sentinel of [
  "export type BaselinePackagePolicy = 'auto' | 'keep-current' | 'required'",
  'baselineIntent?: BaselineIntent',
  'getBaselineIntentPlan',
]) if (!types.includes(sentinel)) throw new Error(`Baseline intent type/API contract missing: ${sentinel}`)

for (const sentinel of [
  'BaselineIntentDialog',
  'baselinePolicyIdentity',
  "policyChanged ? 'restart' : pending.resume",
  "baselineResume !== 'continue'",
  'baselineDecision',
  'onGetBaselineIntentPlan',
]) if (!flow.includes(sentinel)) throw new Error(`Baseline intent FLOW contract missing: ${sentinel}`)

for (const sentinel of [
  'Продолжить поиск (+',
  'baseline-policy-toggle',
  "setPolicy(item.name, 'keep-current')",
  "setPolicy(item.name, 'required')",
  'Подтвердить и запустить Baseline',
]) if (!dialog.includes(sentinel)) throw new Error(`Baseline intent dialog contract missing: ${sentinel}`)

for (const sentinel of [
  'parseBaselineDecision',
  'baselineDecision',
  'getBaselineIntentPlan',
]) if (!hook.includes(sentinel)) throw new Error(`Baseline human-decision hook contract missing: ${sentinel}`)

for (const sentinel of [
  'DEPLOOM_BASELINE_INTENT_JSON',
  'DEPLOOM_BASELINE_EXTRA_ITERATIONS',
  'DEPLOOM_BASELINE_DECISION_GRANT_ITERATIONS',
  'baselineHumanDecisionRequired',
  "flow:baseline-intent-plan",
]) if (!main.includes(sentinel)) throw new Error(`Baseline intent main-process contract missing: ${sentinel}`)

if (!preload.includes("flow:baseline-intent-plan")) throw new Error('Baseline intent preload bridge missing')

for (const sentinel of [
  'BLOCK_VH_BASELINE_INTENT_HUMAN_LOOP_V1',
  'DEPLOOM_BASELINE_DECISION_V1 ',
  'BASELINE_HUMAN_DECISION_REQUIRED',
  'raise SystemExit(3)',
  '"baselineIntent": {',
  'USER_BASELINE_REQUIRED_UPDATE',
  'USER_BASELINE_KEEP_CURRENT',
  '_apply_baseline_intent_scope',
  'excluded from this Baseline update/health scope by USER_POLICY',
]) if (!generator.includes(sentinel)) throw new Error(`Baseline intent solver/verifier contract missing: ${sentinel}`)

console.log('Baseline intent / human decision loop contract OK')
