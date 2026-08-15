import { runParallelQueue, selectParallelGroupQueue, selectParallelGroupWave } from '../dist-electron/parallel-groups.js'
import { isToolManagedWorktreePath, restartWorktreeCleanupTargets, toolManagedWorktreeFromLegacyDeferral, toolManagedWorktreePaths } from '../dist-electron/worktree-ownership.js'

const branch = (name, packages, status = 'waiting', checkedOut = false) => ({ branch: name, label: name, packages, status, checkedOut, metPackages: 0 })
const wave = selectParallelGroupWave([
  branch('g1', ['a', 'b']),
  branch('g2', ['c']),
  branch('g3', ['b', 'd']),
  branch('g4', ['e'], 'created'),
], 3)
if (wave.branches.map((item) => item.branch).join(',') !== 'g1,g2,g4') throw new Error('parallel wave must select disjoint waiting/created branches so interrupted tool worktrees can resume: ' + JSON.stringify(wave))
if (!wave.skipped.some((item) => item.startsWith('g3:'))) throw new Error('overlap must be reported')
if (selectParallelGroupWave([branch('g1', ['a']), branch('g2', ['b'])], 1).branches.length) throw new Error('maxParallelGroups=1 must disable worktree parallelism')
if (selectParallelGroupWave([branch('g1', ['a']), branch('g2', ['b'], 'ready')], 4).branches.length) throw new Error('one untouched branch is not a useful parallel wave')
const resumed = selectParallelGroupQueue([branch('partial', ['a'], 'partial', true), branch('dirty', ['b'], 'changes', true), branch('fresh', ['c'], 'created', true)])
if (resumed.branches.map((item) => item.branch).join(',') !== 'partial,dirty,fresh') throw new Error('saved tool-worktree candidates must reach executor ownership checks instead of being forced sequential: ' + JSON.stringify(resumed))

const queue = selectParallelGroupQueue([branch('g1', ['a']), branch('g2', ['b']), branch('g3', ['c']), branch('g4', ['d'])])
if (queue.branches.length !== 4) throw new Error('scheduler queue must retain work beyond the first concurrency window')
let active = 0
let peak = 0
let thirdStartedBeforeFirstFinished = false
let releaseFirst
const firstGate = new Promise((resolve) => { releaseFirst = resolve })
const queueRun = runParallelQueue(queue.branches, 2, async (_item, index) => {
  active += 1
  peak = Math.max(peak, active)
  if (index === 0) await firstGate
  if (index === 2) thirdStartedBeforeFirstFinished = true
  await new Promise((resolve) => setImmediate(resolve))
  active -= 1
  return index
})
await new Promise((resolve) => setTimeout(resolve, 20))
if (!thirdStartedBeforeFirstFinished) throw new Error('a completed worker must feed the next queued group without waiting for the slowest slot')
releaseFirst()
if ((await queueRun).join(',') !== '0,1,2,3' || peak !== 2) throw new Error('queue must preserve result order and concurrency cap')
let wideActive = 0
let widePeak = 0
let releaseWide
const wideGate = new Promise((resolve) => { releaseWide = resolve })
const wideRun = runParallelQueue(Array.from({ length: 5 }, (_, index) => index), 6, async (index) => {
  wideActive += 1
  widePeak = Math.max(widePeak, wideActive)
  await wideGate
  wideActive -= 1
  return index
})
await new Promise((resolve) => setTimeout(resolve, 20))
if (widePeak !== 5) throw new Error(`configured slots must all open immediately when enough disjoint groups exist: peak=${widePeak}`)
releaseWide()
await wideRun
const mainSource = (await import('node:fs')).readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const ownershipSource = (await import('node:fs')).readFileSync(new URL('../electron/worktree-ownership.ts', import.meta.url), 'utf8')
if (!ownershipSource.includes('realpathSync.native')) throw new Error('Windows short/long TEMP paths must resolve before ownership checks')
for (const required of ['liveMaxParallelGroups', 'configured slots=${maxParallelGroups}', 'parallelWorkerJob(parent, branch)', "'worktree', 'prune', '--expire', 'now'", 'jobId: worker.job.id', 'Причина: ${message.slice(0, 3000)}', 'openCodeTransportOwner(job)', 'if (owner !== job) return', 'client-${randomUUID()}', 'OPENCODE_SERVER_START_ATTEMPTS = 3', 'if (owner.openCodeServerStarting) return owner.openCodeServerStarting']) if (!mainSource.includes(required)) throw new Error(`parallel runtime contract missing: ${required}`)
console.log('Parallel group scheduler contract OK')

