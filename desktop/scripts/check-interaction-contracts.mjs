import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../src', import.meta.url))
const components = join(root, 'components')
const read = (name) => readFileSync(join(components, name), 'utf8')

const add = read('AddProjectDialog.tsx')
const baseline = read('BaselineIntentDialog.tsx')
const workspace = read('WorkspaceDialog.tsx')
const branchFailure = read('BranchFailureModal.tsx')
const goal = read('GoalDetailsModal.tsx')
const quick = read('QuickSelect.tsx')
const css = readFileSync(new URL('../src/App.css', import.meta.url), 'utf8')

for (const [name, source, sentinels] of [
  ['AddProjectDialog', add, ['dialog-actions', "t('common.cancel')", 'button primary']],
  ['BaselineIntentDialog', baseline, ['baseline-intent-actions', 'Запустить автономно', 'Запустить Baseline', 'Подтверждать существенное', 'Техническая глубина поиска', 'Отложить группу', 'Вернуться к этой группе', 'Изменения состава Baseline ещё не применены', 'baseline-policy-toggle']],
  ['WorkspaceDialog', workspace, ['dialog-actions', "t('common.cancel')", "t('workspaceDialog.create')", "t('workspaceDialog.connect.title')"]],
  ['BranchFailureModal', branchFailure, ['modal-actions', "t('common.understood')", 'onClose']],
  ['GoalDetailsModal', goal, ['modal-actions', "t('common.understood')", 'onClose']],
]) {
  for (const sentinel of sentinels) {
    if (!source.includes(sentinel)) throw new Error(`${name} interaction contract missing: ${sentinel}`)
  }
}

for (const legacy of ['Запустить Fast Baseline', 'Работать автономно', 'Режим Baseline']) {
  if (baseline.includes(legacy)) throw new Error(`Legacy Baseline product interaction remains: ${legacy}`)
}

const tsxFiles = readdirSync(components).filter((name) => name.endsWith('.tsx'))
tsxFiles.push('../App.tsx')
for (const relative of tsxFiles) {
  const source = relative === '../App.tsx'
    ? readFileSync(join(root, 'App.tsx'), 'utf8')
    : read(relative)
  if (/<select\b/i.test(source)) throw new Error(`Native select remains in Desktop UI: ${relative}`)
}

for (const sentinel of ['createPortal', 'aria-haspopup="listbox"', 'quick-select-menu']) {
  if (!quick.includes(sentinel)) throw new Error(`QuickSelect contract missing: ${sentinel}`)
}

for (const sentinel of [
  '.baseline-intent-dialog {',
  'display: flex',
  '.baseline-intent-list {',
  'flex: 1 1 auto',
  '.baseline-intent-actions {',
  '.modal-actions',
  '.dialog-actions',
]) {
  if (!css.includes(sentinel)) throw new Error(`Modal layout contract missing: ${sentinel}`)
}

const hook = readFileSync(new URL('../src/hooks/useDialogFocus.ts', import.meta.url), 'utf8')
// Focus trap must not cache first/last at open: a control that becomes enabled
// or visible while the dialog stays open (discovery finishing, a section
// expanding) must be reachable by Tab. Guard that the Tab handler re-evaluates
// the visible focusable set and that the selector excludes disabled controls.
for (const sentinel of [
  'button:not([disabled])',
  'visibleFocusable',
  'visibleFocusable(ref.current)',
]) {
  if (!hook.includes(sentinel)) throw new Error(`Dialog focus-trapping contract missing: ${sentinel}`)
}

// Picker updates path before discovery: its callback still closes over the
// preceding render, so stale-response rejection must use the request epoch.
const addProject = read('AddProjectDialog.tsx')
if (addProject.includes('path.trim() !== trimmed')) throw new Error('Folder picker scan cannot compare the stale render path')
for (const marker of ['onPathChange(value)', 'requestId !== discoveryRequestRef.current', '!discovering && name.trim()', 'setDiscovering(false)']) {
  if (!addProject.includes(marker)) throw new Error(`Project picker contract missing: ${marker}`)
}
console.log('Desktop interaction contracts OK: explicit modal actions + non-native selects + visible Baseline footer')
