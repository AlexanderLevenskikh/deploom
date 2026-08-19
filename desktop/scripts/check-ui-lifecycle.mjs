import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
const read = (relative) => readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
const app = read('../src/App.tsx')
const rail = read('../src/components/ProjectRail.tsx')
const workspace = read('../src/components/WorkspaceDialog.tsx')
const baseline = read('../src/components/BaselineIntentDialog.tsx')
const monitoring = read('../src/components/MonitoringPanel.tsx')
const flow = read('../src/components/FlowWorkspace.tsx')
const model = read('../src/components/ModelPicker.tsx')
const main = read('../electron/main.ts')
const preload = read('../electron/preload.cts')
const types = read('../src/types.ts')
const css = read('../src/App.css')

for (const [name, source, sentinels] of [
  ['App', app, ['WorkspaceDialog', 'onRemoveProject', 'showWorkspaceDialog']],
  ['ProjectRail', rail, ['project-row-remove', 'Trash2', 'window.confirm']],
  ['WorkspaceDialog', workspace, ['Подключить существующий', 'Создать workspace', 'dialog-actions']],
  ['BaselineIntentDialog', baseline, ['useDeferredValue', 'autoFocus', 'deferredQuery']],
  ['MonitoringPanel', monitoring, ['presentRunError', "setView('errors')"]],
  ['FlowWorkspace', flow, ['ModelPicker', 'persistAgentModel', 'startAutopilotWithCurrentModel']],
  ['ModelPicker', model, ['role="listbox"', 'onCommit', 'model-picker-menu']],
  ['main', main, ["flow:remove-project", 'Agent execution binding:', "spec.args.indexOf('--model')"]],
  ['preload', preload, ['removeProject:', "flow:remove-project"]],
  ['types', types, ['removeProject:', 'Promise<{ state: DesktopState; details: WorkspaceDetails }>']],
]) for (const sentinel of sentinels) if (!source.includes(sentinel)) throw new Error(`${name} UI lifecycle contract missing: ${sentinel}`)

if (flow.includes('<datalist') || flow.includes('list="agent-model-suggestions"')) throw new Error('Native datalist model picker must not remain')

for (const sentinel of ['BLOCK_UI_LIFECYCLE_POLISH_V1', '.monitoring-body > .run-monitor', 'height: auto', '.activity-entry { background: var(--surface-raised)', '.model-picker-menu', 'backdrop-filter: none']) {
  if (!css.includes(sentinel)) throw new Error(`UI polish CSS contract missing: ${sentinel}`)
}
console.log('UI lifecycle polish contracts OK')