if (isToolManagedWorktreePath('/tmp/CaseTemp/dependency-flow-planner-123', '/tmp/casetemp', 'linux')) throw new Error('POSIX ownership must not collapse case-distinct TEMP roots')

const temp = 'C:\\Users\\developer\\AppData\\Local\\Temp'
const toolWorktree = 'C:/Users/developer/AppData/Local/Temp/dependency-flow-worktrees/593c34bb-555d-4024-bb01-ba5da6024042/deps-demo-continuation-2'
const plannerWorktree = 'C:/Users/developer/AppData/Local/Temp/dependency-flow-planner-job-123-1786699527349'
const baselineWorktree = 'C:/Users/developer/AppData/Local/Temp/dependency-flow-job-123-baseline'
if (!isToolManagedWorktreePath(toolWorktree, temp, 'win32')) throw new Error('DepLoom temp worktree must survive Windows slash/case normalization')
if (!isToolManagedWorktreePath(plannerWorktree, temp, 'win32') || !isToolManagedWorktreePath(baselineWorktree, temp, 'win32')) throw new Error('detached planner/baseline worktrees must be owned by DepLoom cleanup')
if (isToolManagedWorktreePath('C:/Users/developer/Desktop/manual-worktree/deps-demo-continuation-2', temp, 'win32')) throw new Error('arbitrary user worktree must not be claimed')
const legacyReason = `PARALLEL_USER_WORKTREE_BLOCKED: branch is checked out in a user-owned worktree ${toolWorktree} at commit 9409794`
if (toolManagedWorktreeFromLegacyDeferral(legacyReason, temp, 'win32') !== toolWorktree) throw new Error('legacy self-worktree deferral must be recognized and clearable')

const restartCleanup = restartWorktreeCleanupTargets([
  { path: 'C:/Users/developer/project', branch: 'deps-old' },
  { path: toolWorktree, branch: 'deps-old' },
  { path: 'C:/Users/developer/Desktop/manual-worktree/deps-other', branch: 'deps-other' },
], ['deps-old', 'deps-other'], 'C:/Users/developer/project', temp, 'win32')
if (restartCleanup.toolManagedPaths.join(',') !== toolWorktree) throw new Error('fresh restart must remove its own temp worktree before deleting the old branch')
if (restartCleanup.blockedUserWorktrees.length !== 1 || restartCleanup.blockedUserWorktrees[0].branch !== 'deps-other') throw new Error('fresh restart must never delete an arbitrary user worktree')
for (const required of [
  "'worktree', 'remove', '--force', path",
  "'worktree', 'prune', '--expire', 'now'",
  'restartWorktreeCleanupTargets(',
  'MIGRATION_RESTART_USER_WORKTREE_BLOCKED',
]) if (!mainSource.includes(required)) throw new Error(`restart worktree cleanup contract missing: ${required}`)
const restartWorktreeRemoveIndex = mainSource.indexOf("'worktree', 'remove', '--force', path")
const restartBranchDeleteIndex = mainSource.indexOf("'branch', '-D', branch", restartWorktreeRemoveIndex)
if (restartWorktreeRemoveIndex < 0 || restartBranchDeleteIndex < 0 || restartWorktreeRemoveIndex > restartBranchDeleteIndex) {
  throw new Error('fresh restart must remove a tool-managed worktree before deleting the branch checked out there')
}

const releaseCleanupPaths = toolManagedWorktreePaths([
  { path: 'C:/Users/developer/project', branch: 'CD-42-release' },
  { path: toolWorktree, branch: 'deps-old' },
  { path: 'C:/Users/developer/Desktop/manual-worktree/deps-other', branch: 'deps-other' },
], 'C:/Users/developer/project', temp, 'win32')
if (releaseCleanupPaths.join(',') !== toolWorktree) throw new Error('release cleanup must select every DepLoom temp worktree and leave user worktrees untouched')
for (const required of [
  'cleanupToolManagedProjectWorktreesAfterRelease(',
  "job.action === 'release' && job.projectName",
  "'worktree', 'prune', '--expire', 'now'",
  'Release cleanup: удалены временные DepLoom worktree',
]) if (!mainSource.includes(required)) throw new Error(`release worktree cleanup contract missing: ${required}`)
