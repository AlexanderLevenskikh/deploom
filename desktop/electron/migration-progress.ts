import { createHash } from 'node:crypto'

export type MigrationBranchPlan = {
  branch: string
  // Stable logical branch emitted by the Dashboard. Git may use a continuation
  // alias while scope lookup must still address the original group.
  scopeBranch?: string
  label: string
  packages: string[]
}

export type MigrationPlan = {
  project: string
  baseBranch: string
  mergedBranch: string
  // Dashboard plans may contain any number of work branches and arbitrary
  // bucket names. Keep the configured prefix so unexpected-branch detection
  // does not depend on historical `group-N` naming.
  branchPrefix?: string
  branches: MigrationBranchPlan[]
}

export type GitWorktreeRecord = { path: string; branch?: string }

// `git worktree list --porcelain` keeps prunable records until an explicit
// prune. Their directories no longer exist, so treating them as live makes a
// harmless interrupted run poison every migration-progress refresh.
export function liveGitWorktreeRecords(output: string): GitWorktreeRecord[] {
  return output.split(/\r?\n\r?\n/).flatMap((record) => {
    const lines = record.split(/\r?\n/)
    if (lines.some((line) => line === 'prunable' || line.startsWith('prunable '))) return []
    const pathLine = lines.find((line) => line.startsWith('worktree '))
    if (!pathLine) return []
    const branchLine = lines.find((line) => line.startsWith('branch refs/heads/'))
    return [{ path: pathLine.slice('worktree '.length).trim(), branch: branchLine?.slice('branch refs/heads/'.length) }]
  })
}

// `active` used to be a status of its own, which made the whole row flip every
// time the agent checked out another branch: "is HEAD here right now" is not a
// statement about how much work is done. Checkout is now a separate flag and
// the status describes only verifiable facts about the branch itself, so a
// In the legacy contract, a clean branch carrying all package targets reads
// `ready`. v2 additionally requires an orchestrator execution-completion marker
// so deterministic package materialization alone cannot masquerade as finished
// semantic migration/verification.
export type MigrationBranchStatus = 'waiting' | 'created' | 'partial' | 'changes' | 'ready' | 'integrated' | 'merged'
export type MigrationBranchRuntimePhase = 'planning' | 'queued' | 'starting' | 'running' | 'bootstrapping' | 'verifying' | 'repairing' | 'failed' | 'ready' | 'merging' | 'integration-verifying'

export type MigrationBranchRuntime = {
  phase: MigrationBranchRuntimePhase
  detail?: string
  updatedAt: string
}

export type MigrationBranchProgress = MigrationBranchPlan & {
  status: MigrationBranchStatus
  runtime?: MigrationBranchRuntime
  worktreePath?: string
  worktreeDirtyChanges: number
  integratedInto?: string
  checkedOut: boolean
  metPackages: number
}

export type MigrationProgress = {
  project: string
  mergedBranch: string
  currentBranch: string
  createdBranches: number
  totalBranches: number
  completedDependencies: number
  readyDependencies: number
  activeDependencies: number
  completedBranches: number
  readyBranches: number
  activeBranches: number
  totalDependencies: number
  unmetPackages: string[]
  dirty: boolean
  dirtyChanges: number
  branches: MigrationBranchProgress[]
  unexpectedBranches: string[]
  // Which git ref the package facts were read from. The whole point of the
  // gate is "did the merged branch reach the agreed targets", so reading
  // package.json off whatever the agent happens to have checked out reports
  // already-merged packages as unmet.
  factsRef: string
  // Exact commit used for cumulative package facts. Planner-only churn must not
  // reset no-progress detection unless this (or health) actually changes.
  factsCommit?: string
  // False when a git query timed out or failed, i.e. the picture may be wrong.
  // Callers must not downgrade recorded progress on an untrustworthy reading.
  trustworthy: boolean
}

export const KNOWN_IDE_OS_ARTIFACT_PATTERNS = ['.idea/', '.vs/', '.vscode/', '.fleet/', '.history/', '*.swp', '*.swo', '*.suo', '*.user', '*.userosscache', '*.sln.docstates', '.DS_Store', 'Thumbs.db', 'desktop.ini'] as const

export function workspaceNoiseGitExcludePathspecs(): string[] {
  return [
    ':(glob,exclude)**/.idea/**', ':(glob,exclude)**/.vs/**', ':(glob,exclude)**/.vscode/**',
    ':(glob,exclude)**/.fleet/**', ':(glob,exclude)**/.history/**',
    ':(glob,exclude)**/*.swp', ':(glob,exclude)**/*.swo', ':(glob,exclude)**/*.suo',
    ':(glob,exclude)**/*.user', ':(glob,exclude)**/*.userosscache', ':(glob,exclude)**/*.sln.docstates',
    ':(glob,exclude)**/.DS_Store', ':(glob,exclude)**/Thumbs.db', ':(glob,exclude)**/desktop.ini',
    ':(glob,exclude)**/*~',
  ]
}

const IDE_OS_DIRECTORIES = new Set(['.idea', '.vs', '.vscode', '.fleet', '.history'])
const IDE_OS_BASENAMES = new Set(['.DS_Store', 'Thumbs.db', 'desktop.ini'])
const IDE_OS_SUFFIXES = ['.swp', '.swo', '.suo', '.user', '.userosscache', '.sln.docstates']

