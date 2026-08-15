import fs from 'node:fs'
import ts from 'typescript'

async function loadTypeScriptModule(relativeUrl) {
  const source = fs.readFileSync(new URL(relativeUrl, import.meta.url), 'utf8')
  const javascript = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
    fileName: relativeUrl,
    reportDiagnostics: true,
  })
  if (javascript.diagnostics?.length) {
    throw new Error('TypeScript scenario harness failed to transpile ' + relativeUrl + ': ' + javascript.diagnostics.map((item) => item.messageText).join('; '))
  }
  return import('data:text/javascript;base64,' + Buffer.from(javascript.outputText).toString('base64'))
}

const { AUTOPILOT_ORDER, goalSeekingStopReason, nextAutopilotAction } = await loadTypeScriptModule('../src/autopilot-policy.ts')
const { updateFlowProgress } = await loadTypeScriptModule('../electron/flow-state.ts')

const projectName = 'checkout-form'
const baseDetails = (completedActions, targetClosure, factsCommit = 'merged-a', runExtras = {}) => ({
  teamState: { projects: { [projectName]: { completedActions, ...runExtras } } },
  targetClosure,
  migrationProgress: { factsCommit, factsRef: 'deps-demo-merged' },
})
const state = (overrides = {}) => ({
  projectName,
  target: 'yellow',
  publish: false,
  goalSignatures: {},
  goalCycles: 0,
  ...overrides,
})
const closure = (overrides = {}) => ({
  target: 'yellow',
  reached: false,
  current: 'red',
  remainingPackages: [],
  lagBlockers: [],
  ...overrides,
})
const expectAction = (label, completed, targetClosure, expected, overrides) => {
  const actual = nextAutopilotAction(baseDetails(completed, targetClosure), state(overrides))
  if (actual !== expected) throw new Error(label + ': expected ' + expected + ', got ' + actual)
}

expectAction('empty flow', [], undefined, 'preflight')
expectAction('after preflight', ['preflight'], undefined, 'baseline')
expectAction('after baseline', ['preflight', 'baseline'], undefined, 'agent')
expectAction('missing post-audit artifact', ['preflight', 'baseline', 'agent', 'generate', 'audit'], undefined, 'generate')
expectAction('exhausted 53/73 plan becomes best-effort handoff instead of an infinite agent loop', ['preflight', 'baseline', 'agent', 'generate', 'audit'], closure({ lagOk: 53, total: 73, planCanReachYellow: false }), undefined)
expectAction('remaining executable target', ['preflight', 'baseline', 'agent', 'generate', 'audit'], closure({ remainingPackages: ['postcss-scss'], planCanReachYellow: true }), 'agent')
expectAction('green goal miss with no executable work becomes handoff', ['preflight', 'baseline', 'agent', 'generate', 'audit'], closure({ target: 'green', planCanReachYellow: true }), undefined, { target: 'green' })
expectAction('below-yellow safe residual runs before best-effort', ['preflight', 'baseline', 'agent', 'generate', 'audit'], closure({ lagOk: 55, total: 73, planCanReachYellow: false, remainingPackages: ['safe-a', 'safe-b'] }), 'agent')
expectAction('below-yellow exhausted residual proceeds to best-effort release', ['preflight', 'baseline', 'agent', 'generate', 'audit'], closure({ lagOk: 57, total: 73, planCanReachYellow: false, bestEffortReleaseEligible: true, bestEffortReason: 'safe plan exhausted, Critical=0' }), 'release')
expectAction('yellow reached', ['preflight', 'baseline', 'agent', 'generate', 'audit'], closure({ reached: true, current: 'yellow' }), 'release')
expectAction('publication disabled', AUTOPILOT_ORDER.filter((action) => action !== 'push-workspace'), closure({ reached: true, current: 'yellow' }), undefined)
expectAction('publication enabled', AUTOPILOT_ORDER.filter((action) => action !== 'push-workspace'), closure({ reached: true, current: 'yellow' }), 'push-workspace', { publish: true })

