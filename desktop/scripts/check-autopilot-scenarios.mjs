import fs from 'node:fs'
import ts from 'typescript'

async function loadTypeOnlyModule(relativeUrl) {
  const source = fs.readFileSync(new URL(relativeUrl, import.meta.url), 'utf8')
  const javascript = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
    fileName: relativeUrl,
    reportDiagnostics: true,
  })
  if (javascript.diagnostics?.length) throw new Error('TypeScript scenario harness failed to transpile ' + relativeUrl)
  return import('data:text/javascript;base64,' + Buffer.from(javascript.outputText).toString('base64'))
}

const { AUTOPILOT_ORDER, goalSeekingStopReason, nextAutopilotAction } = await loadTypeOnlyModule('../src/autopilot-policy.ts')

const projectName = 'checkout-form'
const verdict = (status, critical = 0, high = 0, overrides = {}) => ({
  status,
  accepted: status === 'ACCEPTED',
  evidenceComplete: status !== 'UNKNOWN',
  dependencyEvidenceFresh: status !== 'UNKNOWN',
  critical,
  high,
  criticalPackages: critical ? ['danger'] : [],
  highPackages: high ? ['highdep'] : [],
  reasons: status === 'ACCEPTED' ? ['accepted'] : ['needs remediation'],
  policy: { maxKnownCritical: 0, maxKnownHigh: 1 },
  ...overrides,
})
const details = (completedActions, acceptanceVerdict, factsCommit = 'merged-a', runExtras = {}) => ({
  teamState: { projects: { [projectName]: { completedActions, ...runExtras } } },
  acceptanceVerdict,
  migrationProgress: { factsCommit, factsRef: 'deps-demo-merged' },
})
const state = (overrides = {}) => ({ projectName, target: 'yellow', publish: false, goalSignatures: {}, goalCycles: 0, ...overrides })
const expectAction = (label, completed, acceptanceVerdict, expected, overrides) => {
  const actual = nextAutopilotAction(details(completed, acceptanceVerdict), state(overrides))
  if (actual !== expected) throw new Error(label + ': expected ' + expected + ', got ' + actual)
}

expectAction('empty flow', [], undefined, 'preflight')
expectAction('after preflight', ['preflight'], undefined, 'baseline')
expectAction('after baseline', ['preflight', 'baseline'], undefined, 'agent')
expectAction('accepted C0/H1 goes to release regardless of freshness', ['preflight', 'baseline', 'agent', 'generate', 'audit'], verdict('ACCEPTED', 0, 1), 'release')
expectAction('critical finding reopens remediation', ['preflight', 'baseline', 'agent', 'generate', 'audit'], verdict('REMEDIATION_REQUIRED', 1, 0), 'agent')
expectAction('H2 reopens remediation', ['preflight', 'baseline', 'agent', 'generate', 'audit'], verdict('REMEDIATION_REQUIRED', 0, 2), 'agent')
expectAction('unknown audit fails closed', ['preflight', 'baseline', 'agent', 'generate', 'audit'], verdict('UNKNOWN'), undefined)
expectAction('publication disabled', AUTOPILOT_ORDER.filter((action) => action !== 'push-workspace'), verdict('ACCEPTED'), undefined)
expectAction('publication enabled', AUTOPILOT_ORDER.filter((action) => action !== 'push-workspace'), verdict('ACCEPTED'), 'push-workspace', { publish: true })

const noProgressState = state()
const unresolved = verdict('REMEDIATION_REQUIRED', 1, 0)
const first = details(['preflight', 'baseline', 'agent', 'generate', 'audit'], unresolved, 'merged-a')
if (goalSeekingStopReason(first, noProgressState)) throw new Error('first remediation observation stopped too early')
const churn = details(['preflight', 'baseline', 'agent', 'generate', 'audit'], unresolved, 'merged-a')
if (!goalSeekingStopReason(churn, noProgressState)?.includes('acceptance verdict')) throw new Error('candidate/freshness churn must not count as progress')
const progressed = state()
if (goalSeekingStopReason(first, progressed)) throw new Error('first progress observation stopped too early')
if (goalSeekingStopReason(details(['preflight', 'baseline', 'agent', 'generate', 'audit'], unresolved, 'merged-b'), progressed)) throw new Error('changed cumulative commit must count as progress')

const plateau = details(['preflight', 'baseline', 'agent', 'generate', 'audit'], unresolved, 'merged-a', { autonomyPlateau: { target: 'yellow', reason: 'security remediation plateau', updatedAt: new Date().toISOString() } })
if (nextAutopilotAction(plateau, state()) !== undefined) throw new Error('persisted remediation plateau must stop')
if (!goalSeekingStopReason(plateau, state())?.includes('plateau')) throw new Error('plateau must explain stop')

console.log('Progressive Autopilot scenario matrix OK')