export function isIgnorableIdeOsPath(value: string): boolean {
  const normalized = value.trim().replace(/^"|"$/g, '').replace(/\\/g, '/').replace(/^\.\//, '')
  const parts = normalized.split('/').filter(Boolean)
  const basename = parts.at(-1) ?? ''
  return parts.some((part) => IDE_OS_DIRECTORIES.has(part))
    || IDE_OS_BASENAMES.has(basename)
    || IDE_OS_SUFFIXES.some((suffix) => basename.endsWith(suffix))
    || basename.endsWith('~')
}

export function relevantGitStatusLines(porcelainOutput: string): string[] {
  return porcelainOutput.split(/\r?\n/).filter((line) => {
    if (!line.trim()) return false
    // Editor/OS workspace state is not dependency-migration state. Ignore it
    // regardless of whether Git reports it as untracked or tracked/modified.
    const path = line.length >= 4 && line[2] === ' ' ? line.slice(3) : line.trim()
    const destination = path.includes(' -> ') ? path.split(' -> ').at(-1) ?? path : path
    return !isIgnorableIdeOsPath(destination)
  })
}

export function relevantGitStatus(porcelainOutput: string): string {
  return relevantGitStatusLines(porcelainOutput).join('\n')
}

// `??` (untracked) porcelain entries can't reflect an unmerged branch — a stray, ungitignored
// file unrelated to the migration shouldn't block completion or burn auto-resume attempts.
export function countTrackedDirtyChanges(porcelainOutput: string): number {
  return porcelainOutput.split(/\r?\n/).filter((line) => line.trim() && !line.startsWith('??')).length
}

// Identifies only known IDE/OS paths for diagnostics and tests. Runtime gates
// filter these entries in memory: DepLoom must not stash, delete or
// commit a user's IDE state merely because the project is open in an editor.
export function strayUntrackedPaths(porcelainOutput: string): string[] {
  return porcelainOutput
    .split(/\r?\n/)
    .filter((line) => line.startsWith('?? '))
    .map((line) => line.slice(3).trim())
    .filter((path) => path && isIgnorableIdeOsPath(path))
}

// Which of a fixed set of canonical IDE/OS-noise patterns a project's
// existing .gitignore content doesn't already cover -- exact-line match only
// (no glob-equivalence reasoning), so a project that already spells the same
// pattern differently gets a harmless duplicate line rather than a missed
// gap; stashStrayUntrackedFiles remains the safety net regardless.
export function missingGitignorePatterns(existingContent: string, patterns: readonly string[]): string[] {
  const existingLines = new Set(existingContent.split(/\r?\n/).map((line) => line.trim()))
  return patterns.filter((pattern) => !existingLines.has(pattern))
}

function jsonSection(markdown: string, heading: string): unknown {
  const headingIndex = markdown.indexOf(heading)
  if (headingIndex < 0) return undefined
  const fenceStart = markdown.indexOf('```json', headingIndex)
  if (fenceStart < 0) return undefined
  const contentStart = fenceStart + '```json'.length
  const fenceEnd = markdown.indexOf('```', contentStart)
  if (fenceEnd < 0) return undefined
  try {
    return JSON.parse(markdown.slice(contentStart, fenceEnd).trim()) as unknown
  } catch {
    return undefined
  }
}

export function migrationScopeManifestFromPrompt(markdown: string): Record<string, unknown> | undefined {
  const value = jsonSection(markdown, '## Exact compact scope manifest')
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

export type ScopeAction = { project: string; package: string; section: string; current: string; target: string; action: string }

function scopeActions(manifest: Record<string, unknown>, projectName: string): ScopeAction[] {
  const columns = Array.isArray(manifest.columns) ? manifest.columns.filter((value): value is string => typeof value === 'string') : undefined
  if (!Array.isArray(manifest.rows)) return []
  return manifest.rows.flatMap((value): ScopeAction[] => {
    const raw = Array.isArray(value) && columns ? Object.fromEntries(columns.map((column, index) => [column, value[index]])) : value
    if (!raw || typeof raw !== 'object') return []
    const row = raw as Record<string, unknown>
    const project = String(row.project ?? '')
    const packageName = String(row.package ?? row.name ?? '')
    if (project !== projectName || !packageName || row.shouldUpdate !== true) return []
    return [{ project, package: packageName, section: String(row.section ?? ''), current: String(row.current ?? ''), target: String(row.target ?? ''), action: String(row.action ?? 'update') }]
  })
}

export function scopeActionsFromPrompt(markdown: string, projectName: string): ScopeAction[] {
  const manifest = migrationScopeManifestFromPrompt(markdown)
  return manifest ? scopeActions(manifest, projectName) : []
}

export function scopeTargetsFromPrompt(markdown: string, projectName: string): Record<string, string> {
  return Object.fromEntries(scopeActionsFromPrompt(markdown, projectName).map((row) => [row.package, row.target]))
}

export type ProvenDependencyEnvelope = {
  schemaVersion: 5
  proofSchema: string
  toolBuildId: string
  envelopeKey: string
  project: string
  mode: string
  sourceHead: string
  sourceSnapshotKey: string
  assignmentKey: string
  resolverInputKey: string
  fixedResolverInputsKey: string
  preparationProofKey: string
  projectProofKey: string
  observedResolvedHash: string
  resolvedStateKey: string
  resolvedLockfilePath: string
  resolvedLockfileHash: string
  exactDirectAssignment: Record<string, string>
  removals: string[]
  verificationCommands: string[]
  projectChecks: string
  resolverProofStatus: string
  preparationProofStatus: string
  projectProofStatus: string
}

function canonicalProofJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalProofJson).join(',')}]`
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b))
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalProofJson(item)}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function proofEnvelopeContentKey(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  const payload = Object.fromEntries(
    Object.entries(value as Record<string, unknown>).filter(([key]) => key !== 'envelopeKey'),
  )
  return createHash('sha256').update(canonicalProofJson(payload)).digest('hex')
}

export function proofEnvelopeFromPrompt(markdown: string, projectName: string): ProvenDependencyEnvelope | undefined {
  const manifest = migrationScopeManifestFromPrompt(markdown)
  const envelopes = manifest?.proofEnvelopes
  if (!envelopes || typeof envelopes !== 'object' || Array.isArray(envelopes)) return undefined
  const raw = (envelopes as Record<string, unknown>)[projectName]
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
  return raw as ProvenDependencyEnvelope
}

export function validateScopeProofEnvelope(
  markdown: string,
  projectName: string,
): { ok: boolean; reason: string; envelope?: ProvenDependencyEnvelope } {
  const manifest = migrationScopeManifestFromPrompt(markdown)
  const envelope = proofEnvelopeFromPrompt(markdown, projectName)
  if (!manifest || !envelope) return { ok: false, reason: 'proof envelope missing' }
  if (envelope.schemaVersion !== 5 || envelope.project !== projectName) return { ok: false, reason: 'proof envelope project/schema mismatch' }
  if (!/^[0-9a-f]{64}$/i.test(envelope.toolBuildId || '')) return { ok: false, reason: 'proof envelope tool build identity missing' }
  if (typeof manifest.targetMode !== 'string' || envelope.mode !== manifest.targetMode) return { ok: false, reason: 'proof envelope target mode mismatch' }
  if (!envelope.envelopeKey || proofEnvelopeContentKey(envelope) !== envelope.envelopeKey) return { ok: false, reason: 'proof envelope content hash mismatch' }
  if (!envelope.sourceHead || !envelope.sourceSnapshotKey || !envelope.assignmentKey || !envelope.resolverInputKey) return { ok: false, reason: 'proof envelope identity incomplete' }
  if (!/^[0-9a-f]{64}$/i.test(envelope.fixedResolverInputsKey || '')) return { ok: false, reason: 'proof envelope fixed source identity missing' }
  if (!envelope.exactDirectAssignment || typeof envelope.exactDirectAssignment !== 'object' || Array.isArray(envelope.exactDirectAssignment)) return { ok: false, reason: 'proof envelope assignment missing' }
  if (!Array.isArray(envelope.removals)) return { ok: false, reason: 'proof envelope removals invalid' }

  const removals = new Set(envelope.removals.map(String))
  const actions = scopeActions(manifest, projectName)
  if (actions.length && envelope.resolverProofStatus !== 'passed') return { ok: false, reason: `resolver proof status is ${envelope.resolverProofStatus || 'missing'}` }
  if (actions.length && !/^[0-9a-f]{64}$/i.test(envelope.observedResolvedHash || '')) {
    return { ok: false, reason: 'proof envelope observed resolved hash missing' }
  }
  if (actions.length && !/^[0-9a-f]{64}$/i.test(envelope.resolvedStateKey || '')) {
    return { ok: false, reason: 'proof envelope resolved state key missing' }
  }
  if (actions.length && !envelope.resolvedLockfilePath) {
    return { ok: false, reason: 'proof envelope resolved lockfile path missing' }
  }
  if (actions.length && !/^[0-9a-f]{64}$/i.test(envelope.resolvedLockfileHash || '')) {
    return { ok: false, reason: 'proof envelope resolved lockfile hash missing' }
  }

  for (const action of actions) {
    const expected = envelope.exactDirectAssignment[action.package]
    if (expected !== action.target) return { ok: false, reason: `proof assignment mismatch for ${action.package}: envelope=${expected ?? '<missing>'}, prompt=${action.target}` }
    if (action.action === 'remove' && !removals.has(action.package)) return { ok: false, reason: `prompt removes ${action.package} outside proven removals` }
    if (action.action === 'update' && removals.has(action.package)) return { ok: false, reason: `prompt updates ${action.package} although proof requires removal` }
    if (!['update', 'remove'].includes(action.action)) return { ok: false, reason: `unsupported actionable proof action ${action.action}` }
  }
  return { ok: true, reason: 'prompt scope is an exact subset of ProofEnvelope', envelope }
}

function versionExactlyMatches(actual: string, target: string): boolean {
  // This predicate protects proof conformance, not UI progress. The Control
  // Plane materializes exact pins; ranges or higher versions are a different
  // dependency state and therefore require a new proof.
  return actual.trim() === target.trim()
}

export function satisfiedScopePackagesFromPrompt(markdown: string, projectName: string, packageJson: unknown): Set<string> | undefined {
  const manifest = migrationScopeManifestFromPrompt(markdown)
  if (!manifest || !packageJson || typeof packageJson !== 'object') return undefined
  const root = packageJson as Record<string, unknown>
  const satisfied = new Set<string>()
  for (const action of scopeActions(manifest, projectName)) {
    const section = root[action.section]
    const spec = section && typeof section === 'object' ? (section as Record<string, unknown>)[action.package] : undefined
    if (action.action === 'remove' ? spec === undefined : typeof spec === 'string' && versionExactlyMatches(spec, action.target)) satisfied.add(action.package)
  }
  return satisfied
}

export function migrationPlanFromPrompt(markdown: string, projectName: string): MigrationPlan | undefined {
  const value = jsonSection(markdown, '## Branch plan')
  if (!Array.isArray(value)) return undefined
  const raw = value.find((item) => item && typeof item === 'object' && (item as { project?: unknown }).project === projectName) as Record<string, unknown> | undefined
  if (!raw || !Array.isArray(raw.branches)) return undefined
  const prefix = typeof raw.branchPrefix === 'string' ? raw.branchPrefix : typeof raw.base === 'string' ? raw.base : ''
  const branches = raw.branches.flatMap((item): MigrationBranchPlan[] => {
    if (!item || typeof item !== 'object') return []
    const branch = typeof (item as Record<string, unknown>).branch === 'string' ? String((item as Record<string, unknown>).branch) : ''
    if (!branch) return []
    const scopeBranch = typeof (item as Record<string, unknown>).scopeBranch === 'string'
      ? String((item as Record<string, unknown>).scopeBranch)
      : typeof (item as Record<string, unknown>).sourceBranch === 'string' ? String((item as Record<string, unknown>).sourceBranch) : branch
    const packages = Array.isArray((item as Record<string, unknown>).packages)
      ? ((item as Record<string, unknown>).packages as unknown[]).filter((pkg): pkg is string => typeof pkg === 'string')
      : []
    const bucket = typeof (item as Record<string, unknown>).bucket === 'string' ? String((item as Record<string, unknown>).bucket) : ''
    const label = bucket || (prefix && branch.startsWith(`${prefix}-`) ? branch.slice(prefix.length + 1) : branch)
    return [{ branch, scopeBranch, label, packages }]
  })
  if (!branches.length) return undefined
  const mergedBranch = typeof raw.merged === 'string'
    ? raw.merged
    : typeof raw.mergedBranch === 'string' ? raw.mergedBranch : ''
  const baseBranch = typeof raw.base === 'string'
    ? raw.base
    : typeof raw.baseBranch === 'string' ? raw.baseBranch : ''
  return { project: projectName, baseBranch, mergedBranch, ...(prefix ? { branchPrefix: prefix } : {}), branches }
}

export function continuationMigrationPlan(
  plan: MigrationPlan,
  mergedButUnmetBranches: ReadonlySet<string>,
  refs: readonly string[],
): MigrationPlan {
  if (!plan.mergedBranch || !plan.branches.some((branch) => mergedButUnmetBranches.has(branch.branch))) return plan
  const occupied = new Set(refs.flatMap((ref) => [ref, ref.startsWith('origin/') ? ref.slice('origin/'.length) : ref]))
  for (const branch of plan.branches) occupied.add(branch.branch)
  const prefix = plan.mergedBranch.replace(/-merged(?:-\d+)?$/, '') || plan.mergedBranch
  let sequence = 1
  const branches = plan.branches.map((branch) => {
    if (!mergedButUnmetBranches.has(branch.branch)) return branch
    let name = prefix + '-continuation-' + sequence
    while (occupied.has(name)) {
      sequence += 1
      name = prefix + '-continuation-' + sequence
    }
    occupied.add(name)
    sequence += 1
    return { ...branch, branch: name, scopeBranch: branch.scopeBranch ?? branch.branch, label: name.startsWith(prefix + '-') ? name.slice(prefix.length + 1) : name }
  })
  return { ...plan, baseBranch: plan.mergedBranch, branches }
}

export function adoptPreferredScopeBranches(
  plan: MigrationPlan,
  worktreeTargetCounts: Readonly<Record<string, number>>,
): MigrationPlan {
  const claimed = new Set(plan.branches.map((branch) => branch.branch))
  let changed = false
  const branches = plan.branches.map((branch) => {
    const scopeBranch = branch.scopeBranch
    if (!scopeBranch || scopeBranch === branch.branch || claimed.has(scopeBranch)) return branch
    const currentTargets = worktreeTargetCounts[branch.branch] ?? -1
    const scopeTargets = worktreeTargetCounts[scopeBranch] ?? -1
    if (scopeTargets <= currentTargets) return branch
    claimed.delete(branch.branch)
    claimed.add(scopeBranch)
    changed = true
    return { ...branch, branch: scopeBranch, label: scopeBranch }
  })
  return changed ? { ...plan, branches } : plan
}

export function adoptEmptyContinuationBranches(
  plan: MigrationPlan,
  refs: readonly string[],
  emptyBranches: ReadonlySet<string>,
): MigrationPlan {
  const prefix = (plan.mergedBranch.replace(/-merged(?:-\d+)?$/, '') || plan.mergedBranch) + '-continuation-'
  const existing = new Set(refs.flatMap((ref) => [ref, ref.startsWith('origin/') ? ref.slice('origin/'.length) : ref]))
  const candidates = [...emptyBranches]
    .filter((branch) => branch.startsWith(prefix))
    .sort((left, right) => left.localeCompare(right, undefined, { numeric: true }))
  const claimed = new Set(plan.branches.filter((branch) => existing.has(branch.branch)).map((branch) => branch.branch))
  let changed = false
  const branches = plan.branches.map((branch) => {
    if (!branch.branch.startsWith(prefix) || existing.has(branch.branch)) return branch
    const candidate = candidates.find((name) => !claimed.has(name))
    if (!candidate) return branch
    claimed.add(candidate)
    changed = true
    return { ...branch, branch: candidate, label: candidate.slice(prefix.length - 'continuation-'.length) }
  })
  return changed ? { ...plan, branches } : plan
}


function samePackageSet(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false
  const a = [...left].sort()
  const b = [...right].sort()
  return a.every((value, index) => value === b[index])
}

// Residual replans deliberately allocate continuation branches. A later prompt
// may be regenerated from the cumulative merged tree and no longer name a
// still-useful continuation ref even though that ref was created by Dependency
// Flow itself in an earlier persisted plan. Reuse such a branch when its exact
// package scope matches one current branch and the caller has proved the ref is
// reusable (i.e. it has unique work and is not already merged). This avoids
// classifying our own continuation topology as "agent invented scope".
export function adoptHistoricalContinuationBranches(
  plan: MigrationPlan,
  historicalPlans: readonly MigrationPlan[],
  reusableBranches: ReadonlySet<string>,
): MigrationPlan {
  const root = plan.mergedBranch.replace(/-merged(?:-\d+)?$/, '') || plan.mergedBranch
  const prefix = root + '-continuation-'
  const historical = historicalPlans
    .flatMap((item) => item.project === plan.project ? item.branches : [])
    .filter((branch) => branch.branch.startsWith(prefix) && reusableBranches.has(branch.branch))
  if (!historical.length) return plan

  const claimed = new Set(plan.branches.map((branch) => branch.branch))
  let changed = false
  const branches = plan.branches.map((branch) => {
    if (reusableBranches.has(branch.branch)) return branch
    const candidate = historical.find((item) =>
      !claimed.has(item.branch)
      && samePackageSet(item.packages, branch.packages)
      && (!item.scopeBranch || !branch.scopeBranch || item.scopeBranch === branch.scopeBranch || item.scopeBranch === branch.branch || branch.scopeBranch === item.branch)
    )
    if (!candidate) return branch
    claimed.add(candidate.branch)
    changed = true
    return {
      ...branch,
      branch: candidate.branch,
      scopeBranch: candidate.scopeBranch ?? branch.scopeBranch ?? branch.branch,
      label: candidate.label || candidate.branch.slice(prefix.length),
    }
  })
  return changed ? { ...plan, branches } : plan
}

export function recoverContinuationScopeBranches(markdown: string, projectName: string, plan: MigrationPlan): MigrationPlan {
  const manifest = migrationScopeManifestFromPrompt(markdown)
  const columns = Array.isArray(manifest?.columns) ? manifest.columns.map(String) : []
  if (!Array.isArray(manifest?.rows) || !columns.length) return plan
  const projectIndex = columns.indexOf('project')
  const packageIndex = columns.indexOf('package')
  const groupIndex = columns.indexOf('group')
  if (projectIndex < 0 || packageIndex < 0 || groupIndex < 0) return plan
  const groupsByPackage = new Map<string, string>()
  for (const value of manifest.rows) {
    if (!Array.isArray(value) || String(value[projectIndex] ?? '') !== projectName) continue
    const packageName = String(value[packageIndex] ?? '')
    const group = String(value[groupIndex] ?? '')
    if (packageName && group) groupsByPackage.set(packageName, group)
  }
  const root = plan.mergedBranch.replace(/-merged(?:-\d+)?$/, '') || plan.mergedBranch
  let changed = false
  const branches = plan.branches.map((branch) => {
    if (!branch.branch.startsWith(root + '-continuation-')) return branch
    if (branch.scopeBranch && branch.scopeBranch !== branch.branch && !branch.scopeBranch.startsWith(root + '-continuation-')) return branch
    const groups = new Set(branch.packages.map((packageName) => groupsByPackage.get(packageName)).filter((group): group is string => Boolean(group)))
    if (groups.size !== 1) return branch
    changed = true
    return { ...branch, scopeBranch: root + '-group-' + [...groups][0] }
  })
  return changed ? { ...plan, branches } : plan
}

export function replaceMigrationPlanInPrompt(markdown: string, projectName: string, plan: MigrationPlan): string | undefined {
  const headingIndex = markdown.indexOf('## Branch plan')
  if (headingIndex < 0) return undefined
  const fence = '\x60\x60\x60json'
  const fenceStart = markdown.indexOf(fence, headingIndex)
  if (fenceStart < 0) return undefined
  const contentStart = fenceStart + fence.length
  const fenceEnd = markdown.indexOf('\x60\x60\x60', contentStart)
  if (fenceEnd < 0) return undefined
  let value: unknown
  try {
    value = JSON.parse(markdown.slice(contentStart, fenceEnd).trim()) as unknown
  } catch {
    return undefined
  }
  if (!Array.isArray(value)) return undefined
  const raw = value.find((item) => item && typeof item === 'object' && (item as { project?: unknown }).project === projectName) as Record<string, unknown> | undefined
  if (!raw || !Array.isArray(raw.branches) || raw.branches.length !== plan.branches.length) return undefined
  raw.base = plan.baseBranch
  if ('baseBranch' in raw) raw.baseBranch = plan.baseBranch
  raw.merged = plan.mergedBranch
  if ('mergedBranch' in raw) raw.mergedBranch = plan.mergedBranch
  raw.branches = raw.branches.map((item, index) => {
    const source = item && typeof item === 'object' ? item as Record<string, unknown> : {}
    const scopeBranch = plan.branches[index].scopeBranch
      ?? (typeof source.scopeBranch === 'string' ? source.scopeBranch : typeof source.branch === 'string' ? source.branch : plan.branches[index].branch)
    return { ...source, branch: plan.branches[index].branch, scopeBranch, bucket: plan.branches[index].label, packages: plan.branches[index].packages }
  })
  const replacement = '\n' + JSON.stringify(value, null, 2) + '\n'
  return markdown.slice(0, contentStart) + replacement + markdown.slice(fenceEnd)
}

// A continuation branch may deliberately read its package scope from an old
// logical group, but every operational instruction must still name the new
// Git branch. Generated prompts contain both prose and immutable JSON facts;
// only non-JSON text is rebound here, while Branch plan is rewritten through
// the structured helper above and retains scopeBranch for dashboard lookup.
export function rebindMigrationPromptBranchIdentity(markdown: string, projectName: string, scopeBranch: string, plan: MigrationPlan): string | undefined {
  const gitBranches = new Set(plan.branches.map((branch) => branch.branch))
  if (plan.branches.length !== 1 || gitBranches.size !== 1) return undefined
  const gitBranch = plan.branches[0].branch
  const structured = replaceMigrationPlanInPrompt(markdown, projectName, plan)
  if (!structured || scopeBranch === gitBranch) return structured
  const replaceIdentity = (value: string) => value.split(scopeBranch).join(gitBranch)
  const fencePattern = /```([^\r\n]*)\r?\n[\s\S]*?```/g
  let cursor = 0
  let result = ''
  for (const match of structured.matchAll(fencePattern)) {
    const index = match.index ?? 0
    result += replaceIdentity(structured.slice(cursor, index))
    const language = String(match[1] ?? '').trim().toLowerCase()
    result += language === 'json' ? match[0] : replaceIdentity(match[0])
    cursor = index + match[0].length
  }
  return result + replaceIdentity(structured.slice(cursor))
}

// Work branches are no longer tied to numeric display groups. Modern plans use
// arbitrary compatibility/subgroup/package buckets (for example
// `libs-peer-vite-vitest-...` or `libs-package-jsdom`). Preserve unexpected
// branch detection by using the explicit Branch-plan prefix, with a legacy
// numeric-stem fallback for old prompts that did not serialize branchPrefix.
function inferWorkBranchPrefix(plan: MigrationPlan): string | undefined {
  const explicit = String(plan.branchPrefix || '').trim().replace(/-+$/, '')
  if (explicit) {
    // Continuation replans keep the original branchPrefix (for example `libs`)
    // while their current work branches live in `libs-continuation-*`. Using
    // the broad `libs-` prefix would make historical `libs-group-*` refs look
    // like newly invented scope. Narrow the namespace only for an all-
    // continuation plan; sibling continuation refs still remain detectable.
    const continuationPrefix = explicit + '-continuation-'
    if (plan.branches.every((branch) => branch.branch.startsWith(continuationPrefix))) {
      return continuationPrefix
    }
    if (plan.branches.every((branch) => branch.branch.startsWith(explicit + '-'))) {
      return explicit + '-'
    }
  }

  const base = String(plan.baseBranch || '').trim().replace(/-+$/, '')
  if (base && plan.branches.every((branch) => branch.branch.startsWith(base + '-'))) return base + '-'

  const legacyMatches = plan.branches.map((branch) => /^(.*-)\d+$/.exec(branch.branch))
  if (!legacyMatches.length || legacyMatches.some((match) => !match)) return undefined
  const stems = new Set(legacyMatches.map((match) => match![1]))
  return stems.size === 1 ? [...stems][0] : undefined
}

function isReservedWorkflowBranch(name: string, plan: MigrationPlan, prefix: string): boolean {
  if (name === plan.baseBranch || name === plan.mergedBranch) return true
  const suffix = name.startsWith(prefix) ? name.slice(prefix.length) : name
  return suffix === 'release' || suffix.startsWith('release-') || suffix.startsWith('continuation-')
}

// A branch's own git ancestry answers "did this branch's commits land
// somewhere they shouldn't have" -- but ancestry-of-base is not evidence of
// that: a branch just created from base is trivially "contained by" base at
// the identical commit, which is its normal, expected state before any work
// exists on it, not a sign it was ever merged anywhere. Observed for real:
// the orchestrator now creates a branch as its own step before the agent
// commits anything, and the next 5s UI poll read that window as "влита не в
// merged" the instant the branch was created. `baseBranch` is deliberately
// excluded from the candidate targets for exactly this reason -- only
// sibling work branches can meaningfully represent a wrong-target merge.
export function integratedBranchTargets(
  plan: MigrationPlan,
  mergedBranches: ReadonlySet<string>,
  containment: ReadonlyArray<readonly [string, ReadonlySet<string>]>,
): Record<string, string> {
  const candidates = plan.branches.map((branch) => branch.branch)
  return Object.fromEntries(containment.flatMap(([branch, containers]) => {
    if (mergedBranches.has(branch)) return []
    const target = candidates.find((candidate) => candidate !== branch && candidate !== plan.mergedBranch && containers.has(candidate))
    return target ? [[branch, target] as const] : []
  }))
}

export type MigrationProgressInput = {
  plan: MigrationPlan
  refs: string[]
  currentBranch: string
  mergedBranches: string[]
  dirtyChanges?: number
  integratedBranches?: Record<string, string>
  // Scope targets already satisfied on the merged branch — the gate's subject.
  satisfiedPackages?: ReadonlySet<string>
  // Scope targets satisfied on each individual work branch, used only to tell
  // a finished-but-unmerged branch from one that was merely created.
  branchSatisfiedPackages?: Record<string, ReadonlySet<string>>
  branchWorktreeSatisfiedPackages?: Record<string, ReadonlySet<string>>
  branchWorktreeDirtyChanges?: Readonly<Record<string, number>>
  branchWorktreePaths?: Readonly<Record<string, string>>
  runtimeByBranch?: Readonly<Record<string, MigrationBranchRuntime>>
  // Undefined preserves the legacy contract where reaching package targets
  // alone made a clean work branch ready. v2 runs pass a set explicitly so a
  // control-plane materialized package.json cannot be merged before bounded
  // semantic Executor batches + group verification actually finish.
  executionCompletedBranches?: ReadonlySet<string>
  // Refs pointing at exactly the merged baseline have no unique commits and
  // therefore cannot carry invented scope.
  emptyBranches?: readonly string[]
  factsRef?: string
  factsCommit?: string
  trustworthy?: boolean
}

export function buildMigrationProgress(input: MigrationProgressInput): MigrationProgress {
  const { plan, refs, currentBranch, mergedBranches } = input
  const dirtyChanges = input.dirtyChanges ?? 0
  const integratedBranches = input.integratedBranches ?? {}
  const satisfiedPackages = input.satisfiedPackages
  const emptyBranches = new Set(input.emptyBranches ?? [])
  const existing = new Set(refs.flatMap((ref) => [ref, ref.startsWith('origin/') ? ref.slice('origin/'.length) : ref]))
  const merged = new Set(mergedBranches)
  const branches = plan.branches.map((branch): MigrationBranchProgress => {
    const integratedInto = integratedBranches[branch.branch]
    const branchSatisfied = input.branchSatisfiedPackages?.[branch.branch]
    const worktreeSatisfied = input.branchWorktreeSatisfiedPackages?.[branch.branch]
    const worktreeDirtyChanges = input.branchWorktreeDirtyChanges?.[branch.branch] ?? 0
    const displayedSatisfied = worktreeSatisfied ?? branchSatisfied
    const metPackages = displayedSatisfied ? branch.packages.filter((name) => displayedSatisfied.has(name)).length : 0
    const committedMetPackages = branchSatisfied ? branch.packages.filter((name) => branchSatisfied.has(name)).length : 0
    const carriesAllTargets = Boolean(branchSatisfied) && committedMetPackages === branch.packages.length
    const executionComplete = input.executionCompletedBranches === undefined || input.executionCompletedBranches.has(branch.branch)
    // Ancestry alone is not evidence of work: a branch created at the base
    // commit and never committed to is trivially an ancestor of everything,
    // so it reported as merged while none of its packages had been touched.
    // Claiming a branch is done must require the branch to carry its targets.
    const doneAndMerged = !emptyBranches.has(branch.branch) && merged.has(branch.branch) && (branchSatisfied === undefined || carriesAllTargets)
    return {
      ...branch,
      status: doneAndMerged
        ? 'merged'
        : integratedInto && !merged.has(branch.branch) ? 'integrated'
          : !existing.has(branch.branch) ? 'waiting'
            : carriesAllTargets && executionComplete ? 'ready'
              : worktreeDirtyChanges > 0 ? 'changes'
                : metPackages > 0 ? 'partial' : 'created',
      ...(input.runtimeByBranch?.[branch.branch] ? { runtime: input.runtimeByBranch[branch.branch] } : {}),
      ...(input.branchWorktreePaths?.[branch.branch] ? { worktreePath: input.branchWorktreePaths[branch.branch] } : {}),
      worktreeDirtyChanges,
      ...(integratedInto && !merged.has(branch.branch) ? { integratedInto } : {}),
      checkedOut: currentBranch === branch.branch,
      metPackages,
    }
  })
  const workPrefix = inferWorkBranchPrefix(plan)
  const planBranchNames = new Set(plan.branches.map((branch) => branch.branch))
  const unexpectedBranches = workPrefix
    ? [...existing].filter((name) =>
        name.startsWith(workPrefix)
        && !planBranchNames.has(name)
        && !emptyBranches.has(name)
        && !isReservedWorkflowBranch(name, plan, workPrefix)
      ).sort()
    : []
  const plannedPackages = [...new Set(branches.flatMap((branch) => branch.packages))]
  const completedDependencies = satisfiedPackages
    ? plannedPackages.filter((packageName) => satisfiedPackages.has(packageName)).length
    : branches.filter((branch) => branch.status === 'merged').reduce((total, branch) => total + branch.packages.length, 0)
  const unmetPackages = satisfiedPackages ? plannedPackages.filter((packageName) => !satisfiedPackages.has(packageName)) : []
  const readyPackageNames = new Set(branches.filter((branch) => branch.status === 'ready').flatMap((branch) => branch.packages))
  const activeRuntimePhases: MigrationBranchRuntimePhase[] = ['starting', 'running', 'bootstrapping', 'verifying', 'repairing', 'merging', 'integration-verifying']
  const activeRuntimeBranches = branches.filter((branch) => branch.runtime && activeRuntimePhases.includes(branch.runtime.phase))
  const activePackageNames = new Set(activeRuntimeBranches.flatMap((branch) => branch.packages))
  return {
    project: plan.project,
    mergedBranch: plan.mergedBranch,
    currentBranch,
    createdBranches: branches.filter((branch) => branch.status !== 'waiting').length,
    totalBranches: branches.length,
    completedDependencies,
    readyDependencies: [...readyPackageNames].filter((name) => !satisfiedPackages?.has(name)).length,
    activeDependencies: [...activePackageNames].filter((name) => !satisfiedPackages?.has(name)).length,
    completedBranches: branches.filter((branch) => branch.status === 'merged').length,
    readyBranches: branches.filter((branch) => branch.status === 'ready').length,
    activeBranches: activeRuntimeBranches.length,
    totalDependencies: plannedPackages.length,
    unmetPackages,
    dirty: dirtyChanges > 0,
    dirtyChanges,
    branches,
    unexpectedBranches,
    factsRef: input.factsRef ?? '',
    ...(input.factsCommit ? { factsCommit: input.factsCommit } : {}),
    trustworthy: input.trustworthy ?? true,
  }
}

// Plain-language state for a branch, used both in the UI and in the feedback
// the agent receives. The agent acts on this text, so it must never imply
// unfinished work on a branch that is actually done.
const BRANCH_STATE_TEXT: Record<MigrationBranchStatus, string> = {
  waiting: 'ветка не создана',
  created: 'ветка создана, работа не начата',
  partial: 'часть целей выполнена',
  changes: 'есть незакоммиченные изменения',
  ready: 'готова, но не влита',
  integrated: 'влита не в merged',
  merged: 'завершена',
}

export function migrationBranchStateText(branch: MigrationBranchProgress): string {
  if (branch.status === 'partial' && branch.packages.length > 0 && branch.metPackages === branch.packages.length) {
    return 'package targets достигнуты, но agent/verification ещё не подтверждены'
  }
  const base = branch.status === 'integrated' && branch.integratedInto
    ? `влита в ${branch.integratedInto}, а не в merged`
    : BRANCH_STATE_TEXT[branch.status]
  const detail = ['created', 'partial', 'changes'].includes(branch.status) && branch.packages.length
    ? `, целей достигнуто ${branch.metPackages} из ${branch.packages.length}`
    : ''
  return `${base}${detail}`
}

// The orchestrator loop's own pick of "what to work on next" -- plan order,
// first branch not yet merged. Kept separate from the loop itself only so
// this one decision (not, say, whether an 'integrated' branch should block
// rather than be silently skipped -- the caller still decides that from the
// status it gets back) is independently testable without Electron.
export function nextIncompleteMigrationBranch(progress: MigrationProgress): MigrationBranchProgress | undefined {
  return progress.branches.find((branch) => branch.status !== 'merged')
}

export function migrationDirtyAffectsCompletion(progress: MigrationProgress): boolean {
  if (!progress.dirty) return false
  // Dirty state belongs to the migration gate only while HEAD is on a branch
  // owned by the Branch plan. A failed release commit deliberately leaves a
  // staged/dirty release branch, which must not retroactively turn a fully
  // merged migration back into "incomplete". Package facts are already read
  // from mergedBranch, so release dirt is a next-stage recovery concern.
  return progress.currentBranch === progress.mergedBranch
    || progress.branches.some((branch) => branch.branch === progress.currentBranch)
}

export function migrationCompletionIssues(progress: MigrationProgress): string[] {
  const incomplete = progress.branches.filter((branch) => branch.status !== 'merged')
  return [
    progress.branches.length === 0 ? 'Branch plan не содержит work branches' : '',
    incomplete.length ? `не готовы ветки: ${incomplete.map((branch) => `${branch.branch} (${migrationBranchStateText(branch)})`).join(', ')}` : '',
    progress.unmetPackages.length ? `не выполнены цели согласованного scope в ${progress.factsRef || 'рабочем дереве'}: ${progress.unmetPackages.slice(0, 12).join(', ')}${progress.unmetPackages.length > 12 ? ` и ещё ${progress.unmetPackages.length - 12}` : ''}` : '',
    migrationDirtyAffectsCompletion(progress) ? `в рабочем дереве ветки миграции осталось изменений: ${progress.dirtyChanges}` : '',
  ].filter(Boolean)
}

// A previous run may be interrupted while the orchestrator owns an active
// merge. Any agent that sees this state must preserve it for the bounded
// recovery loop instead of starting/aborting/committing a different merge.
export function mergeInProgressNote(mergeHeadPresent: boolean): string {
  if (!mergeHeadPresent) return ''
  return 'В рабочем дереве уже идёт незавершённый git merge (MERGE_HEAD присутствует): не начинай новый merge, не отменяй его и не создавай merge-коммит самостоятельно. Сохрани текущее Git-состояние — desktop сопоставит MERGE_HEAD с Branch plan и передаст его ограниченному recovery-loop, а финальный merge-коммит создаст оркестратор.'
}

export function migrationStateSummary(progress: MigrationProgress): string {
  const merged = progress.branches.filter((branch) => branch.status === 'merged')
  const remaining = progress.branches.filter((branch) => branch.status !== 'merged')
  return [
    `HEAD сейчас на ${progress.currentBranch || 'неизвестной ветке'}`,
    `влито в ${progress.mergedBranch}: ${merged.length ? merged.map((branch) => branch.branch).join(', ') : 'ничего'}`,
    `осталось: ${remaining.length ? remaining.map((branch) => `${branch.branch} — ${migrationBranchStateText(branch)}`).join('; ') : 'ничего'}`,
    `факты по пакетам прочитаны из ${progress.factsRef || 'рабочего дерева'}`,
  ].join('. ')
}

function manifestRowsByPackage(manifest: Record<string, unknown> | undefined): Map<string, { target: string; shouldUpdate: boolean }> {
  const result = new Map<string, { target: string; shouldUpdate: boolean }>()
  const columns = Array.isArray(manifest?.columns) ? manifest!.columns.filter((value): value is string => typeof value === 'string') : undefined
  if (!manifest || !Array.isArray(manifest.rows)) return result
  for (const value of manifest.rows) {
    const raw = Array.isArray(value) && columns ? Object.fromEntries(columns.map((column, index) => [column, value[index]])) : value
    if (!raw || typeof raw !== 'object') continue
    const row = raw as Record<string, unknown>
    const packageName = String(row.package ?? row.name ?? '')
    if (!packageName) continue
    result.set(packageName, { target: String(row.target ?? ''), shouldUpdate: row.shouldUpdate === true })
  }
  return result
}

// A group-scoped prompt is generated fresh from the live dashboard (so its
// manifest hash is computed by the same code the validator trusts, not
// reimplemented), but the branch plan the user actually reviewed and pinned
// is the already-saved prompt file. Between those two moments the roadmap can
// legitimately change (a fresh generate, an edited target) -- silently
// handing the agent a scope that drifted from what was agreed is exactly the
// kind of gap this whole gate exists to close, so it's checked explicitly
// rather than trusted.
export function migrationGroupScopeDriftIssues(
  freshMarkdown: string,
  savedMarkdown: string,
  projectName: string,
  savedBranchName: string,
  freshBranchName = savedBranchName,
): string[] {
  const freshPlan = migrationPlanFromPrompt(freshMarkdown, projectName)
  const savedPlan = migrationPlanFromPrompt(savedMarkdown, projectName)
  const freshBranch = freshPlan?.branches.find((branch) => branch.branch === freshBranchName)
  const savedBranch = savedPlan?.branches.find((branch) => branch.branch === savedBranchName)
  if (!freshBranch) return ['branch ' + freshBranchName + ' is missing from freshly generated branch plan']
  if (!savedBranch) return ['branch ' + savedBranchName + ' is missing from saved prompt']
  const freshPackages = [...freshBranch.packages].sort()
  const savedPackages = [...savedBranch.packages].sort()
  if (freshPackages.join(',') !== savedPackages.join(',')) {
    return ['набор пакетов ветки ' + savedBranchName + ' (scope ' + freshBranchName + ') изменился: сохранённый [' + savedPackages.join(', ') + '], свежий [' + freshPackages.join(', ') + ']']
  }
  const freshRows = manifestRowsByPackage(migrationScopeManifestFromPrompt(freshMarkdown))
  const savedRows = manifestRowsByPackage(migrationScopeManifestFromPrompt(savedMarkdown))
  const issues: string[] = []
  for (const packageName of freshPackages) {
    const fresh = freshRows.get(packageName)
    const saved = savedRows.get(packageName)
    if (!fresh || !saved) {
      issues.push(packageName + ': manifest row missing from ' + (!fresh ? 'fresh' : 'saved') + ' prompt')
      continue
    }
    if (fresh.target !== saved.target || fresh.shouldUpdate !== saved.shouldUpdate) {
      issues.push(packageName + ': target/shouldUpdate changed (saved target=' + saved.target + ', shouldUpdate=' + saved.shouldUpdate + '; fresh target=' + fresh.target + ', shouldUpdate=' + fresh.shouldUpdate + ')')
    }
  }
  return issues
}


export function migrationBatchScopeDriftIssues(batchMarkdown: string, fullGroupMarkdown: string, expectedPackages: readonly string[]): string[] {
  const batchRows = manifestRowsByPackage(migrationScopeManifestFromPrompt(batchMarkdown))
  const fullRows = manifestRowsByPackage(migrationScopeManifestFromPrompt(fullGroupMarkdown))
  const expected = [...new Set(expectedPackages)].sort()
  const actual = [...batchRows.entries()].filter(([, row]) => row.shouldUpdate).map(([name]) => name).sort()
  const issues: string[] = []
  if (actual.join(',') !== expected.join(',')) {
    issues.push(`execution batch изменился: ожидался [${expected.join(', ')}], получен [${actual.join(', ')}]`)
  }
  for (const packageName of expected) {
    const batch = batchRows.get(packageName)
    const full = fullRows.get(packageName)
    if (!batch || !full) {
      issues.push(`${packageName}: нет записи manifest в ${!batch ? 'batch' : 'полном group'} prompt`)
      continue
    }
    if (batch.target !== full.target || batch.shouldUpdate !== full.shouldUpdate) {
      issues.push(`${packageName}: batch изменил target/shouldUpdate относительно полного group prompt`)
    }
  }
  return issues
}

export function rollbackIncompleteMigrationActions(actions: string[], progress: MigrationProgress, lastAction?: string): string[] {
  if (lastAction === 'release' || actions.includes('release') || migrationCompletionIssues(progress).length === 0) return actions
  const invalidated = new Set(['agent', 'generate', 'audit', 'release', 'commit-state', 'push-workspace'])
  return actions.filter((action) => !invalidated.has(action))
}

// `git diff --check` exits non-zero for *any* whitespace complaint, not only
// leftover conflict markers -- and "trailing whitespace" is exactly what a
// Markdown hard line break (two trailing spaces) looks like. Since the merge
// carries this migration's own doc shards, gating on the exit code alone
// rejects a perfectly resolved merge over formatting the tool itself wrote.
// Only the lines git labels as conflict markers actually block a commit.
export function leftoverConflictMarkerLines(diffCheckOutput: string): string[] {
  return diffCheckOutput
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => /leftover conflict marker/i.test(line))
}

export type DependencyMergeResult = { ok: true; merged: Record<string, string> } | { ok: false }

// A real, confirmed-common shape for this tool's merges: two branch-plan
// groups each bump a *disjoint* set of packages, but because both edits land
// in the same dependencies/devDependencies object, git's line-based merge
// still reports a textual conflict even though nothing semantically
// overlaps. Resolves each dependency key independently against the merge
// base: a key changed on only one side takes that side's value; a key
// changed identically on both sides is a non-issue; a key changed
// *differently* on both sides is a genuine conflict this must not guess at.
export function threeWayMergeDependencyMaps(
  base: Record<string, string> | undefined,
  ours: Record<string, string> | undefined,
  theirs: Record<string, string> | undefined,
): DependencyMergeResult {
  const baseMap = base ?? {}
  const oursMap = ours ?? {}
  const theirsMap = theirs ?? {}
  const merged: Record<string, string> = { ...oursMap }
  for (const key of new Set([...Object.keys(baseMap), ...Object.keys(oursMap), ...Object.keys(theirsMap)])) {
    const baseValue = baseMap[key]
    const oursValue = oursMap[key]
    const theirsValue = theirsMap[key]
    if (oursValue === theirsValue) continue // identical result on both sides (including both removed)
    const oursChanged = oursValue !== baseValue
    const theirsChanged = theirsValue !== baseValue
    if (theirsChanged && !oursChanged) {
      if (theirsValue === undefined) delete merged[key]
      else merged[key] = theirsValue
      continue
    }
    if (oursChanged && !theirsChanged) continue // keep ours -- already the starting point of `merged`
    return { ok: false } // both sides changed this exact package differently -- a real conflict
  }
  return { ok: true, merged }
}

const PACKAGE_JSON_DEPENDENCY_FIELDS = ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'] as const

// Merges only the dependency-map fields three-way; every other field
// (scripts, name, everything else) must already be identical between `ours`
// and `theirs` -- checked explicitly below -- so this can never silently
// drop a real change made outside dependency versions just because it
// wasn't one of the four fields this function knows how to merge.
export function mergePackageJsonThreeWay(baseText: string, oursText: string, theirsText: string): string | undefined {
  let base: Record<string, unknown>
  let ours: Record<string, unknown>
  let theirs: Record<string, unknown>
  try {
    base = JSON.parse(baseText)
    ours = JSON.parse(oursText)
    theirs = JSON.parse(theirsText)
  } catch {
    return undefined
  }
  const stripDependencyFields = (value: Record<string, unknown>): Record<string, unknown> => {
    const copy = { ...value }
    for (const field of PACKAGE_JSON_DEPENDENCY_FIELDS) delete copy[field]
    return copy
  }
  if (JSON.stringify(stripDependencyFields(ours)) !== JSON.stringify(stripDependencyFields(theirs))) return undefined
  const result: Record<string, unknown> = { ...ours }
  for (const field of PACKAGE_JSON_DEPENDENCY_FIELDS) {
    const baseField = base[field]
    const oursField = ours[field]
    const theirsField = theirs[field]
    if (oursField === undefined && theirsField === undefined) continue
    const merge = threeWayMergeDependencyMaps(
      baseField as Record<string, string> | undefined,
      oursField as Record<string, string> | undefined,
      theirsField as Record<string, string> | undefined,
    )
    if (!merge.ok) return undefined
    result[field] = merge.merged
  }
  return `${JSON.stringify(result, null, 2)}\n`
}