let progress
const sequence = []
for (let guard = 0; guard < 5; guard += 1) {
  const action = nextAutopilotAction(baseDetails(progress?.completedActions ?? [], undefined), state())
  sequence.push(action)
  progress = updateFlowProgress(progress, action, 'running', 'yellow')
  progress = updateFlowProgress(progress, action, 'passed', 'yellow')
}
if (sequence.join(',') !== 'preflight,baseline,agent,generate,audit') throw new Error('Initial FLOW sequence drifted: ' + sequence.join(','))
const insufficient = closure({ lagOk: 53, total: 73, planCanReachYellow: false })
const exhaustedAction = nextAutopilotAction(baseDetails(progress.completedActions, insufficient), state())
if (exhaustedAction !== undefined) throw new Error('Exhausted plan must finish as handoff/best-effort instead of re-entering agent, got ' + exhaustedAction)

const noProgressState = state()
const actionableNoProgress = closure({ lagOk: 53, total: 73, planCanReachYellow: false, remainingPackages: ['x'] })
const noProgressDetails = baseDetails(['preflight', 'baseline', 'agent', 'generate', 'audit'], actionableNoProgress, 'merged-a')
if (goalSeekingStopReason(noProgressDetails, noProgressState)) throw new Error('First goal observation stopped too early')
// Planner candidate churn is NOT project progress: a different residual list
// with the same merged commit + health must close the path immediately.
const churnOnlyDetails = baseDetails(['preflight', 'baseline', 'agent', 'generate', 'audit'], closure({ lagOk: 53, total: 73, planCanReachYellow: false, remainingPackages: ['y', 'z'] }), 'merged-a')
if (!goalSeekingStopReason(churnOnlyDetails, noProgressState)?.includes('не изменились')) throw new Error('Second same-outcome cycle must stop even if Planner changed candidate packages')
const progressedState = state()
if (goalSeekingStopReason(noProgressDetails, progressedState)) throw new Error('First progress observation stopped too early')
const committedRepair = baseDetails(['preflight', 'baseline', 'agent', 'generate', 'audit'], actionableNoProgress, 'merged-b')
if (goalSeekingStopReason(committedRepair, progressedState)) throw new Error('Changed cumulative merged commit must count as real progress')


const persistedPlateau = { target: 'yellow', reason: 'same semantic plan, actionRows=0', updatedAt: new Date().toISOString() }
const staleActionableAfterPlateau = baseDetails(
  ['preflight', 'baseline', 'agent', 'generate', 'audit'],
  closure({ lagOk: 53, total: 73, planCanReachYellow: false, remainingPackages: ['stale-row'] }),
  'merged-a',
  { autonomyPlateau: persistedPlateau },
)
if (nextAutopilotAction(staleActionableAfterPlateau, state()) !== undefined) throw new Error('Persisted autonomy plateau must suppress stale agent re-entry after audit')
if (!goalSeekingStopReason(staleActionableAfterPlateau, state())?.includes('plateau')) throw new Error('Persisted autonomy plateau must explain why autopilot stopped')
const bestEffortAfterPlateau = baseDetails(
  ['preflight', 'baseline', 'agent', 'generate', 'audit'],
  closure({ lagOk: 58, total: 73, remainingPackages: ['stale-row'], bestEffortReleaseEligible: true, bestEffortReason: 'plan exhausted, Critical=0' }),
  'merged-a',
  { autonomyPlateau: persistedPlateau },
)
if (nextAutopilotAction(bestEffortAfterPlateau, state()) !== 'release') throw new Error('Persisted plateau may proceed to explicitly eligible best-effort release')

const closures = [
  undefined,
  closure({ reached: true, current: 'yellow' }),
  closure({ planCanReachYellow: false, lagOk: 53, total: 73 }),
  closure({ planCanReachYellow: true, remainingPackages: ['x'] }),
]
let combinations = 0
for (let mask = 0; mask < (1 << AUTOPILOT_ORDER.length); mask += 1) {
  const completed = AUTOPILOT_ORDER.filter((_action, index) => Boolean(mask & (1 << index)))
  for (const targetClosure of closures) {
    combinations += 1
    const action = nextAutopilotAction(baseDetails(completed, targetClosure), state())
    if (action !== undefined && !AUTOPILOT_ORDER.includes(action)) throw new Error('Policy returned an unknown action: ' + action)
    if (completed.includes('audit') && !targetClosure && action !== 'generate') throw new Error('Missing audited artifact must always regenerate')
  }
}

console.log('Autopilot scenario matrix OK: ' + combinations + ' state/closure combinations plus lifecycle and no-progress regressions')