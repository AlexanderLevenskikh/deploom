import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const root = process.cwd()
const compiled = join(root, 'dist-electron', 'workspace-bootstrap.js')
const { initializeWorkspaceRepository, WORKSPACE_SETTINGS_PATH } = await import(pathToFileURL(compiled).href)

const calls = []
const fakeGit = async (command, args, cwd) => {
  calls.push({ command, args: [...args], cwd })
  if (command === 'git' && args[0] === 'init') mkdirSync(join(cwd, '.git'), { recursive: true })
  return { code: 0, stdout: '', stderr: '' }
}

const parent = mkdtempSync(join(tmpdir(), 'dependency-flow-workspace-check-'))
const target = join(parent, 'new-workspace')
try {
  await initializeWorkspaceRepository(target, 'git@example.test:team/deps-workspace.git', fakeGit)
  const settings = JSON.parse(readFileSync(join(target, WORKSPACE_SETTINGS_PATH), 'utf8'))
  assert.deepEqual(settings, { schemaVersion: 1, projects: [] }, 'new workspace must contain a minimal project settings file')
  const gitignore = readFileSync(join(target, '.gitignore'), 'utf8')
  assert.match(gitignore, /settings\.local\.json/, 'local override must stay untracked')
  assert.match(gitignore, /artifacts\/\*/, 'generated roadmap artifacts must stay untracked')
  assert.ok(calls.some((call) => call.command === 'git' && call.args[0] === 'init'), 'new workspace must initialize Git locally')
  assert.ok(calls.some((call) => call.args.includes('refs/heads/main')), 'bootstrap must use a deterministic initial branch')
  assert.ok(calls.some((call) => call.args.includes('commit') && call.args.includes('chore: initialize DepLoom workspace')), 'new workspace must get a root commit')
  assert.ok(calls.some((call) => call.args.join(' ') === 'remote add origin git@example.test:team/deps-workspace.git'), 'optional team remote must be origin')

  const setup = readFileSync(join(root, 'src', 'components', 'SetupScreen.tsx'), 'utf8')
  assert.doesNotMatch(setup, /Базовый template|templateRemote|remote\.trim\(\)/, 'new-workspace UI must not require a template setting')
  assert.match(setup, /Создать и подключить/, 'new-workspace UI must expose local bootstrap')

  const main = readFileSync(join(root, 'electron', 'main.ts'), 'utf8')
  assert.match(main, /if \(templateRemote\)/, 'legacy explicit template path must remain supported')
  assert.match(main, /initializeWorkspaceRepository\(target/, 'template-free path must use local bootstrap')
  assert.match(main, /flow:register-workspace/, 'existing workspaces must remain registerable')
} finally {
  rmSync(parent, { recursive: true, force: true })
}

console.log('workspace bootstrap check: ok')
