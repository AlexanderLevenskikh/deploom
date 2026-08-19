import fs from 'node:fs'
import path from 'node:path'

const root = process.cwd()
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8')
const fail = (message) => { console.error(message); process.exitCode = 1 }

const keyPattern = /^\s*"([^"]+)":/gm
const keys = (text) => new Set([...text.matchAll(keyPattern)].map((match) => match[1]))

const en = keys(read('src/i18n/locales/en.ts'))
const ru = keys(read('src/i18n/locales/ru.ts'))
const missingRu = [...en].filter((key) => !ru.has(key))
const extraRu = [...ru].filter((key) => !en.has(key))
if (missingRu.length || extraRu.length) {
  fail(`i18n key mismatch: missingRu=${missingRu.join(',')} extraRu=${extraRu.join(',')}`)
}

const provider = read('src/i18n.tsx')
if (!/return 'en'/.test(provider)) fail('English must be the default when no saved language exists')
if (!/localStorage\.getItem\(STORAGE_KEY\)/.test(provider)) fail('Saved language preference must remain supported')

const flow = read('src/data/flow.ts')
if (!/titleKey: 'flow\.stage\./.test(flow) || /\btitle:\s*'[^']*[А-Яа-яЁё]/.test(flow)) {
  fail('FLOW stage copy must use locale dictionary keys')
}

for (const rel of [
  'src/App.tsx',
  'src/components/AddProjectDialog.tsx',
  'src/components/BranchFailureModal.tsx',
  'src/components/DashboardWorkspace.tsx',
  'src/components/ProjectRail.tsx',
  'src/components/SetupScreen.tsx',
  'src/components/WorkspaceDialog.tsx',
  'src/components/RunMonitor.tsx',
]) {
  const source = read(rel)
  if (/[А-Яа-яЁё]/.test(source)) fail(`${rel}: hard-coded Cyrillic UI text remains`)
}

const flowWorkspace = read('src/components/FlowWorkspace.tsx')
for (const forbidden of [
  '>Путь к проекту<',
  '>Текущий уровень<',
  '>Целевой уровень<',
  '>Прогресс запуска<',
  '>Текущий запуск<',
  '>Три итоговых документа<',
  '>Prompt устарел<',
  '>План уже частично выполнен<',
  '>Автопилот работает<',
]) {
  if (flowWorkspace.includes(forbidden)) fail(`FlowWorkspace static copy is not localized: ${forbidden}`)
}

if (!process.exitCode) console.log(`i18n dictionaries OK (${en.size} typed keys, EN default)`)
