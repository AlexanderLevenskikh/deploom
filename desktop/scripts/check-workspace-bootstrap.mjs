import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const root = process.cwd()
const compiled = join(root, 'dist-electron', 'workspace-bootstrap.js')
const { initializeWorkspaceRepository, WORKSPACE_SETTINGS_PATH } = await import(pathToFileURL(compiled).href)
const { readProjectManifest } = await import(pathToFileURL(join(root, 'dist-electron/project-manifest.js')).href)

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
  assert.match(setup, /t\('setup\.create'\)/, 'new-workspace UI must expose the localized bootstrap action')
  const enLocale = readFileSync(join(root, 'src', 'i18n', 'locales', 'en.ts'), 'utf8')
  const ruLocale = readFileSync(join(root, 'src', 'i18n', 'locales', 'ru.ts'), 'utf8')
  assert.match(enLocale, /"setup\.create":\s*"Create and connect"/, 'English locale must expose the local bootstrap action')
  assert.match(ruLocale, /"setup\.create":\s*"Создать и подключить"/, 'Russian locale must expose the local bootstrap action')

  const main = readFileSync(join(root, 'electron', 'main.ts'), 'utf8')
  assert.match(main, /if \(templateRemote\)/, 'legacy explicit template path must remain supported')
  assert.match(main, /initializeWorkspaceRepository\(target/, 'template-free path must use local bootstrap')
  assert.match(main, /flow:register-workspace/, 'existing workspaces must remain registerable')
} finally {
  rmSync(parent, { recursive: true, force: true })
}

// Exercise the production IPC handlers against a real nested Git repository.
const ts = (await import('typescript')).default
const fs = await import('node:fs')
const paths = await import('node:path')
const { execFileSync } = await import('node:child_process')
const mainSource = readFileSync(join(root, 'electron/main.ts'), 'utf8')
const ast = ts.createSourceFile('main.ts', mainSource, ts.ScriptTarget.Latest, true)
function ipcHandler(name, bindings) {
  let callback
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(ast) === 'ipcMain.handle' && node.arguments[0]?.text === name) callback = node.arguments[1].getText(ast)
    ts.forEachChild(node, visit)
  }
  visit(ast)
  assert.ok(callback, `missing production handler ${name}`)
  const js = ts.transpileModule(`const handler = ${callback}`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  return new Function('bindings', `with(bindings) { ${js}; return handler; }`)(bindings)
}
const fixture = mkdtempSync(join(tmpdir(), 'deploom-project-boundary-'))
try {
  const repository = join(fixture, 'repo'), working = join(repository, 'front'), workspaceRoot = join(fixture, 'workspace')
  mkdirSync(working, { recursive: true }); mkdirSync(workspaceRoot)
  writeFileSync(join(working, 'package.json'), '{"name":"front"}')
  writeFileSync(join(repository, 'package.json'), '{"name":"root"}')
  writeFileSync(join(repository, 'preserved.txt'), 'never delete user files')
  execFileSync('git', ['init', repository], { stdio: 'pipe' })
  const workspace = { id: 'ws', path: workspaceRoot }, other = { id: 'other', path: workspaceRoot }
  const state = { workspaces: [workspace, other], selectedWorkspaceId: 'ws' }
  const settingsPath = join(workspaceRoot, 'settings.json'), jobs = new Map()
  const bindings = {
    ...fs, ...paths, jobs, readProjectManifest,
    loadState: () => state, saveState: () => {},
    findWorkspace: (current, id) => { const value = current.workspaces.find(w => w.id === id); if (!value) throw Error('missing workspace'); return value },
    selectedWorkspace: current => current.workspaces.find(w => w.id === current.selectedWorkspaceId),
    resolveSettingsPath: () => settingsPath,
    workspaceDetails: async value => ({ workspace: value }),
    readProjects: () => JSON.parse(readFileSync(settingsPath, 'utf8')).projects,
    iterativeAutopilot: { hasSession: () => false },
    spawnCapture: async (command, args, cwd) => {
      try { return { code: 0, stdout: execFileSync(command, args, { cwd, encoding: 'utf8', stdio: 'pipe' }), stderr: '' } }
      catch(error) { return { code: error.status || 1, stdout: '', stderr: String(error.stderr) } }
    },
    // The production handler detects the repo's default branch (never a
    // hardcoded 'master'); the fixture pins the branch to prove the wiring.
    detectDefaultBranch: async () => 'main',
  }
  const layoutStart = mainSource.indexOf('async function resolveProjectGitLayout(')
  const layoutEnd = mainSource.indexOf('function projectPathInWorktree(', layoutStart)
  const layoutJs = ts.transpileModule(mainSource.slice(layoutStart, layoutEnd), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  bindings.resolveProjectGitLayout = new Function('bindings', `with(bindings) { ${layoutJs}; return resolveProjectGitLayout; }`)(bindings)
  const add = ipcHandler('flow:add-project', bindings)
  await add(null, { workspaceId: 'ws', name: 'front', path: working, repositoryPath: repository })
  let saved = JSON.parse(readFileSync(settingsPath, 'utf8')).projects
  assert.equal(saved[0].path, working)
  assert.equal(saved[0].repositoryPath.toLowerCase(), fs.realpathSync.native(repository).toLowerCase())
  assert.equal(saved[0].git.sourceBranch, 'main', 'add-project must store the detected default branch, not a hardcoded master')
  await assert.rejects(add(null, { workspaceId: 'ws', name: 'wrong', path: working, repositoryPath: fixture }), /Git/)
  await assert.rejects(add(null, { workspaceId: 'ws', name: 'missing', path: fixture }), /package.json/)
  await add(null, { workspaceId: 'ws', name: 'root-only', path: repository })
  saved = JSON.parse(readFileSync(settingsPath, 'utf8')).projects
  assert.equal(saved.length, 2, 'root selection must not add discovered subprojects')
  assert.equal(saved[1].path, repository)
  const malformed = join(repository, 'malformed'); mkdirSync(malformed)
  writeFileSync(join(malformed, 'package.json'), '{not-json')
  const beforeMalformed = readFileSync(settingsPath, 'utf8')
  await assert.rejects(add(null, { workspaceId: 'ws', name: 'malformed', path: malformed }), /PROJECT_PACKAGE_JSON_INVALID/)
  assert.equal(readFileSync(settingsPath, 'utf8'), beforeMalformed, 'invalid manifest registration must preserve settings')
  const remove = ipcHandler('flow:remove-workspace', bindings)
  jobs.set('busy', { workspace }); await assert.rejects(remove(null, 'ws'), /активные/); jobs.clear()
  bindings.iterativeAutopilot.hasSession = () => true
  await assert.rejects(remove(null, 'ws'), /активные/)
  bindings.iterativeAutopilot.hasSession = () => false
  const removed = await remove(null, 'ws')
  assert.equal(removed.state.selectedWorkspaceId, 'other')
  assert.ok(fs.existsSync(settingsPath)); assert.ok(fs.existsSync(join(repository, 'preserved.txt')))
  const last = await remove(null, 'other')
  assert.equal(last.details, undefined); assert.equal(last.state.workspaces.length, 0)
  assert.ok(fs.existsSync(working))
} finally { rmSync(fixture, { recursive: true, force: true }) }

console.log('workspace bootstrap and project boundaries: ok')
