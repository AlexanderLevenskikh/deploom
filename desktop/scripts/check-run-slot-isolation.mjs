// A06: run-action concurrency must be isolated by CANONICAL REPOSITORY, not by
// display name or workspace, and the job slot must be reserved synchronously
// before the first await so two concurrent starts cannot both pass the check.
// This runs the REAL production conflict/reservation functions extracted from
// main.ts (types erased by transpile, dependencies injected as globals).
import fs from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import ts from 'typescript'
import { normalizePathForComparison } from '../dist-electron/process-launcher.js'

const source = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const start = source.indexOf('function sameRepositoryPath(')
const end = source.indexOf('function reserveProjectActionSlot(', start)
const reserveEnd = source.indexOf('let mainWindow: BrowserWindow | null', end)
if (start < 0 || end < 0 || reserveEnd < 0) throw new Error('A06 source slice not found in main.ts')
const slice = source.slice(start, reserveEnd)
const jobs = new Map()
const js = ts.transpileModule(slice, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const fn = new Function(
  'jobs', 'randomUUID', 'normalize', 'resolve', 'normalizePathForComparison',
  'WORKSPACE_GLOBAL_ACTIONS', 'PROJECT_BACKGROUND_ACTIONS',
  js + '\nreturn { sameRepositoryPath, projectRunConflicts, reserveProjectActionSlot };',
)
const { normalize, resolve } = path
const WORKSPACE_GLOBAL_ACTIONS = new Set(['sync-tool', 'generate-all', 'commit-state', 'push-workspace'])
const PROJECT_BACKGROUND_ACTIONS = new Set(['preflight', 'baseline'])
const { projectRunConflicts, reserveProjectActionSlot } = fn(
  jobs, randomUUID, normalize, resolve, normalizePathForComparison,
  WORKSPACE_GLOBAL_ACTIONS, PROJECT_BACKGROUND_ACTIONS,
)
const ws = (id) => ({ id, name: id, path: 'C:/workspaces/' + id, templateRemote: '', toolRemote: '', settingsPath: '', agent: 'claude' })
const job = (workspace, name, repo, action) => ({
  id: randomUUID(), action, workspace: ws(workspace), projectName: name,
  projectPath: `C:/repos/${repo}`, cancelled: false,
})

// A06: two project aliases of ONE repository MUST conflict, whatever their
// display names, whatever the workspace (the old check keyed on workspace+name
// and explicitly let baseline/preflight overlap on different names).
if (!projectRunConflicts(job('one', 'alias-A', 'shared', 'agent'), ws('one'), { name: 'alias-B', path: 'C:/repos/shared' }, 'baseline')) {
  throw new Error('A06: two aliases of the same repo must conflict even for baseline vs agent')
}
if (!projectRunConflicts(job('one', 'alias-A', 'shared', 'agent'), ws('two'), { name: 'alias-C', path: 'C:/repos/shared' }, 'agent')) {
  throw new Error('A06: the same repo registered in two workspaces must conflict')
}
if (!projectRunConflicts(job('one', 'nested-pkg', 'monorepo', 'agent'), ws('one'), { name: 'nested-pkg-2', path: 'C:/repos/monorepo/packages/b' }, 'agent')) {
  throw new Error('A06: nested packages sharing a git checkout must conflict')
}
// Case/drive-normalized path equality on Windows.
if (!projectRunConflicts(job('one', 'a', 'shared', 'agent'), ws('one'), { name: 'b', path: 'c:/REPOS/shared' }, 'agent')) {
  throw new Error('A06: path comparison must be case-insensitive on Windows')
}
// Different repositories remain independent.
if (projectRunConflicts(job('one', 'a', 'repo-a', 'agent'), ws('one'), { name: 'b', path: 'C:/repos/repo-b' }, 'baseline')) {
  throw new Error('A06: unrelated repos must stay independent')
}
if (projectRunConflicts(job('one', 'a', 'repo-a', 'agent'), ws('two'), { name: 'a', path: 'C:/repos/repo-b' }, 'agent')) {
  throw new Error('A06: same name in another workspace on a different repo must stay independent')
}

// A06: the slot is reserved upfront and released afterwards; a second caller
// on the same repo loses the race instead of both passing the check.
const first = reserveProjectActionSlot(ws('one'), { name: 'alias-B', path: 'C:/repos/shared' }, 'agent')
if (!first.reservation || first.conflict) throw new Error('A06: first reservation must win')
const second = reserveProjectActionSlot(ws('one'), { name: 'alias-A', path: 'C:/repos/shared' }, 'baseline')
if (!second.conflict || second.reservation) throw new Error('A06: concurrent start on the same repo must lose the race')
// Exchange: the real job replaces the reservation; a third start then loses
// against the real job.
jobs.delete(first.reservation.id)
jobs.set(job('one', 'alias-B', 'shared', 'agent').id, job('one', 'alias-B', 'shared', 'agent'))
const third = reserveProjectActionSlot(ws('one'), { name: 'alias-A', path: 'C:/repos/shared' }, 'agent')
if (!third.conflict) throw new Error('A06: a third start must lose against the registered real job')
// Release in finally: after deletion the slot is free again.
jobs.clear()
const fourth = reserveProjectActionSlot(ws('one'), { name: 'alias-A', path: 'C:/repos/shared' }, 'agent')
if (!fourth.reservation) throw new Error('A06: the slot must be reusable after release')

console.log('Run-slot isolation by canonical repository OK')
