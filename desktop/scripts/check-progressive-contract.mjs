import fs from 'node:fs'

const main = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const flow = fs.readFileSync(new URL('../src/components/FlowWorkspace.tsx', import.meta.url), 'utf8')
const hook = fs.readFileSync(new URL('../src/hooks/useDependencyFlow.ts', import.meta.url), 'utf8')
const dialog = fs.readFileSync(new URL('../src/components/BaselineIntentDialog.tsx', import.meta.url), 'utf8')
const audit = fs.readFileSync(new URL('../../manual_dependency_audit.py', import.meta.url), 'utf8')
const acceptance = fs.readFileSync(new URL('../electron/acceptance-policy.ts', import.meta.url), 'utf8')
const policy = fs.readFileSync(new URL('../src/autopilot-policy.ts', import.meta.url), 'utf8')
const recovery = fs.readFileSync(new URL('../electron/flow-recovery.ts', import.meta.url), 'utf8')
const generator = fs.readFileSync(new URL('../../dependency_live_roadmap_generator.py', import.meta.url), 'utf8')
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
  "const target: TargetLevel = details.baselineIntent?.targetLevel === 'green' ? 'green' : 'yellow'",
  "text('Acceptance', 'Acceptance')",
  "text('Freshness', 'Freshness')",
  "text('Результат принят', 'Result accepted')",
  'BLOCK_PROGRESSIVE_HUMAN_FLOW_V1',
  "text('Проверено', 'Verified')",
  "text('Открыть артефакты', 'Open artifacts')",
  'releaseBlocked',
  "ACTION_ORDER.filter((action) => action !== 'push-workspace')",
  // A01: a new Baseline (prepare) preserves the WHOLE loaded intent - target
  // level, lag %, lag window, productMode, control/budget, M/L caps and
  // acceptancePolicy all stay as saved; only run-scoped fields (policies,
  // deferred cohorts, cohort action, auto-open prompt) restart from a clean
  // slate. The old code rebuilt prepare from freshBaselineIntent() and
  // re-carried just four fields, silently resetting every product-level
  // preference to a default on every "Start a new search".
  "mode === 'prepare' ? { ...loaded, intent: {",
  '...loaded.intent,',
  'policies: {},',
  'deferredCohorts: [],',
  'cohortAction: undefined,',
  'autoOpenPrompt: undefined,',
]) has(flow, sentinel, 'Flow acceptance UX')
for (const forbidden of [
  'setTarget(', 'target-field', 'goalMissed', 'bestEffortReleaseEligible', 'goalBlocked', 'targetReached',
  // A01: prepare must never fall back to the fresh default acceptance policy
  // or other product-level preferences (the old loaded.??.fresh reset).
  'acceptancePolicy: loaded.intent.acceptancePolicy ?? fresh.acceptancePolicy',
  '...fresh,',
]) lacks(flow, forbidden, 'A01 preference reset / old target UX')

lacks(dialog, 'preferredFreshnessPct', 'unimplemented freshness target must not be exposed')
has(dialog, "text('Бюджет Baseline / планирования', 'Baseline / planning budget')", 'honest planning budget')
has(dialog, "controlMode === 'AUTONOMOUS'", 'autonomous control mode')
has(dialog, "controlMode === 'CONFIRM_SIGNIFICANT'", 'confirm significant mode')

// F5: the Draft auto-open is consumed by runId only after the prompt content
// was confirmed delivered, so a remount / fresh details never re-opens a run.
has(flow, "openDraftPromptRef.current().then((confirmed) => {", 'one-shot auto-open waits for confirmed receipt')
has(flow, "if (confirmed) onAcknowledgeDraftRun(details.workspace.id, project.name, draftResult.runId)", 'auto-open consumes by runId after receipt')
// F5: the live strip keeps a monotonic elapsed + predictable live/stale
// independent of backend events, and retained measured stage counters.
has(flow, 'setNowTick', 'independent 1s progress re-render')
has(flow, 'anchorSec + sinceLastEventMs / 1000', 'monotonic elapsed extends from the last backend event')
has(flow, "current.step === progress.step", 'stage counters retained across events of the same stage')
has(flow, 'Прогресс этапа', 'stage percent is explicitly a stage percent')
// F5: the backend emits an independent draft-progress heartbeat and freezes it
// at the terminal finalize event; stage percent never claims a terminal 100.
has(generator, '_heartbeat_loop', 'backend heartbeat loop')
has(generator, 'DRAFT_PROGRESS_HEARTBEAT_SECONDS', 'configurable heartbeat cadence')
has(generator, 'mark_draft_terminal', 'terminal freezes the heartbeat')
has(generator, 'min(99, int(round(raw * 100)))', 'stage percent capped below terminal 100')
// F5: the acknowledged-runId marker lives in the hook and survives remounts.
has(hook, 'acknowledgedRunId', 'one-shot ack survives remount (hook-owned marker)')
has(policy, "if (!verdict || verdict.status === 'UNKNOWN') return undefined", 'fail-closed autopilot')
has(policy, "if (verdict.accepted)", 'accepted completion')
has(policy, "return 'agent'", 'security remediation re-entry')
has(recovery, "'ACCEPTANCE_NOT_SATISFIED'", 'pre-release acceptance hard stop')
has(recovery, "'ACCEPTANCE_AUTHORITY_INVALIDATED_DURING_RELEASE'", 'post-release acceptance hard stop')
has(ci, 'npm run check:acceptance-policy', 'acceptance contract must run in CI')
has(ci, 'npm run check:progressive-contract', 'progressive contract must run in CI')
has(ci, 'npm run check:human-flow', 'human-flow contract must run in CI')

console.log('Progressive acceptance source contract OK')
