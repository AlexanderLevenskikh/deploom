import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const desktop = path.resolve(here, '..')
const repo = path.resolve(desktop, '..')
const read = (relative) => fs.readFileSync(path.join(repo, relative), 'utf8')

const app = read('desktop/src/App.tsx')
const flow = read('desktop/src/components/FlowWorkspace.tsx')
const dialog = read('desktop/src/components/BaselineIntentDialog.tsx')
const main = read('desktop/electron/main.ts')
const acceptance = read('desktop/electron/acceptance-policy.ts')
const anytime = read('block_psi_anytime.py')
const css = read('desktop/src/App.css')

const has = (source, needle, label) => { if (!source.includes(needle)) throw new Error(`${label}: missing ${JSON.stringify(needle)}`) }
const lacks = (source, needle, label) => { if (source.includes(needle)) throw new Error(`${label}: forbidden ${JSON.stringify(needle)}`) }

for (const needle of [
  'BLOCK_PROGRESSIVE_HUMAN_FLOW_V1',
  "text('Проверено', 'Verified')",
  "text('Сейчас пробуем', 'Trying now')",
  "text('Отложено', 'Deferred')",
  "text('Актуальность библиотек', 'Library freshness')",
  "text('Открыть артефакты', 'Open artifacts')",
  '.dependency-roadmap/artifacts/runs',
  '<details className="flow-technical-details">',
]) has(flow, needle, 'human FLOW')

lacks(app, '<MonitoringPanel', 'default App surface')
has(app, 'human-error-toast', 'human error surface')
has(css, '.human-flow-card', 'human FLOW styling')
has(css, 'grid-template-columns: 236px minmax(620px, 1fr)', 'two-column default shell')

for (const needle of ['BLOCK_HUMAN_FLOW_ARTIFACT_LOG_V1', 'ARTIFACT_LOG_FLUSH_MS = 250', "'activity.log'", "'run.json'", "flag: 'a'"]) {
  has(main, needle, 'artifact logging')
}

has(acceptance, 'maxKnownCritical: 0,', 'fixed Critical policy')
lacks(acceptance, 'maxKnownCritical: boundedCount(raw.maxKnownCritical', 'relaxable Critical policy')
has(acceptance, 'MAX_ACCEPTANCE_AUDIT_AGE_MS = 24 * 60 * 60 * 1000', 'audit age policy')
has(acceptance, 'Vulnerability audit evidence is older than 24 hours.', 'stale audit fail-closed')
lacks(dialog, 'setMaxKnownCritical', 'Critical UI control')
has(dialog, 'baseline-critical-fixed', 'fixed Critical UI')

const hardBudgetPos = anytime.indexOf('if self.elapsed_seconds >= self.policy.wall_clock_seconds:')
const exhaustivePos = anytime.indexOf('if self.exhaustive_authorized:', hardBudgetPos)
if (hardBudgetPos < 0 || exhaustivePos < 0 || exhaustivePos < hardBudgetPos) throw new Error('EXHAUSTIVE still bypasses hard user budget')

has(main, "raw.executionMode === 'FAST' || raw.executionMode === 'AUTOPILOT' ? 'CONFIRM_SIGNIFICANT' : 'AUTONOMOUS'", 'backend autonomous default')
console.log('Human FLOW + acceptance correctness contracts OK')
