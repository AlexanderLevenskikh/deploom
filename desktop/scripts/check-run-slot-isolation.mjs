// A06+R4: run-action concurrency must be isolated by CANONICAL REPOSITORY, not
// by display name or workspace, and the job slot must be reserved synchronously
// before the first await.
// R4: the canonical identity is the shared GIT WORKTREE ROOT (and main git
// dir for linked worktrees), resolved through realpath -- an exact-string path
// comparison cannot see a monorepo (packages/a vs packages/b), a linked
// worktree, a symlink/junction alias, or nested checkouts. This runs the REAL
// production conflict/reservation functions extracted from main.ts (types
// erased by transpile, dependencies injected as globals) against a REAL git
// monorepo built in a temp dir.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import ts from 'typescript'
import { normalizePathForComparison } from '../dist-electron/process-launcher.js'

const { join, dirname, sep, normalize, resolve } = path

const source = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const start = source.indexOf('function mainGitDirOf(')
const end = source.indexOf('function reserveProjectActionSlot(', start)
const reserveEnd = source.indexOf('let mainWindow: BrowserWindow | null', end)
if (start < 0 || end < 0 || reserveEnd < 0) throw new Error('R4 source slice not found in main.ts')
const slice = source.slice(start, reserveEnd)
const jobs = new Map()
const js = ts.transpileModule(slice, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const factory = new Function(
  'jobs', 'randomUUID', 'join', 'dirname', 'sep', 'normalize', 'resolve',
  'normalizePathForComparison',
  'realpathSync', 'existsSync', 'statSync', 'readFileSync',
  'WORKSPACE_GLOBAL_ACTIONS', 'PROJECT_BACKGROUND_ACTIONS',
  js + '\nreturn { sameRepositoryPath, projectRunConflicts, reserveProjectActionSlot };',
)
const WORKSPACE_GLOBAL_ACTIONS = new Set(['sync-tool', 'generate-all', 'commit-state', 'push-workspace'])
const PROJECT_BACKGROUND_ACTIONS = new Set(['preflight', 'baseline'])
const instance = factory(
  jobs, randomUUID, join, dirname, sep, normalize, resolve, normalizePathForComparison,
  fs.realpathSync, fs.existsSync, fs.statSync, fs.readFileSync,
  WORKSPACE_GLOBAL_ACTIONS, PROJECT_BACKGROUND_ACTIONS,
)
const { projectRunConflicts, reserveProjectActionSlot } = instance

// ---- N3: POSIX path semantics for linked worktrees (in-memory model).
// The shared Git dir of a linked worktree is Git's `commondir` metadata
// (git rev-parse --git-common-dir), resolved RELATIVE to the worktree gitdir --
// never a string split on a 'worktrees' segment, which loses the POSIX root
// ('/repo/main/.git/worktrees/w1' -> 'repo/main/.git') and can be fooled by an
// unrelated parent folder literally named 'worktrees'. Modeled as an in-memory
// POSIX filesystem: no real repository is created or mutated.
{
  const px = path.posix
  const model = {
    '/repo/main/.git': 'dir',
    '/repo/linked/.git': 'file:/repo/main/.git/worktrees/w1',
    '/repo/linked2/.git': 'file:/repo/main/.git/worktrees/w2',
    '/repo/main/.git/worktrees/w1/commondir': 'data:../..',
    '/repo/main/.git/worktrees/w2/commondir': 'data:../..',
    '/tmp/worktrees/other/.git': 'dir',
  }
  const projectDirs = new Set(['/repo/main', '/repo/linked', '/repo/linked2', '/tmp/worktrees/other'])
  const statSync = (p) => {
    const v = model[p]
    if (v === 'dir') return { isDirectory: () => true }
    if (v && v.startsWith('file:')) return { isDirectory: () => false }
    const err = new Error('ENOENT'); err.code = 'ENOENT'; throw err
  }
  const existsSync = (p) => model[p] !== undefined || projectDirs.has(p)
  const readFileSync = (p) => {
    const v = model[p]
    if (v !== undefined && v.startsWith('file:')) return `gitdir: ${v.slice(5)}\n`
    if (v !== undefined && v.startsWith('data:')) return `${v.slice(5)}\n`
    const err = new Error('ENOENT'); err.code = 'ENOENT'; throw err
  }
  const identity = (p) => p
  const posixInstance = factory(
    new Map(), randomUUID, px.join, px.dirname, px.sep, px.normalize, px.resolve, identity,
    identity, existsSync, statSync, readFileSync,
    WORKSPACE_GLOBAL_ACTIONS, PROJECT_BACKGROUND_ACTIONS,
  )
  const posixWs = (id) => ({ id, name: id, path: id, templateRemote: '', toolRemote: '', settingsPath: '', agent: 'claude' })
  const posixJob = (wsId, name, projectPath, action) => ({
    id: randomUUID(), action, workspace: posixWs(wsId), projectName: name, projectPath, cancelled: false,
  })
  const posixConflict = (existing, wsId, name, pathName, action) =>
    posixInstance.projectRunConflicts(existing, posixWs(wsId), { name, path: pathName }, action)

  // Main checkout + linked worktree of the SAME repo: same --git-common-dir.
  const mainJob = posixJob('one', 'main', '/repo/main', 'agent')
  if (!posixConflict(mainJob, 'one', 'linked1', '/repo/linked', 'baseline')) {
    throw new Error('N3: main checkout + linked worktree must conflict in one workspace')
  }
  if (!posixConflict(mainJob, 'two', 'another-ws', '/repo/linked', 'baseline')) {
    throw new Error('N3: main checkout + linked worktree must conflict across workspaces')
  }
  // Two linked worktrees of the SAME repo conflict too.
  const linked1Job = posixJob('one', 'linked1', '/repo/linked', 'agent')
  if (!posixConflict(linked1Job, 'one', 'linked2', '/repo/linked2', 'baseline')) {
    throw new Error('N3: two linked worktrees of one repo must conflict')
  }
  // An INDEPENDENT repo whose parent folder is literally named 'worktrees' is
  // its own identity -- never conflated with anything by segment surgery.
  if (posixConflict(mainJob, 'one', 'other', '/tmp/worktrees/other', 'baseline')) {
    throw new Error('N3: an unrelated repo under a folder named worktrees must not conflict')
  }
}

// ---- Build a real monorepo + an independent repo + a linked worktree.
const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'deploom-r4-'))
const git = (cwd, ...args) => execFileSync('git', args, { cwd, stdio: 'pipe' })
const initRepo = (dir) => {
  fs.mkdirSync(dir, { recursive: true })
  git(dir, 'init', '-q')
  git(dir, 'config', 'user.email', 'check@deploom.local')
  git(dir, 'config', 'user.name', 'contract-check')
  fs.writeFileSync(path.join(dir, 'marker.txt'), 'ok\n')
  git(dir, 'add', '.')
  git(dir, 'commit', '-qm', 'init')
}
try {
  const monorepo = path.join(tmpRoot, 'monorepo')
  fs.mkdirSync(path.join(monorepo, 'packages', 'a'), { recursive: true })
  fs.mkdirSync(path.join(monorepo, 'packages', 'b'), { recursive: true })
  initRepo(monorepo)
  const other = path.join(tmpRoot, 'other')
  fs.mkdirSync(path.join(other, 'packages', 'a'), { recursive: true })
  initRepo(other)
  // A linked worktree of the monorepo (its .git is a gitdir: pointer file).
  const worktree = path.join(tmpRoot, 'wt')
  git(monorepo, 'worktree', 'add', '-q', '-b', 'wt-branch', worktree)
  fs.mkdirSync(path.join(worktree, 'packages', 'x'), { recursive: true })
  // A junction (Windows) / symlink alias of one package dir.
  const alias = path.join(tmpRoot, 'alias-a')
  fs.symlinkSync(path.join(monorepo, 'packages', 'a'), alias,
    process.platform === 'win32' ? 'junction' : 'dir')

  const ws = (id) => ({ id, name: id, path: id, templateRemote: '', toolRemote: '', settingsPath: '', agent: 'claude' })
  const job = (workspace, name, projectPath, action) => ({
    id: randomUUID(), action, workspace: ws(workspace), projectName: name,
    projectPath, cancelled: false,
  })

  // R4: nested packages of ONE monorepo share one worktree root -> conflict.
  if (!projectRunConflicts(job('one', 'a', join(monorepo, 'packages', 'a'), 'agent'), ws('one'), { name: 'b', path: join(monorepo, 'packages', 'b') }, 'baseline')) {
    throw new Error('R4: two packages of one monorepo must conflict')
  }
  // The monorepo root itself vs a nested package.
  if (!projectRunConflicts(job('one', 'root', monorepo, 'agent'), ws('one'), { name: 'b', path: join(monorepo, 'packages', 'b') }, 'agent')) {
    throw new Error('R4: the monorepo root and a nested package must conflict')
  }
  // R4: a LINKED WORKTREE of the same repo conflicts with the main checkout
  // (shared mainGitDir), even though the two worktree roots differ.
  if (!projectRunConflicts(job('one', 'wt', join(worktree, 'packages', 'x'), 'agent'), ws('one'), { name: 'a', path: join(monorepo, 'packages', 'a') }, 'agent')) {
    throw new Error('R4: a linked worktree must conflict with its main checkout')
  }
  // R4: a symlink/junction alias of a package resolves through realpath to the
  // same worktree root -> conflict with its sibling.
  if (!projectRunConflicts(job('one', 'alias', alias, 'agent'), ws('one'), { name: 'b', path: join(monorepo, 'packages', 'b') }, 'agent')) {
    throw new Error('R4: a symlink alias of a monorepo package must conflict with its sibling')
  }
  // Different repositories remain independent (different project names within
  // one workspace, and different workspaces regardless of the name).
  if (projectRunConflicts(job('one', 'a', join(monorepo, 'packages', 'a'), 'agent'), ws('one'), { name: 'b', path: join(other, 'packages', 'a') }, 'baseline')) {
    throw new Error('R4: unrelated repos must stay independent within one workspace (different names)')
  }
  if (projectRunConflicts(job('one', 'a', join(monorepo, 'packages', 'a'), 'agent'), ws('two'), { name: 'a', path: join(other, 'packages', 'a') }, 'agent')) {
    throw new Error('R4: same name in another workspace on a different repo must stay independent')
  }

  // A06: the slot is reserved upfront and released afterwards; a second caller
  // on the same repo loses the race instead of both passing the check.
  const first = reserveProjectActionSlot(ws('one'), { name: 'b', path: join(monorepo, 'packages', 'b') }, 'agent')
  if (!first.reservation || first.conflict) throw new Error('A06: first reservation must win')
  const second = reserveProjectActionSlot(ws('one'), { name: 'a', path: join(monorepo, 'packages', 'a') }, 'baseline')
  if (!second.conflict || second.reservation) throw new Error('A06: concurrent start on the same repo must lose the race')
  // Exchange: the real job replaces the reservation; a third start then loses
  // against the real job.
  jobs.delete(first.reservation.id)
  jobs.set(job('one', 'b', join(monorepo, 'packages', 'b'), 'agent').id, job('one', 'b', join(monorepo, 'packages', 'b'), 'agent'))
  const third = reserveProjectActionSlot(ws('one'), { name: 'a', path: join(monorepo, 'packages', 'a') }, 'agent')
  if (!third.conflict) throw new Error('A06: a third start must lose against the registered real job')
  // Release in finally: after deletion the slot is free again.
  jobs.clear()
  const fourth = reserveProjectActionSlot(ws('one'), { name: 'a', path: join(monorepo, 'packages', 'a') }, 'agent')
  if (!fourth.reservation) throw new Error('A06: the slot must be reusable after release')

  console.log('Run-slot isolation by canonical repository (worktree root) OK')
} finally {
  fs.rmSync(tmpRoot, { recursive: true, force: true })
}
