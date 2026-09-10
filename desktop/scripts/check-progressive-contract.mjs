import fs from 'node:fs'

const main = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const flow = fs.readFileSync(new URL('../src/components/FlowWorkspace.tsx', import.meta.url), 'utf8')
const dialog = fs.readFileSync(new URL('../src/components/BaselineIntentDialog.tsx', import.meta.url), 'utf8')
const audit = fs.readFileSync(new URL('../../manual_dependency_audit.py', import.meta.url), 'utf8')
const acceptance = fs.readFileSync(new URL('../electron/acceptance-policy.ts', import.meta.url), 'utf8')
const policy = fs.readFileSync(new URL('../src/autopilot-policy.ts', import.meta.url), 'utf8')
const recovery = fs.readFileSync(new URL('../electron/flow-recovery.ts', import.meta.url), 'utf8')
const ci = fs.readFileSync(new URL('../../.github/workflows/ci.yml', import.meta.url), 'utf8')

const has = (source, needle, label) => { if (!source.includes(needle)) throw new Error(`${label}: missing ${JSON.stringify(needle)}`) }
const lacks = (source, needle, label) => { if (source.includes(needle)) throw new Error(`${label}: forbidden ${JSON.stringify(needle)}`) }

for (const sentinel of [
  'dependencyInputHash', 'dependencyInputFiles', 'dependency_input_identity(project)',
]) has(audit, sentinel, 'audit input binding')
for (const sentinel of [
  'acceptanceVerdictFromManualAudit', 'dependencyInputIdentity', 'package.json/lockfile inputs changed after the audit',
]) has(acceptance, sentinel, 'acceptance authority')
for (const sentinel of [
  'acceptanceVerdict: project ? readAcceptanceVerdict',
  'ACCEPTANCE_REMEDIATION_REQUIRED',
  'Supervisor acceptance-remediation',
  'ACCEPTANCE_NOT_SATISFIED',
  'Freshness/Yellow capacity is not a correctness gate',
  "'--yarn-audit-engine', 'yarn-inventory'",
  'Acceptance remediation имеет приоритет над freshness residual',
  'ACCEPTANCE_AUTHORITY_INVALIDATED_DURING_RELEASE',
  'const executableActionRows = promptActionRows',
]) has(main, sentinel, 'main progressive orchestration')
for (const forbidden of [
  'GOAL_CLOSURE_PROPOSAL_INSUFFICIENT',
  'Full Yellow доказанно недостижим',
  'Supervisor goal-seeking',
]) lacks(main, forbidden, 'old mandatory Yellow orchestration')

for (const sentinel of [
  "const target: TargetLevel = 'yellow'",
  "text('Acceptance', 'Acceptance')",
  "text('Freshness', 'Freshness')",
  "text('Результат принят', 'Result accepted')",
  'BLOCK_PROGRESSIVE_HUMAN_FLOW_V1',
  "text('Проверено', 'Verified')",
  "text('Открыть артефакты', 'Open artifacts')",
  'releaseBlocked',
  "ACTION_ORDER.filter((action) => action !== 'push-workspace')",
  'acceptancePolicy: loaded.intent.acceptancePolicy ?? fresh.acceptancePolicy',
]) has(flow, sentinel, 'Flow acceptance UX')
for (const forbidden of ['setTarget(', 'target-field', 'goalMissed', 'bestEffortReleaseEligible', 'goalBlocked', 'targetReached']) lacks(flow, forbidden, 'old target UX')

lacks(dialog, 'preferredFreshnessPct', 'unimplemented freshness target must not be exposed')
has(dialog, "text('Бюджет Baseline / планирования', 'Baseline / planning budget')", 'honest planning budget')
has(dialog, "controlMode === 'AUTONOMOUS'", 'autonomous control mode')
has(dialog, "controlMode === 'CONFIRM_SIGNIFICANT'", 'confirm significant mode')
has(policy, "if (!verdict || verdict.status === 'UNKNOWN') return undefined", 'fail-closed autopilot')
has(policy, "if (verdict.accepted)", 'accepted completion')
has(policy, "return 'agent'", 'security remediation re-entry')
has(recovery, "'ACCEPTANCE_NOT_SATISFIED'", 'pre-release acceptance hard stop')
has(recovery, "'ACCEPTANCE_AUTHORITY_INVALIDATED_DURING_RELEASE'", 'post-release acceptance hard stop')
has(ci, 'npm run check:acceptance-policy', 'acceptance contract must run in CI')
has(ci, 'npm run check:progressive-contract', 'progressive contract must run in CI')
has(ci, 'npm run check:human-flow', 'human-flow contract must run in CI')

console.log('Progressive acceptance source contract OK')
