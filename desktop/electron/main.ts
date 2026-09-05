import { app, BrowserWindow, dialog, ipcMain, nativeTheme, net, Notification, protocol, session, shell } from 'electron'
import updaterPackage from 'electron-updater'
import { isDeterministicToolFailure } from './baseline-retry.js'
import { BASELINE_DECISION_MARKER, extractBaselineDecisionEnvelope } from './migration-baseline-decision.js'
const { autoUpdater } = updaterPackage
import { buildClaudeAgentArgs, buildClaudeResumeArgs, buildCodexAgentArgs, buildCodexResumeArgs, buildOpenCodeAgentArgs, buildOpenCodeResumeArgs, parseOpencodeModelsOutput } from './agent-command.js'
import { agentBatchCompletionFingerprint, agentScopeFingerprint, extractAgentSessionId, resumableAgentSessionId } from './agent-session.js'
import { restoreVerifiedAgentCompletion, updateFlowProgress, type FlowAction } from './flow-state.js'
import { adoptEmptyContinuationBranches, adoptHistoricalContinuationBranches, adoptPreferredScopeBranches, buildMigrationProgress, continuationMigrationPlan, integratedBranchTargets, leftoverConflictMarkerLines, liveGitWorktreeRecords, mergeInProgressNote, mergePackageJsonThreeWay, migrationBranchStateText, migrationCompletionIssues, migrationBatchScopeDriftIssues, migrationGroupScopeDriftIssues, migrationPlanFromPrompt, migrationScopeManifestFromPrompt, migrationStateSummary, nextIncompleteMigrationBranch, recoverContinuationScopeBranches, rebindMigrationPromptBranchIdentity, replaceMigrationPlanInPrompt, relevantGitStatus, relevantGitStatusLines, workspaceNoiseGitExcludePathspecs, rollbackIncompleteMigrationActions, satisfiedScopePackagesFromPrompt, scopeActionsFromPrompt, scopeTargetsFromPrompt, validateScopeProofEnvelope, type MigrationBranchProgress, type MigrationBranchRuntime, type MigrationBranchRuntimePhase, type MigrationPlan, type MigrationProgress } from './migration-progress.js'
import { buildGroupScopedPrompt, buildProjectPrompt } from './prompt-harness.js'
import { rebindOperationalProjectPath } from './prompt-paths.js'
import { buildAgentExecutionBatches } from './agent-batching.js'
import { assessAgentContextBudget, assertAgentContextBudget } from './agent-context-budget.js'
import { applyDependencyActionsToPackageJson, classifyDependencyMaterializationFailure, dependencyMaterializationInstallSpec } from './dependency-materialization.js'
import { createMaterializationProof, DEPENDENCY_CONTROL_KEYS, materializationProofPath, readMaterializationProof, validateMaterializationProof, writeMaterializationProof, type DependencyMaterializationProof } from './materialization-proof.js'
import { loadProvenResolvedState, resolvedStateTargetPath, restoreProvenResolvedStateLockfile, verifyProvenResolvedStateLockfile } from './resolved-state-proof.js'
import { buildGroupVerificationRepairPrompt } from './group-repair.js'
import { batchRoadmapDocument, replaceRoadmapPath } from './roadmap-dossier.js'
import { latestAgentCheckpoint } from './agent-checkpoint.js'
import { applyBranchBase, preferNewestProjectLevels, projectLevelsFromHistorySnapshots, projectLevelsFromRoadmap, type ProjectLevel } from './project-settings.js'
import { releaseBranchForAction } from './publication.js'
import { releaseGateCommands, releasePolicyForProject } from './release-policy.js'
import { buildMergeRecoveryPrompt } from './merge-recovery.js'
import { classifyFlowRecovery, type FlowRecoveryIssue } from './flow-recovery.js'
import { deterministicPlannerDecision } from './deterministic-planner.js'
import { plannerResultCacheKey, plannerResultCachePath, writePlannerResultCache } from './planner-result-cache.js'
import { buildReleaseRecoveryPrompt, readReleaseRecoveryResult } from './release-recovery.js'
import { assessMigrationCheckpoint, unexplainedFailures, verificationCommandKey, type MigrationVerificationAssessment, type VerificationEvidence } from './migration-verification.js'
import { migrationGatePolicy } from './migration-gates.js'
import { baselineVerificationCacheKey, cleanEphemeralVerificationCaches } from './verification-environment.js'
import { buildMergedRepairPrompt, readMergedRepairResult } from './merged-repair.js'
import { assessPromptRevision, buildPlannerPrompt, partitionPlannerDeferrals, readPlannerResult, residualStabilityTargets, validateSupervisorScopeAdditions, type PlannerResult } from './planner-session.js'
import { autonomyPolicy, normalizedPlannerFailure } from './autonomy-policy.js'
import { runParallelQueue, selectParallelGroupQueue } from './parallel-groups.js'
import { isToolManagedWorktreePath, portablePathKey, restartWorktreeCleanupTargets, toolManagedWorktreeFromLegacyDeferral, toolManagedWorktreePaths } from './worktree-ownership.js'
import { scheduleUpdateChecks } from './updater-schedule.js'
import { teamStatePaths } from './state-commit.js'
import { changedOverrideProjects } from './dashboard-state.js'
import { forgetScopedPromptPath, rememberScopedPromptPath, roadmapContainsProject, scopedPromptPath } from './project-context.js'
import { scopeExpansionCoverage, shouldUseSupervisorSeed, targetClosureFromRoadmap, targetClosureFromRoadmapWithTargets, targetClosureMessage, type ClosureTarget, type TargetClosure } from './target-closure.js'
import { summarizeUpdaterError } from './updater-error.js'
import { flowNotificationContent, type FlowNotificationEvent } from './notifications.js'
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { commandEnvironment, decodeProcessOutputChunk, normalizePathForComparison, packageManagerResolutionHint, processTreeDetached, resolveSpawnInvocation } from './process-launcher.js'
import { openCodeDatabaseEnv, openCodeDatabaseLocked, openCodeRuntimePaths } from './opencode-runtime.js'
import { initializeWorkspaceRepository } from './workspace-bootstrap.js'
import { createHash, randomUUID } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, unlinkSync, writeFileSync, type Dirent } from 'node:fs'
import { basename, delimiter, dirname, join, normalize, resolve, isAbsolute, relative, sep } from 'node:path'
import { createServer as createNetServer } from 'node:net'
import os from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const DEFAULT_TEMPLATE_REMOTE = ''
const DEFAULT_TOOL_REMOTE = ''
const DASHBOARD_SCHEME = 'dependency-flow-dashboard'
protocol.registerSchemesAsPrivileged([
  { scheme: DASHBOARD_SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true } },
])

type AgentProvider = 'codex' | 'opencode' | 'claude'
type ThemePreference = 'system' | 'light' | 'dark'
type BaselinePackagePolicy = 'auto' | 'keep-current' | 'required'
type BaselineSearchMode = 'AUTO' | 'BOUNDED_IMPROVEMENT' | 'EXHAUSTIVE'
type BaselineExecutionMode = 'FAST' | 'AUTOPILOT' | 'BACKGROUND'
type BaselineDeferredCohort = { id: string; label: string; packages: string[]; predicate?: string; confidence?: number; authority: 'DIAGNOSTIC_HINT'; deferredAt?: string; decisionId?: string; boundaryPackages?: string[]; warningPackages?: string[] }
type BaselineCohortAction = { kind: 'DEFER' | 'REACTIVATE'; cohortId: string; label: string; packages: string[]; predicate?: string; confidence?: number; decisionId?: string }
type BaselineIntent = { schemaVersion: 1; policies: Record<string, BaselinePackagePolicy>; extraIterations?: number; decisionGrantIterations?: number; searchMode?: BaselineSearchMode; executionMode?: BaselineExecutionMode; deferredCohorts?: BaselineDeferredCohort[]; cohortAction?: BaselineCohortAction }
type BaselineIntentCandidate = { name: string; kind: 'runtime' | 'dev' | 'peer'; requestedSpec: string; currentVersion?: string }
type BaselineIntentPlan = { candidates: BaselineIntentCandidate[]; intent: BaselineIntent }
type HardwareSnapshot = { capturedAt: string; cpu: { logicalCores: number; loadPct?: number }; memory: { totalBytes: number; freeBytes: number; usedBytes: number; usedPct: number }; process: { memoryBytes?: number; cpuPct?: number }; disks?: Array<{ name: string; filesystem?: string; freeBytes?: number; totalBytes?: number; usedPct?: number }> }
type BaselineRecoveryInfo = { available: boolean; mode?: 'yellow' | 'green'; status?: string; phase?: string; updatedAt?: string; generation?: number; iteration?: number; lastAssignment?: string; lastPredicate?: string; learnedConstraints?: number; exactExclusions?: number; reason?: string }


type GitPlan = {
  sourceBranch?: string
  baseBranch?: string
  branchPrefix?: string
  mergedBranch?: string
  releaseBranch?: string
  push?: boolean
}

type ProjectSpec = {
  name: string
  path: string
  git?: GitPlan
}

type WorkspaceRecord = {
  id: string
  name: string
  path: string
  templateRemote: string
  toolRemote: string
  teamRemote?: string
  settingsPath: string
  agent: AgentProvider
  agentModel?: string
  selectedProject?: string
  latestPromptPath?: string
  latestPromptPaths?: Record<string, string>
}

type DesktopState = {
  schemaVersion: 1
  selectedWorkspaceId?: string
  notificationsEnabled?: boolean
  themePreference?: ThemePreference
  workspaces: WorkspaceRecord[]
}

type WorkspaceDetails = {
  workspace: WorkspaceRecord
  projects: ProjectSpec[]
  settingsExists: boolean
  dashboardPath?: string
  dashboardUrl?: string
  dashboardExists: boolean
  projectPromptPath?: string
  // The exported prompt is a snapshot: "Верификация" rewriting the roadmap
  // afterward (even for an unrelated reason, e.g. picking up a new app
  // version) can reshuffle which packages are selected for *any* group, not
  // just one being deliberately re-scoped -- silently staling out a prompt
  // that was fine when it was exported. True when the dashboard was
  // regenerated after the currently tracked prompt was last saved.
  promptStale: boolean
  git: { branch: string; dirty: boolean; summary: string[] }
  teamState?: TeamFlowState
  projectLevels: Record<string, ProjectLevel>
  targetClosure?: TargetClosure
  migrationProgress?: MigrationProgress
  baselineRecovery?: BaselineRecoveryInfo
}

type AgentSessionState = { provider: AgentProvider; id: string; interrupted: boolean; updatedAt: string; scopeFingerprint?: string }

type TeamFlowState = {
  schemaVersion: 1
  updatedAt: string
  // `agentSession` (one session for the whole migration) is the shape every
  // run before v0.1.38 persisted. `agentSessions` (keyed by branch) is used
  // once the per-branch-group loop starts a project's migration. A project
  // never has both: the loop only ever engages when no legacy `agentSession`
  // is on disk for it, specifically so an in-flight legacy run is never
  // reinterpreted under the new session-lifetime model mid-migration.
  projects: Record<string, {
    lastAction: string
    status: 'running' | 'passed' | 'failed' | 'paused'
    updatedAt: string
    target?: string
    releaseBranch?: string
    releaseSourceCommit?: string
    releaseGateCommand?: string
    completedActions?: string[]
    agentSession?: AgentSessionState
    agentSessions?: Record<string, AgentSessionState>
    // v2 separates atomic dependency materialization from bounded semantic
    // LLM batches. A branch is not merge-ready merely because package.json
    // reached its targets; the orchestrator must also persist completion of
    // every semantic batch + group verification.
    executionContractVersion?: number
    completedAgentBatchFingerprints?: string[]
    completedMigrationBranches?: string[]
    activeAgentBranch?: string
    recovery?: FlowRecoveryIssue
    autonomyPlateau?: { target: string; reason: string; updatedAt: string }
    bestEffortRelease?: { target: string; current: string; reason: string; updatedAt: string; handoffPath?: string }
  }>
}

type ActionInput = {
  action: FlowAction
  workspaceId?: string
  projectName?: string
  target?: 'yellow' | 'green'
  label?: string
  promptPath?: string
  commitMessage?: string
  releaseBranch?: string
  gateCommand?: string
  resumeAgent?: boolean
  restartMigration?: boolean
  baselineResume?: 'auto' | 'continue' | 'restart'
  baselineIntent?: BaselineIntent
  agentNote?: string
  sourceCommit?: string
  autopilot?: boolean
}

type CommandSpec = {
  label: string
  command: string
  args: string[]
  cwd: string
  stdin?: string
  skipWhenNoStagedChanges?: boolean
  finalizeTeamStateBeforeRun?: boolean
  captureAgentSession?: boolean
  timeoutMs?: number
  stallWarningMs?: number
  stallAbortMs?: number
  env?: NodeJS.ProcessEnv
}

type JobRecord = {
  id: string
  action: FlowAction
  projectName?: string
  workspace: WorkspaceRecord
  target?: string
  releaseBranch?: string
  releaseSourceCommit?: string
  releaseGateCommand?: string
  child?: ChildProcessWithoutNullStreams
  cancelled: boolean
  pauseRequested?: boolean
  agentProvider?: AgentProvider
  agentSessionId?: string
  agentScopeFingerprint?: string
  // True only when the current CLI process was explicitly interrupted (Stop/app quit).
  // Ordinary autonomous retries deliberately start a fresh conversation.
  agentSessionResumable?: boolean
  // Set only by the per-branch-group loop, for the duration of that one
  // group's session -- routes captured session ids into agentSessions[branch]
  // instead of the legacy single agentSession slot.
  agentBranch?: string
  // A one-off note typed in the desktop app for this specific run, handed to
  // whichever group the loop is stuck on when the click happens -- consumed
  // once (see runMigrationAgentLoop) so it doesn't bleed into later groups
  // the same loop invocation might go on to process.
  agentNote?: string
  stdoutBuffer?: string
  openCodeServer?: ChildProcessWithoutNullStreams
  openCodeServerUrl?: string
  openCodeServerError?: string
  openCodeServerStarting?: Promise<string | undefined>
  openCodeRuntimeRoot?: string
  openCodeDbPath?: string
  openCodeDbGeneration?: number
  recoveryIssue?: FlowRecoveryIssue
  bestEffortReason?: string
  bestEffortCurrentLevel?: string
  residualExecutionPromptPath?: string
  autonomyPlateauReason?: string
  parallelParent?: JobRecord
  parallelChildren?: Set<ChildProcessWithoutNullStreams>
  parallelJobs?: Map<string, JobRecord>
  // Shared by all parallel group workers through migrationRootJob(). A base
  // branch command is probed at most once per FLOW job instead of creating a
  // fresh install/worktree for every group that happens to hit the same gate.
  baselineVerificationExitCodes?: Map<string, number>
  baselineVerificationProbe?: Promise<void>
  logSource?: { kind: 'group' | 'planner'; id: string; label: string }
  branchRuntime?: Map<string, MigrationBranchRuntime>
  // True for actions launched by the UI Autopilot. Per-stage OS notifications
  // are suppressed for these jobs because a successful agent iteration may be
  // followed immediately by another residual/replan cycle.
  autopilot?: boolean
}

type UpdateStatus = { state: 'idle' | 'checking' | 'available' | 'downloading' | 'current' | 'ready' | 'error'; version?: string; percent?: number; message?: string; authRequired?: boolean }

const jobs = new Map<string, JobRecord>()
const PROJECT_BACKGROUND_ACTIONS = new Set<FlowAction>(['preflight', 'baseline'])
const WORKSPACE_GLOBAL_ACTIONS = new Set<FlowAction>(['sync-tool', 'generate-all', 'commit-state', 'push-workspace'])

function projectRunConflicts(existing: JobRecord, workspace: WorkspaceRecord, project: ProjectSpec, action: FlowAction): boolean {
  if (existing.workspace.id !== workspace.id) return false
  if (existing.projectName === project.name) return true

  // Truly workspace-global operations still serialize against every project.
  // Baseline/preflight are project-private (Baseline writes to its private
  // baseline-project-output sink), so on a different project they may overlap
  // with another project-scoped FLOW stage. Keep two non-background mutating
  // stages serialized for now: this fixes the broad Baseline lock without
  // opening unrelated cross-project mutation races.
  if (WORKSPACE_GLOBAL_ACTIONS.has(action) || WORKSPACE_GLOBAL_ACTIONS.has(existing.action)) return true
  if (PROJECT_BACKGROUND_ACTIONS.has(action) || PROJECT_BACKGROUND_ACTIONS.has(existing.action)) return false
  return true
}

let mainWindow: BrowserWindow | null = null
let currentUpdateStatus: UpdateStatus = { state: 'idle' }
let downloadedUpdateVersion: string | undefined
let updateCheckInFlight: ReturnType<typeof autoUpdater.checkForUpdates> | undefined
let updateInstallInProgress = false
let stopUpdateChecks: (() => void) | undefined

function statePath(): string {
  return join(app.getPath('userData'), 'dependency-flow-state.json')
}


// BLOCK_VH_BASELINE_INTENT_HUMAN_LOOP_V1
function normalizeBaselineIntent(value: unknown): BaselineIntent {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const rawPolicies = raw.policies && typeof raw.policies === 'object' ? raw.policies as Record<string, unknown> : {}
  const policies: Record<string, BaselinePackagePolicy> = {}
  for (const [name, policy] of Object.entries(rawPolicies)) {
    if (policy === 'keep-current' || policy === 'required') policies[name] = policy
  }
  const deferredCohorts: BaselineDeferredCohort[] = Array.isArray(raw.deferredCohorts)
    ? raw.deferredCohorts.slice(0, 24).flatMap((value) => {
      if (!value || typeof value !== 'object') return []
      const item = value as Record<string, unknown>
      const id = String(item.id ?? '').trim().slice(0, 120)
      const packages = Array.isArray(item.packages) ? [...new Set(item.packages.map((name) => String(name).trim()).filter(Boolean))].sort().slice(0, 64) : []
      if (!id || !packages.length) return []
      return [{
        id,
        label: String(item.label ?? id).trim().slice(0, 240),
        packages,
        predicate: String(item.predicate ?? '').slice(0, 1000),
        confidence: Math.max(0, Math.min(1, Number(item.confidence ?? 0) || 0)),
        authority: 'DIAGNOSTIC_HINT' as const,
        deferredAt: String(item.deferredAt ?? '').slice(0, 80),
        decisionId: String(item.decisionId ?? '').slice(0, 120),
        boundaryPackages: Array.isArray(item.boundaryPackages) ? item.boundaryPackages.map((name) => String(name)).filter(Boolean).slice(0, 32) : [],
        warningPackages: Array.isArray(item.warningPackages) ? item.warningPackages.map((name) => String(name)).filter(Boolean).slice(0, 32) : [],
      }]
    })
    : []
  let cohortAction: BaselineCohortAction | undefined
  if (raw.cohortAction && typeof raw.cohortAction === 'object') {
    const action = raw.cohortAction as Record<string, unknown>
    const kind = action.kind === 'DEFER' || action.kind === 'REACTIVATE' ? action.kind : undefined
    if (kind) cohortAction = {
      kind,
      cohortId: String(action.cohortId ?? '').slice(0, 120),
      label: String(action.label ?? '').slice(0, 240),
      packages: Array.isArray(action.packages) ? [...new Set(action.packages.map((name) => String(name)).filter(Boolean))].sort().slice(0, 64) : [],
      predicate: String(action.predicate ?? '').slice(0, 1000),
      confidence: Math.max(0, Math.min(1, Number(action.confidence ?? 0) || 0)),
      decisionId: String(action.decisionId ?? '').slice(0, 120),
    }
  }
  return {
    schemaVersion: 1,
    policies,
    extraIterations: Math.max(0, Math.floor(Number(raw.extraIterations ?? 0) || 0)),
    decisionGrantIterations: Math.max(0, Math.floor(Number(raw.decisionGrantIterations ?? 0) || 0)),
    searchMode: raw.searchMode === 'EXHAUSTIVE' || raw.searchMode === 'BOUNDED_IMPROVEMENT' ? raw.searchMode : 'AUTO',
    executionMode: raw.executionMode === 'BACKGROUND' ? 'BACKGROUND' : 'FAST',
    deferredCohorts,
    ...(cohortAction ? { cohortAction } : {}),
  }
}

function baselineIntentPath(workspace: WorkspaceRecord, projectName: string): string {
  const safeProject = projectName.replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'project'
  return join(workspace.path, '.dependency-roadmap', 'desktop', 'baseline-intent', `${safeProject}.json`)
}

function loadBaselineIntent(workspace: WorkspaceRecord, projectName: string): BaselineIntent {
  try { return normalizeBaselineIntent(JSON.parse(readFileSync(baselineIntentPath(workspace, projectName), 'utf8'))) }
  catch { return normalizeBaselineIntent(undefined) }
}

function saveBaselineIntent(workspace: WorkspaceRecord, projectName: string, intent: BaselineIntent): void {
  const normalized = normalizeBaselineIntent(intent)
  // decisionGrantIterations is invocation-local; persisting it would silently
  // grant a new tranche after an unrelated crash/restart.
  const durable = { ...normalized }
  delete durable.cohortAction
  atomicWriteJsonSync(baselineIntentPath(workspace, projectName), { ...durable, decisionGrantIterations: 0, searchMode: 'AUTO' })
}

function projectInstalledVersion(projectPath: string, packageName: string): string | undefined {
  try {
    const manifest = JSON.parse(readFileSync(join(projectPath, 'node_modules', ...packageName.split('/'), 'package.json'), 'utf8')) as { version?: unknown }
    const version = String(manifest.version ?? '').trim()
    return version || undefined
  } catch { return undefined }
}

function baselineIntentPlan(workspace: WorkspaceRecord, project: ProjectSpec): BaselineIntentPlan {
  const manifestPath = join(project.path, 'package.json')
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as Record<string, unknown>
  const result = new Map<string, BaselineIntentCandidate>()
  const add = (section: string, kind: BaselineIntentCandidate['kind']) => {
    const values = manifest[section]
    if (!values || typeof values !== 'object') return
    for (const [name, rawSpec] of Object.entries(values as Record<string, unknown>)) {
      if (result.has(name)) continue
      result.set(name, {
        name,
        kind,
        requestedSpec: String(rawSpec ?? ''),
        currentVersion: projectInstalledVersion(project.path, name),
      })
    }
  }
  add('dependencies', 'runtime')
  add('optionalDependencies', 'runtime')
  add('devDependencies', 'dev')
  add('peerDependencies', 'peer')
  return { candidates: [...result.values()].sort((a, b) => a.name.localeCompare(b.name)), intent: loadBaselineIntent(workspace, project.name) }
}

type JsonReplacer = (this: unknown, key: string, value: unknown) => unknown

function atomicWriteJsonSync(path: string, value: unknown, replacer?: JsonReplacer): void {
  mkdirSync(dirname(path), { recursive: true })
  const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`
  try {
    const serialized = replacer
      ? JSON.stringify(value, replacer, 2)
      : JSON.stringify(value, null, 2)
    writeFileSync(temporary, `${serialized}\n`, 'utf8')
    renameSync(temporary, path)
  } finally {
    if (existsSync(temporary)) {
      try { rmSync(temporary, { force: true }) } catch { /* old destination remains intact */ }
    }
  }
}

function updateErrorStatus(error: unknown): UpdateStatus {
  const raw = error instanceof Error ? error.message : String(error)
  console.error('[updater]', raw)
  return { state: 'error', message: summarizeUpdaterError(raw, 300) }
}


function normalizeThemePreference(value: unknown): ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system' ? value : 'system'
}
function applyThemePreference(value: unknown): ThemePreference {
  const preference = normalizeThemePreference(value)
  nativeTheme.themeSource = preference
  return preference
}
function currentThemePreference(): ThemePreference { return normalizeThemePreference(loadState().themePreference) }
let previousCpuSample: { idle: number; total: number } | undefined
function systemCpuLoadPct(): number | undefined {
  const cpus = os.cpus(); let idle = 0; let total = 0
  for (const cpu of cpus) { idle += cpu.times.idle; total += Object.values(cpu.times).reduce((sum, value) => sum + value, 0) }
  const previous = previousCpuSample; previousCpuSample = { idle, total }
  if (!previous) return undefined
  const totalDelta = total - previous.total; const idleDelta = idle - previous.idle
  return totalDelta > 0 ? Math.max(0, Math.min(100, (1 - idleDelta / totalDelta) * 100)) : undefined
}
function hardwareSnapshot(): HardwareSnapshot {
  const totalBytes = os.totalmem(); const freeBytes = os.freemem(); const usedBytes = Math.max(0, totalBytes - freeBytes)
  const metrics = app.getAppMetrics(); const processMemory = metrics.reduce((sum, metric) => sum + (metric.memory?.workingSetSize ?? 0) * 1024, 0); const processCpu = metrics.reduce((sum, metric) => sum + (metric.cpu?.percentCPUUsage ?? 0), 0); const loadPct = systemCpuLoadPct()
  return { capturedAt: new Date().toISOString(), cpu: { logicalCores: os.cpus().length, ...(loadPct !== undefined ? { loadPct } : {}) }, memory: { totalBytes, freeBytes, usedBytes, usedPct: totalBytes > 0 ? usedBytes / totalBytes * 100 : 0 }, process: { ...(processMemory ? { memoryBytes: processMemory } : {}), ...(processCpu ? { cpuPct: processCpu } : {}) } }
}
function baselineRecoverySlot(projectName: string, mode: 'yellow' | 'green'): string { return createHash('sha256').update(`${projectName}\0${mode}`).digest('hex').slice(0, 24) }
function baselineRecoveryInfo(workspace: WorkspaceRecord, projectName: string | undefined): BaselineRecoveryInfo | undefined {
  if (!projectName) return undefined; const path = join(workspace.path, '.dependency-roadmap', 'state', 'baseline-run-recovery.json')
  if (!existsSync(path)) return { available: false, reason: 'checkpoint-missing' }
  try { const payload = JSON.parse(readFileSync(path, 'utf8')) as { entries?: Record<string, unknown> }; const entries = payload.entries && typeof payload.entries === 'object' ? payload.entries : {}; const candidates = (['yellow', 'green'] as const).flatMap((mode) => { const entry = entries[baselineRecoverySlot(projectName, mode)]; return entry && typeof entry === 'object' ? [{ mode, entry: entry as Record<string, unknown> }] : [] })
    if (!candidates.length) return { available: false, reason: 'checkpoint-missing' }
    const chosen = candidates.map((item) => ({ ...item, updatedAtMs: Date.parse(String(item.entry.updatedAt || '')) || 0 })).sort((left, right) => right.updatedAtMs - left.updatedAtMs)[0]; const entry = chosen.entry; const state = entry.state && typeof entry.state === 'object' ? entry.state as Record<string, unknown> : {}; const iteration = Number(state.iteration); const generation = Number(entry.generation); const learned = Array.isArray(state.learnedConstraints) ? state.learnedConstraints.length : undefined; const exact = Array.isArray(state.globalExactExclusions) ? state.globalExactExclusions.length : undefined
    const status = typeof entry.status === 'string' ? entry.status : undefined
    // Python RecoveryStore deliberately marks completed/passed checkpoints as
    // non-resumable (reason=already-complete). Desktop previously treated the
    // mere presence of such an entry as "Continue available", which could turn
    // a post-processing failure after a successful Baseline into a bogus
    // BASELINE_RECOVERY_CONTINUE_UNAVAILABLE loop.
    const resumable = status !== 'completed' && status !== 'passed'
    return { available: resumable, mode: chosen.mode, status, phase: typeof entry.phase === 'string' ? entry.phase : undefined, updatedAt: typeof entry.updatedAt === 'string' ? entry.updatedAt : undefined, ...(Number.isFinite(generation) ? { generation } : {}), ...(Number.isFinite(iteration) ? { iteration } : {}), lastAssignment: typeof state.lastAssignment === 'string' ? state.lastAssignment : undefined, lastPredicate: typeof state.lastPredicate === 'string' ? state.lastPredicate : undefined, ...(learned !== undefined ? { learnedConstraints: learned } : {}), ...(exact !== undefined ? { exactExclusions: exact } : {}), ...(!resumable ? { reason: 'already-complete' } : {}) }
  } catch (error) { return { available: false, reason: error instanceof Error ? error.message : String(error) } }
}
function emptyState(): DesktopState {
  return { schemaVersion: 1, notificationsEnabled: true, themePreference: 'system', workspaces: [] }
}

function loadState(): DesktopState {
  const path = statePath()
  if (!existsSync(path)) return emptyState()
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as DesktopState
    return parsed.schemaVersion === 1 && Array.isArray(parsed.workspaces) ? parsed : emptyState()
  } catch {
    return emptyState()
  }
}

function saveState(state: DesktopState): void {
  atomicWriteJsonSync(statePath(), state)
}

function notificationsEnabled(state = loadState()): boolean {
  return state.notificationsEnabled !== false
}

function showFlowNotification(event: FlowNotificationEvent): void {
  if (!notificationsEnabled() || !Notification.isSupported()) return
  const content = flowNotificationContent(event)
  new Notification(content).show()
}
function selectedWorkspace(state = loadState()): WorkspaceRecord | undefined {
  return state.workspaces.find((item) => item.id === state.selectedWorkspaceId) ?? state.workspaces[0]
}

function resolveSettingsPath(workspace: WorkspaceRecord): string {
  return resolve(workspace.path, workspace.settingsPath)
}

// BLOCK_W_P0_P1_TYPES_NESTED_FIX_V1
const PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH = 5
const PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS = new Set([
  '.git', 'node_modules', '.dependency-roadmap', '.dependency-update-history',
  '.next', 'dist', 'build', 'coverage', '.turbo', '.cache',
])

function discoverProjectPackageDirectories(root: string): string[] {
  const found: string[] = []
  const walk = (directory: string, depth: number): void => {
    if (depth > PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH) return
    if (directory !== root && existsSync(join(directory, 'package.json'))) {
      found.push(resolve(directory))
      return
    }
    let entries: Dirent[]
    try {
      entries = readdirSync(directory, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.isSymbolicLink() || PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS.has(entry.name)) continue
      walk(join(directory, entry.name), depth + 1)
    }
  }
  walk(resolve(root), 0)
  return [...new Set(found)].sort((left, right) => left.localeCompare(right))
}

function resolveProjectPackageDirectory(inputPath: string, strict = false): string {
  const selected = resolve(inputPath)
  if (!existsSync(selected) || !statSync(selected).isDirectory()) {
    if (strict) throw new Error(`Папка проекта не найдена: ${selected}`)
    return selected
  }
  if (existsSync(join(selected, 'package.json'))) return selected
  const candidates = discoverProjectPackageDirectories(selected)
  if (candidates.length === 1) return candidates[0]
  if (!strict) return selected
  if (!candidates.length) {
    throw new Error(`В выбранной папке ${selected} package.json не найден. Выберите Git-репозиторий с одним npm-проектом или папку конкретного npm-проекта.`)
  }
  const preview = candidates.slice(0, 8).map((candidate) => relative(selected, candidate) || '.').join(', ')
  throw new Error(`Найдено несколько package.json (${preview}${candidates.length > 8 ? `, ... (+${candidates.length - 8})` : ''}). Выберите папку конкретного npm-проекта, чтобы DepLoom не угадывал workspace.`)
}

async function resolveProjectGitLayout(project: ProjectSpec): Promise<{ gitRoot: string; packageRelativePath: string }> {
  const packageRoot = resolve(project.path)
  const probe = await spawnCapture('git', ['-C', packageRoot, 'rev-parse', '--show-toplevel'], packageRoot, 15_000)
  if (probe.timedOut || probe.code !== 0) {
    throw new Error(probe.stderr.trim() || `Не удалось определить Git root для ${packageRoot}`)
  }
  const gitRoot = resolve(probe.stdout.trim())
  const relativePath = relative(gitRoot, packageRoot)
  if (relativePath.startsWith('..') || isAbsolute(relativePath)) {
    throw new Error(`PROJECT_PACKAGE_OUTSIDE_GIT_ROOT: package=${packageRoot}, gitRoot=${gitRoot}`)
  }
  return { gitRoot, packageRelativePath: relativePath === '.' ? '' : relativePath.split(sep).join('/') }
}

function projectPathInWorktree(worktreeRoot: string, packageRelativePath: string): string {
  return packageRelativePath ? join(worktreeRoot, ...packageRelativePath.split('/')) : worktreeRoot
}

function projectTreePath(packageRelativePath: string, fileName: string): string {
  return packageRelativePath ? `${packageRelativePath}/${fileName}` : fileName
}

function readProjects(workspace: WorkspaceRecord): ProjectSpec[] {
  const settingsPath = resolveSettingsPath(workspace)
  if (!existsSync(settingsPath)) return []
  try {
    const settings = JSON.parse(readFileSync(settingsPath, 'utf8')) as { root?: string; projects?: ProjectSpec[] }
    if (!Array.isArray(settings.projects)) return []
    const configuredRoot = typeof settings.root === 'string' && settings.root.trim()
      ? resolve(settings.root)
      : workspace.path
    return settings.projects.map((project) => {
      const absolute = isAbsolute(project.path) ? resolve(project.path) : resolve(configuredRoot, project.path)
      return { ...project, path: resolveProjectPackageDirectory(absolute) }
    })
  } catch {
    return []
  }
}

function promptPathForProject(workspace: WorkspaceRecord, projectName: string): string | undefined {
  const scoped = scopedPromptPath(workspace, projectName)
  if (scoped && existsSync(scoped)) return scoped
  // One-time compatibility bridge for state written before prompts were
  // project-scoped. Never trust the legacy scalar merely because it exists:
  // prove that the prompt itself contains this project's Branch plan.
  const legacy = workspace.latestPromptPath
  if (!legacy || !existsSync(legacy)) return undefined
  try {
    return migrationPlanFromPrompt(readFileSync(legacy, 'utf8'), projectName) ? legacy : undefined
  } catch {
    return undefined
  }
}

function roadmapFileContainsProject(roadmapPath: string, projectName: string): boolean {
  if (!existsSync(roadmapPath)) return false
  try {
    return roadmapContainsProject(JSON.parse(readFileSync(roadmapPath, 'utf8')), projectName)
  } catch {
    return false
  }
}

// `--only-project` intentionally rewrites the configured roadmap/dashboard outputs.
// Without a per-project snapshot, generating project B therefore invalidates the
// prompt/readiness/closure view of project A even though A itself did not change.
// Keep these UI/orchestration snapshots outside the workspace Git tree: they are
// runtime cache, not team state, and must never make the repository dirty.
function projectArtifactCacheDir(workspace: WorkspaceRecord, projectName: string): string {
  const projectKey = Buffer.from(projectName, 'utf8').toString('base64url')
  return join(app.getPath('userData'), 'project-artifacts', workspace.id, projectKey)
}

function projectArtifactCachePath(workspace: WorkspaceRecord, projectName: string, kind: 'json' | 'html'): string {
  return join(projectArtifactCacheDir(workspace, projectName), kind === 'json' ? 'dependency-roadmap.json' : 'local-dependency-roadmap.html')
}

function baselineProjectOutputDir(workspace: WorkspaceRecord, projectName: string): string {
  const stem = projectName.replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'project'
  return join(workspace.path, '.dependency-roadmap', 'desktop', 'baseline-project-output', stem)
}

function snapshotProjectArtifacts(
  workspace: WorkspaceRecord,
  projectName: string,
  source?: { roadmap: string; dashboard?: string },
): boolean {
  const sourceRoadmap = source?.roadmap ?? artifactPath(workspace, 'jsonOut', '.dependency-roadmap/artifacts/dependency-roadmap.json')
  if (!roadmapFileContainsProject(sourceRoadmap, projectName)) return false
  const directory = projectArtifactCacheDir(workspace, projectName)
  mkdirSync(directory, { recursive: true })
  copyFileSync(sourceRoadmap, projectArtifactCachePath(workspace, projectName, 'json'))
  const sourceDashboard = source?.dashboard ?? artifactPath(workspace, 'htmlOut', '.dependency-roadmap/artifacts/local-dependency-roadmap.html')
  if (existsSync(sourceDashboard)) copyFileSync(sourceDashboard, projectArtifactCachePath(workspace, projectName, 'html'))
  return true
}

function projectRoadmapPath(workspace: WorkspaceRecord, projectName: string): string | undefined {
  const cached = projectArtifactCachePath(workspace, projectName, 'json')
  if (roadmapFileContainsProject(cached, projectName)) return cached
  const configured = artifactPath(workspace, 'jsonOut', '.dependency-roadmap/artifacts/dependency-roadmap.json')
  return roadmapFileContainsProject(configured, projectName) ? configured : undefined
}

function projectDashboardPath(workspace: WorkspaceRecord, projectName: string): string | undefined {
  const cachedRoadmap = projectArtifactCachePath(workspace, projectName, 'json')
  const cachedDashboard = projectArtifactCachePath(workspace, projectName, 'html')
  if (roadmapFileContainsProject(cachedRoadmap, projectName) && existsSync(cachedDashboard)) return cachedDashboard
  const configuredRoadmap = artifactPath(workspace, 'jsonOut', '.dependency-roadmap/artifacts/dependency-roadmap.json')
  const configuredDashboard = artifactPath(workspace, 'htmlOut', '.dependency-roadmap/artifacts/local-dependency-roadmap.html')
  return roadmapFileContainsProject(configuredRoadmap, projectName) && existsSync(configuredDashboard) ? configuredDashboard : undefined
}

function dashboardHasProject(workspace: WorkspaceRecord, projectName: string): boolean {
  return Boolean(projectDashboardPath(workspace, projectName))
}

function readSettings(workspace: WorkspaceRecord): Record<string, unknown> {
  const settingsPath = resolveSettingsPath(workspace)
  if (!existsSync(settingsPath)) return {}
  try {
    return JSON.parse(readFileSync(settingsPath, 'utf8')) as Record<string, unknown>
  } catch {
    return {}
  }
}

function artifactPath(workspace: WorkspaceRecord, key: string, fallback: string): string {
  const settings = readSettings(workspace)
  const configured = typeof settings[key] === 'string' ? String(settings[key]) : fallback
  return resolve(workspace.path, configured)
}

function teamStatePath(workspace: WorkspaceRecord): string {
  return join(workspace.path, '.dependency-roadmap', 'desktop', 'flow-state.json')
}

function readTeamState(workspace: WorkspaceRecord): TeamFlowState | undefined {
  const path = teamStatePath(workspace)
  if (!existsSync(path)) return undefined
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as TeamFlowState
  } catch {
    return undefined
  }
}

function readProjectLevels(workspace: WorkspaceRecord): Record<string, ProjectLevel> {
  const roadmapPath = artifactPath(workspace, 'jsonOut', '.dependency-roadmap/artifacts/dependency-roadmap.json')
  let current: Record<string, ProjectLevel> = {}
  if (existsSync(roadmapPath)) {
    try { current = projectLevelsFromRoadmap(JSON.parse(readFileSync(roadmapPath, 'utf8')), statSync(roadmapPath).mtime.toISOString()) } catch { /* use history below */ }
  }
  const snapshotsDir = join(artifactPath(workspace, 'historyDir', '.dependency-roadmap/history'), 'snapshots')
  if (!existsSync(snapshotsDir)) return current
  const snapshots = readdirSync(snapshotsDir)
    .filter((name) => name.endsWith('.json')).sort().reverse().slice(0, 100)
    .flatMap((name) => {
      try { return [JSON.parse(readFileSync(join(snapshotsDir, name), 'utf8')) as unknown] } catch { return [] }
    })
  return preferNewestProjectLevels(projectLevelsFromHistorySnapshots(snapshots), current)
}

function writeBestEffortHandoff(job: JobRecord): string | undefined {
  if (job.action !== 'release' || !job.projectName || !job.bestEffortReason) return undefined
  try {
    const roadmapPath = projectRoadmapPath(job.workspace, job.projectName)
    if (!roadmapPath || !existsSync(roadmapPath)) return undefined
    const closure = targetClosureFromRoadmap(JSON.parse(readFileSync(roadmapPath, 'utf8')), job.projectName, job.target === 'green' ? 'green' : 'yellow')
    const directory = join(job.workspace.path, '.dependency-roadmap', 'desktop', 'handoff')
    mkdirSync(directory, { recursive: true })
    const path = join(directory, `${job.projectName.replace(/[^a-zA-Z0-9._-]+/g, '-')}-${job.target === 'green' ? 'green' : 'yellow'}-best-effort.md`)
    const blockerLines = closure.lagBlockers.length
      ? closure.lagBlockers.map((blocker) => `- \`${blocker.package}\`${blocker.current ? ` ${blocker.current}` : ''}${blocker.required ? ` → требуется ${blocker.required}` : ''}${blocker.note ? ` — ${blocker.note}` : ''}`).join('\n')
      : '- Roadmap не содержит детализированных lag blockers; пересоберите verification перед следующей итерацией.'
    const markdown = `# DepLoom — best-effort handoff\n\n` +
      `Проект: **${job.projectName}**\n\n` +
      `Цель: **${job.target === 'green' ? 'Green' : 'Yellow'}**, достигнутый уровень: **${closure.current}**${typeof closure.lagOkPct === 'number' ? ` (${closure.lagOkPct.toFixed(1)}%)` : ''}.\n\n` +
      `Release-ветка: \`${job.releaseBranch || 'не зафиксирована'}\`.\n\n` +
      `## Что уже гарантировано\n\n` +
      `- весь исполнимый migration plan исчерпан;\n` +
      `- в merged попали только прошедшие verifier/интеграционные проверки изменения;\n` +
      `- release создан только после обычных project gates и repository hooks;\n` +
      `- health blocker не скрыт исключением и остаётся видимым.\n\n` +
      `## Почему FLOW остановился на best-effort\n\n${job.bestEffortReason}\n\n` +
      `## Оставшиеся blockers\n\n${blockerLines}\n\n` +
      `## Следующему агенту\n\n` +
      `Не повторяй уже зелёные группы. Начни с этого handoff и свежего roadmap, проверь ограничения peer/dependency closure и предложи минимальное расширение scope для оставшихся blockers. Не ослабляй final gates/hooks и не меняй уже подтверждённые targets без нового plan/replan.\n`
    writeFileSync(path, markdown, 'utf8')
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Best-effort handoff сохранён: ${path}` })
    return path
  } catch (error) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Не удалось записать best-effort handoff (release остаётся валидным): ${error instanceof Error ? error.message : String(error)}` })
    return undefined
  }
}

function updateTeamState(job: JobRecord, status: 'running' | 'passed' | 'failed' | 'paused'): void {
  if (!job.projectName) return
  const path = teamStatePath(job.workspace)
  const current = readTeamState(job.workspace) ?? { schemaVersion: 1, updatedAt: new Date().toISOString(), projects: {} }
  current.updatedAt = new Date().toISOString()
  const previous = current.projects[job.projectName]
  const progress = updateFlowProgress(previous, job.action, status, job.target)
  const freshBaseline = job.action === 'baseline' && status === 'passed'
  // `job.agentBranch` is always set before a session id can be captured (see
  // runGroupAgentSession), so every write from here on goes into the
  // per-branch map; `agentSession` (legacy) is only ever read back from
  // whatever a pre-v0.1.39 install already wrote, never written again.
  const capturedSession = ['agent', 'recover'].includes(job.action) && job.agentProvider && job.agentSessionId && job.agentBranch
    ? {
        provider: job.agentProvider,
        id: job.agentSessionId,
        interrupted: status === 'failed' && job.agentSessionResumable === true,
        updatedAt: current.updatedAt,
        ...(job.agentScopeFingerprint ? { scopeFingerprint: job.agentScopeFingerprint } : {}),
      }
    : undefined
  const agentSessions = freshBaseline ? undefined : capturedSession
    ? { ...previous?.agentSessions, [job.agentBranch as string]: capturedSession }
    : previous?.agentSessions
  const clearRecovery = freshBaseline || (status === 'passed' && (job.action === 'recover' || ['agent', 'release'].includes(job.action) || previous?.recovery?.action === job.action))
  const bestEffortHandoffPath = status === 'passed' ? writeBestEffortHandoff(job) : undefined
  const autonomyPlateau = status === 'passed' && ['agent', 'recover'].includes(job.action) && job.autonomyPlateauReason
    ? { target: job.target || previous?.target || 'yellow', reason: job.autonomyPlateauReason, updatedAt: current.updatedAt }
    : job.action !== 'baseline' ? previous?.autonomyPlateau : undefined
  current.projects[job.projectName] = {
    ...progress,
    updatedAt: current.updatedAt,
    ...(freshBaseline
      ? { executionContractVersion: 2, completedAgentBatchFingerprints: [], completedMigrationBranches: [] }
      : previous?.executionContractVersion
        ? {
            executionContractVersion: previous.executionContractVersion,
            ...(previous.completedAgentBatchFingerprints ? { completedAgentBatchFingerprints: previous.completedAgentBatchFingerprints } : {}),
            ...(previous.completedMigrationBranches ? { completedMigrationBranches: previous.completedMigrationBranches } : {}),
          }
        : {}),
    ...(job.releaseBranch || previous?.releaseBranch ? { releaseBranch: job.releaseBranch ?? previous?.releaseBranch } : {}),
    ...(job.releaseSourceCommit || previous?.releaseSourceCommit ? { releaseSourceCommit: job.releaseSourceCommit ?? previous?.releaseSourceCommit } : {}),
    ...(job.releaseGateCommand || previous?.releaseGateCommand ? { releaseGateCommand: job.releaseGateCommand ?? previous?.releaseGateCommand } : {}),
    ...(!freshBaseline && previous?.agentSession ? { agentSession: previous.agentSession } : {}),
    ...(agentSessions ? { agentSessions } : {}),
    ...(!freshBaseline ? (job.agentBranch ? { activeAgentBranch: job.agentBranch } : previous?.activeAgentBranch ? { activeAgentBranch: previous.activeAgentBranch } : {}) : {}),
    ...(!clearRecovery && previous?.recovery ? { recovery: previous.recovery } : {}),
    ...(autonomyPlateau ? { autonomyPlateau } : {}),
    ...(job.action === 'release' && status === 'passed' && job.bestEffortReason
      ? { bestEffortRelease: { target: job.target || 'yellow', current: job.bestEffortCurrentLevel || 'unknown', reason: job.bestEffortReason, updatedAt: current.updatedAt, ...(bestEffortHandoffPath ? { handoffPath: bestEffortHandoffPath } : {}) } }
      : job.action !== 'baseline' && previous?.bestEffortRelease ? { bestEffortRelease: previous.bestEffortRelease } : {}),
  }
  atomicWriteJsonSync(path, current)
}

async function persistRecoveryIssue(job: JobRecord, message: string): Promise<FlowRecoveryIssue | undefined> {
  if (!job.projectName || job.cancelled) return undefined
  const project = findProject(job.workspace, job.projectName)
  const branch = await spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000)
  const classified = classifyFlowRecovery(message, job.action)
  const issue: FlowRecoveryIssue = {
    ...classified,
    action: job.action,
    message,
    ...(branch.code === 0 && branch.stdout.trim() ? { branch: branch.stdout.trim() } : {}),
    updatedAt: new Date().toISOString(),
  }
  const state = readTeamState(job.workspace)
  const run = state?.projects[job.projectName]
  if (!state || !run) return issue
  state.updatedAt = issue.updatedAt
  state.projects[job.projectName] = { ...run, recovery: issue }
  const path = teamStatePath(job.workspace)
  atomicWriteJsonSync(path, state)
  return issue
}

async function inferredRecoveryIssue(workspace: WorkspaceRecord, project: ProjectSpec): Promise<FlowRecoveryIssue | undefined> {
  const stored = readTeamState(workspace)?.projects[project.name]?.recovery
  if (stored) return stored
  const run = readTeamState(workspace)?.projects[project.name]
  const releaseBranch = run?.releaseBranch || project.git?.releaseBranch
  if (!releaseBranch) return undefined
  const [branch, status] = await Promise.all([
    spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000),
    spawnCapture('git', ['-C', project.path, 'status', '--porcelain=v1', '--untracked-files=all'], project.path, 15_000),
  ])
  if (branch.code === 0 && status.code === 0 && branch.stdout.trim() === releaseBranch && relevantGitStatus(status.stdout)) {
    const progress = await readMigrationProgress(workspace, project)
    if (!progress || !progress.trustworthy || !migrationIsComplete(progress)) return undefined
    return {
      code: 'MIGRATION_DONE_WORKTREE_NOT_CLEAN',
      kind: 'agent',
      action: 'release',
      message: `Подготовленная release-ветка ${releaseBranch} содержит незакоммиченные изменения после завершённой миграции.`,
      branch: releaseBranch,
      updatedAt: new Date().toISOString(),
    }
  }
  return undefined
}

function captureAgentSession(job: JobRecord, chunk: string): void {
  if (!job.agentProvider || job.agentSessionId) return
  const combined = `${job.stdoutBuffer ?? ''}${chunk}`
  const lines = combined.split(/\r?\n/)
  job.stdoutBuffer = lines.pop() ?? ''
  if (job.stdoutBuffer.trim()) lines.push(job.stdoutBuffer)
  for (const line of lines) {
    const sessionId = extractAgentSessionId(line, job.agentProvider)
    if (!sessionId) continue
    job.agentSessionId = sessionId
    job.stdoutBuffer = ''
    updateTeamState(job, 'running')
    return
  }
}

// Every command is spawned as its own POSIX process group (or as a normal
// Windows process tree). A timeout/cancel must terminate npm/node/test descendants
// too; otherwise a hidden child can hold pipes, worktrees or package-manager locks.
function killProcessTree(child: ChildProcessWithoutNullStreams): void {
  const pid = child.pid
  if (!pid) return
  if (process.platform === 'win32') {
    const killer = spawn('taskkill', ['/pid', String(pid), '/t', '/f'], { windowsHide: true })
    killer.on('error', () => {})
    return
  }
  try { process.kill(-pid, 'SIGTERM') } catch { try { child.kill('SIGTERM') } catch { /* already gone */ } }
  const hardKill = setTimeout(() => {
    try { process.kill(-pid, 'SIGKILL') } catch { try { child.kill('SIGKILL') } catch { /* already gone */ } }
  }, 3_000)
  hardKill.unref()
}

// A killed process just looks like a non-zero exit to the caller, which for a
// query like `merge-base --is-ancestor` silently reads as a confident "no".
// `timedOut` lets callers tell "the answer is no" from "we never got one" —
// the difference between reporting real progress and erasing it because the
// machine was busy.
type CaptureResult = { code: number; stdout: string; stderr: string; timedOut: boolean }

function spawnCapture(command: string, args: string[], cwd: string, timeoutMs = 8_000, envOverrides?: NodeJS.ProcessEnv): Promise<CaptureResult> {
  return new Promise((resolvePromise) => {
    let stdout = ''
    let stderr = ''
    let settled = false
    let timedOut = false
    const commandEnv = commandEnvironment(envOverrides ? { ...process.env, ...envOverrides } : process.env)
    const invocation = resolveSpawnInvocation(command, args, { env: commandEnv })
    const child = spawn(invocation.command, invocation.args, { cwd, shell: false, detached: processTreeDetached(), windowsHide: true, windowsVerbatimArguments: invocation.windowsVerbatimArguments, env: commandEnv })
    const timer = setTimeout(() => { timedOut = true; killProcessTree(child) }, timeoutMs)
    child.stdout.on('data', (chunk: Buffer) => { stdout += decodeProcessOutputChunk(chunk) })
    child.stderr.on('data', (chunk: Buffer) => { stderr += decodeProcessOutputChunk(chunk) })
    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolvePromise({ code: 1, stdout, stderr: `${stderr}${error.message}`, timedOut })
    })
    child.on('close', (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolvePromise({ code: code ?? 1, stdout, stderr, timedOut })
    })
  })
}

async function gitOverview(workspacePath: string): Promise<WorkspaceDetails['git']> {
  const [branchResult, statusResult] = await Promise.all([
    spawnCapture('git', ['-C', workspacePath, 'branch', '--show-current'], workspacePath),
    spawnCapture('git', ['-C', workspacePath, 'status', '--short'], workspacePath),
  ])
  const summary = relevantGitStatusLines(statusResult.stdout)
  return {
    branch: branchResult.stdout.trim() || 'detached',
    dirty: summary.length > 0,
    summary: summary.slice(0, 30),
  }
}

// Progress queries run against a repository the agent is actively building in,
// so they compete with its installs and test runs for disk. The old 8s default
// made a busy machine look like lost work; these reads are cheap but must be
// allowed to wait.
const GIT_QUERY_TIMEOUT_MS = 60_000
const AGENT_EXECUTION_BATCH_MAX_PACKAGES = 6
const MAX_AUTONOMOUS_STAGE_RECOVERY_CYCLES = 8
const PLANNER_ATTEMPT_TIMEOUT_MS = 6 * 60_000

async function readMigrationProgress(workspace: WorkspaceRecord, project: ProjectSpec): Promise<MigrationProgress | undefined> {
  const promptPath = promptPathForProject(workspace, project.name)
  if (!promptPath || !existsSync(promptPath)) return undefined
  const promptMarkdown = readFileSync(promptPath, 'utf8')
  const plan = migrationPlanFromPrompt(promptMarkdown, project.name)
  if (!plan) return undefined
  const git = (args: string[]) => spawnCapture('git', ['-C', project.path, ...args], project.path, GIT_QUERY_TIMEOUT_MS)
  const [refsResult, branchResult, statusResult, worktreeResult] = await Promise.all([
    git(['for-each-ref', '--format=%(refname:short)', 'refs/heads', 'refs/remotes/origin']),
    git(['branch', '--show-current']),
    git(['status', '--porcelain']),
    git(['worktree', 'list', '--porcelain']),
  ])
  if (refsResult.code !== 0) return undefined
  let trustworthy = ![refsResult, branchResult, statusResult, worktreeResult].some((result) => result.timedOut || result.code !== 0)
  const worktreePaths = new Map<string, string>()
  if (worktreeResult.code === 0) {
    for (const record of liveGitWorktreeRecords(worktreeResult.stdout)) {
      if (record.branch && existsSync(record.path)) worktreePaths.set(record.branch, record.path)
    }
  }
  const refs = refsResult.stdout.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
  const refSet = new Set(refs)
  const selectRef = (branch: string) => refSet.has(branch) ? branch : refSet.has(`origin/${branch}`) ? `origin/${branch}` : undefined
  const localName = (ref: string) => ref.startsWith('origin/') ? ref.slice('origin/'.length) : ref
  const mergedRef = selectRef(plan.mergedBranch)
  const emptyBranchResult = mergedRef
    ? await git(['for-each-ref', '--points-at=' + mergedRef, '--format=%(refname:short)', 'refs/heads', 'refs/remotes/origin'])
    : undefined
  if (emptyBranchResult && (emptyBranchResult.timedOut || emptyBranchResult.code !== 0)) trustworthy = false
  const emptyBranches = emptyBranchResult?.code === 0
    ? emptyBranchResult.stdout.split(/\r?\n/).map((item) => localName(item.trim())).filter(Boolean)
    : []
  const plannedRefs = new Map(plan.branches.flatMap(({ branch }) => {
    const ref = selectRef(branch)
    return ref ? [[branch, ref] as const] : []
  }))
  // One `--contains` query per work branch answers both "is it in merged" and
  // "did it land somewhere else instead", replacing the previous N x N grid of
  // `merge-base` processes that was re-spawned on every refresh.
  const containment = await Promise.all([...plannedRefs].map(async ([branch, branchRef]) => {
    const result = await git(['for-each-ref', `--contains=${branchRef}`, '--format=%(refname:short)', 'refs/heads', 'refs/remotes/origin'])
    if (result.timedOut || result.code !== 0) trustworthy = false
    const containers = new Set(result.stdout.split(/\r?\n/).map((item) => localName(item.trim())).filter(Boolean))
    return [branch, containers] as const
  }))
  const mergedBranches = containment.filter(([, containers]) => containers.has(plan.mergedBranch)).map(([branch]) => branch)
  const mergedSet = new Set(mergedBranches)
  const integratedBranches = integratedBranchTargets(plan, mergedSet, containment)
  const dirtyChanges = relevantGitStatusLines(statusResult.stdout).length
  const { packageRelativePath } = await resolveProjectGitLayout(project)
  const packageJsonTreePath = projectTreePath(packageRelativePath, 'package.json')
  const packageJsonAtRef = async (ref: string): Promise<unknown | undefined> => {
    const result = await git(['show', `${ref}:${packageJsonTreePath}`])
    if (result.timedOut || result.code !== 0) return undefined
    try {
      return JSON.parse(result.stdout) as unknown
    } catch {
      return undefined
    }
  }
  const scopeAt = (packageJson: unknown | undefined) => packageJson === undefined
    ? undefined
    : satisfiedScopePackagesFromPrompt(promptMarkdown, project.name, packageJson)
  // The gate asks whether the *merged* branch reached the agreed targets. Only
  // when that branch does not exist yet is the working tree a meaningful
  // stand-in, and then the answer is labelled as such.
  const mergedPackageJson = mergedRef ? await packageJsonAtRef(mergedRef) : undefined
  if (mergedRef && mergedPackageJson === undefined) trustworthy = false
  let satisfiedPackages = scopeAt(mergedPackageJson)
  let factsRef = mergedRef ? plan.mergedBranch : ''
  if (!mergedRef) {
    factsRef = `рабочем дереве (${branchResult.stdout.trim() || 'HEAD'})`
    try {
      satisfiedPackages = satisfiedScopePackagesFromPrompt(promptMarkdown, project.name, JSON.parse(readFileSync(join(project.path, 'package.json'), 'utf8')))
    } catch { /* the canonical validator reports malformed project/scope data */ }
  }
  const branchScopes = await Promise.all([...plannedRefs].map(async ([branch, branchRef]) => {
    const satisfied = scopeAt(await packageJsonAtRef(branchRef))
    return satisfied ? [[branch, satisfied] as const] : []
  }))
  // Branch refs intentionally remain the completion gate, but an interrupted
  // parallel worker can have valuable uncommitted progress in its own
  // worktree. Read that live package.json for display and resume decisions so
  // the UI never calls real work an untouched branch after an app restart.
  const worktreeFacts = await Promise.all(plan.branches.map(async (plannedBranch) => {
    const candidates = await Promise.all([...new Set([plannedBranch.branch, plannedBranch.scopeBranch].filter((value): value is string => Boolean(value)))].map(async (candidate) => {
      const worktreePath = worktreePaths.get(candidate)
      if (!worktreePath) return undefined
      const candidateProjectPath = projectPathInWorktree(worktreePath, packageRelativePath)
      if (portablePathKey(candidateProjectPath) === portablePathKey(project.path)) return undefined
      let satisfied: ReadonlySet<string> | undefined
      try {
        satisfied = scopeAt(JSON.parse(readFileSync(join(candidateProjectPath, 'package.json'), 'utf8')))
      } catch {
        trustworthy = false
      }
      const status = await spawnCapture('git', ['-C', candidateProjectPath, 'status', '--porcelain=v1', '--untracked-files=all'], candidateProjectPath, 15_000)
      if (status.timedOut || status.code !== 0) trustworthy = false
      const dirtyChanges = status.code === 0 ? relevantGitStatusLines(status.stdout).length : 0
      const metPackages = satisfied ? plannedBranch.packages.filter((name) => satisfied.has(name)).length : 0
      return { candidate, worktreePath, satisfied, dirtyChanges, metPackages }
    }))
    const best = candidates.filter((item): item is NonNullable<typeof item> => Boolean(item))
      .sort((left, right) => right.metPackages - left.metPackages || right.dirtyChanges - left.dirtyChanges)[0]
    return best ? { branch: plannedBranch.branch, ...best } : undefined
  }))
  const liveWorktrees = worktreeFacts.filter((item): item is NonNullable<typeof item> => Boolean(item))
  const factsCommitResult = await git(['rev-parse', mergedRef ?? 'HEAD'])
  const factsCommit = factsCommitResult.code === 0 ? factsCommitResult.stdout.trim() : undefined
  const runState = readTeamState(workspace)?.projects[project.name]
  const executionCompletedBranches = runState?.executionContractVersion === 2
    ? new Set(runState.completedMigrationBranches ?? [])
    : undefined
  return buildMigrationProgress({
    plan,
    refs,
    currentBranch: branchResult.stdout.trim(),
    mergedBranches,
    dirtyChanges,
    integratedBranches,
    satisfiedPackages,
    branchSatisfiedPackages: Object.fromEntries(branchScopes.flat()),
    branchWorktreeSatisfiedPackages: Object.fromEntries(liveWorktrees.flatMap((item) => item.satisfied ? [[item.branch, item.satisfied] as const] : [])),
    branchWorktreeDirtyChanges: Object.fromEntries(liveWorktrees.map((item) => [item.branch, item.dirtyChanges])),
    branchWorktreePaths: Object.fromEntries(liveWorktrees.map((item) => [item.branch, item.worktreePath])),
    runtimeByBranch: migrationRuntime(workspace, project.name),
    ...(executionCompletedBranches ? { executionCompletedBranches } : {}),
    emptyBranches,
    factsRef,
    ...(factsCommit ? { factsCommit } : {}),
    trustworthy,
  })
}

async function mergeInProgress(projectPath: string): Promise<boolean> {
  const result = await spawnCapture('git', ['-C', projectPath, 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], projectPath, 15_000)
  return result.code === 0
}

function migrationIsComplete(progress: MigrationProgress): boolean {
  return migrationCompletionIssues(progress).length === 0
}

// Reading workspace details must not rewrite the run's recorded progress. This
// used to fire on every refresh -- including the 5s poll during an active job
// -- so a single slow git query could permanently roll a finished stage back
// and mark the agent session interrupted, which is what made stage markers
// jump backwards mid-run. Reconciliation now runs only on a trustworthy reading
// while nothing is active: it may roll an invalidated migration back, or restore
// the agent stage forward when Git proves an older Desktop falsely failed it on
// a dirty release branch.
function reconcileTeamState(workspace: WorkspaceRecord, project: ProjectSpec | undefined, progress: MigrationProgress | undefined): TeamFlowState | undefined {
  const state = readTeamState(workspace)
  if (!state || !project || !progress || !progress.trustworthy) return state
  if ([...jobs.values()].some((job) => job.workspace.id === workspace.id)) return state
  let run = state.projects[project.name]
  const completed = new Set(run?.completedActions ?? [])
  if (!run) return state

  // Recovery severity is policy, not historical truth. Older versions stored
  // MIGRATION_PLAN_APPROVAL_REQUIRED/GROUP_SCOPE_* as hard stops; keeping that
  // stale `kind` after an app upgrade would preserve the red banner forever
  // even though the new Supervisor can handle it autonomously. Reclassify the
  // persisted issue against the current policy before deciding FLOW state.
  if (run.recovery) {
    const classified = classifyFlowRecovery(run.recovery.message, run.recovery.action)
    if (classified.code !== run.recovery.code || classified.kind !== run.recovery.kind) {
      const updatedAt = new Date().toISOString()
      run = { ...run, recovery: { ...run.recovery, ...classified, updatedAt }, updatedAt }
      state.updatedAt = updatedAt
      state.projects[project.name] = run
      const path = teamStatePath(workspace)
      atomicWriteJsonSync(path, state)
    }
  }

  // Older Desktop versions could mark the agent stage failed merely because
  // HEAD had already moved to a dirty release branch. Git is the stronger
  // source of truth here: if every reviewed work branch is merged and the
  // exact scope is satisfied in mergedBranch, restore the migration stage
  // instead of making the user rerun an already completed migration.
  if (migrationIsComplete(progress)) {
    if (completed.has('agent') && !(run.lastAction === 'agent' && run.status === 'failed')) return state
    const restored = restoreVerifiedAgentCompletion(run)
    const updatedAt = new Date().toISOString()
    state.updatedAt = updatedAt
    state.projects[project.name] = {
      ...run,
      ...restored,
      updatedAt,
    }
    const path = teamStatePath(workspace)
    atomicWriteJsonSync(path, state)
    return state
  }

  if (!completed.has('agent') || completed.has('release')) return state
  const reconciledActions = rollbackIncompleteMigrationActions([...completed], progress, run.lastAction)
  const unchanged = run.lastAction === 'agent' && run.status === 'failed'
    && reconciledActions.join(',') === (run.completedActions ?? []).join(',')
  if (unchanged) return state
  const updatedAt = new Date().toISOString()
  state.updatedAt = updatedAt
  state.projects[project.name] = {
    ...run,
    lastAction: 'agent',
    status: 'failed',
    updatedAt,
    completedActions: reconciledActions,
  }
  const path = teamStatePath(workspace)
  atomicWriteJsonSync(path, state)
  return state
}

type MigrationGate = { issues: string[]; feedback: string }

// The canonical validator inspects a directory on disk. Pointing it at the
// project checkout answers "does whatever branch the agent left checked out
// meet the whole scope", which is never the question -- a work branch legitimately
// carries only its own group. A detached worktree gives the validator the
// merged branch's real package.json and lockfile without disturbing the
// agent's checkout.
async function withMergedCheckout<T>(job: JobRecord, project: ProjectSpec, mergedBranch: string, currentBranch: string, run: (directory: string) => Promise<T>): Promise<T> {
  if (currentBranch === mergedBranch) return run(project.path)
  const { packageRelativePath } = await resolveProjectGitLayout(project)
  const temporaryTree = join(app.getPath('temp'), `dependency-flow-${job.id}-merged`)
  const added = await spawnCapture('git', ['-C', project.path, 'worktree', 'add', '--detach', temporaryTree, mergedBranch], project.path, 300_000)
  if (added.code !== 0) throw new Error(added.stderr.trim() || `не удалось подготовить проверку ветки ${mergedBranch}`)
  try {
    return await run(projectPathInWorktree(temporaryTree, packageRelativePath))
  } finally {
    await spawnCapture('git', ['-C', project.path, 'worktree', 'remove', '--force', temporaryTree], project.path, 120_000)
  }
}

async function agentMigrationIssues(job: JobRecord): Promise<MigrationGate> {
  if (!['agent', 'recover'].includes(job.action) || !job.projectName) return { issues: [], feedback: '' }
  const project = findProject(job.workspace, job.projectName)
  const progress = await readMigrationProgress(job.workspace, project)
  if (!progress) return { issues: ['не удалось прочитать Branch plan из сохранённого prompt'], feedback: '' }
  const feedback = [mergeInProgressNote(await mergeInProgress(project.path)), migrationStateSummary(progress)].filter(Boolean).join(' ')
  const progressIssues = migrationCompletionIssues(progress)
  if (progressIssues.length) return { issues: progressIssues, feedback }

  const promptPath = promptPathForProject(job.workspace, project.name)
  if (!promptPath || !existsSync(promptPath)) return { issues: ['не найден сохранённый prompt с точным scope manifest'], feedback }
  const promptMarkdown = readFileSync(promptPath, 'utf8')
  const scopeManifest = migrationScopeManifestFromPrompt(promptMarkdown)
  if (!scopeManifest) return { issues: ['prompt не содержит Exact compact scope manifest'], feedback }
  const roadmapPath = projectRoadmapPath(job.workspace, project.name)
  if (!roadmapPath || !existsSync(roadmapPath)) return { issues: ['не найден roadmap JSON для проверки согласованного scope'], feedback }
  const targetMode = scopeManifest.targetMode === 'default' || scopeManifest.targetMode === 'yellow' || scopeManifest.targetMode === 'green'
    ? scopeManifest.targetMode
    : job.target === 'green' ? 'green' : 'yellow'
  const temporaryScope = join(app.getPath('temp'), `dependency-flow-${job.id}-scope.json`)
  writeFileSync(temporaryScope, JSON.stringify(scopeManifest), 'utf8')
  try {
    const result = await withMergedCheckout(job, project, progress.mergedBranch, progress.currentBranch, (directory) => spawnCapture('python', [
      join(bundledToolDir(), 'validate_dependency_update.py'),
      '--roadmap-json', roadmapPath,
      '--project-dir', directory,
      '--project', project.name,
      '--target-mode', targetMode,
      '--scope-manifest', temporaryScope,
      '--json',
    ], job.workspace.path, 120_000))
    if (result.code === 0) return { issues: [], feedback }
    try {
      const payload = JSON.parse(result.stdout) as { findings?: Array<{ severity?: string; code?: string; package?: string; target?: string; actual?: string }> }
      const errors = (payload.findings ?? []).filter((finding) => finding.severity === 'error')
      if (errors.length) {
        const failedPackages = [...new Set(errors.map((finding) => finding.package ?? '<project>'))]
        const examples = failedPackages.slice(0, 12)
        return { issues: [`канонический scope не выполнен в ${progress.mergedBranch}: ${failedPackages.length} пакетов (${examples.join(', ')}${failedPackages.length > 12 ? ` и ещё ${failedPackages.length - 12}` : ''})`], feedback }
      }
    } catch { /* return the process failure below */ }
    return { issues: [`не удалось подтвердить точный scope: ${result.stderr.trim() || result.stdout.trim() || `validator exit=${result.code}`}`], feedback }
  } catch (error) {
    return { issues: [error instanceof Error ? error.message : String(error)], feedback }
  } finally {
    try { unlinkSync(temporaryScope) } catch { /* temporary file was already absent */ }
  }
}


function reserveLocalPort(): Promise<number> {
  return new Promise((resolvePromise, reject) => {
    const server = createNetServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : undefined
      server.close((error) => {
        if (error) reject(error)
        else if (port) resolvePromise(port)
        else reject(new Error('Не удалось подобрать локальный порт для OpenCode server.'))
      })
    })
  })
}

async function fetchWithTimeout(url: string, init?: RequestInit, timeoutMs = 2_000): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(url, { ...init, signal: controller.signal })
  } finally {
    clearTimeout(timer)
  }
}

const OPENCODE_SERVER_START_ATTEMPTS = 3

function openCodeTransportOwner(job: JobRecord): JobRecord {
  return migrationRootJob(job)
}

function ensureOpenCodeRuntime(job: JobRecord): string {
  const owner = openCodeTransportOwner(job)
  if (owner.openCodeDbPath) return owner.openCodeDbPath
  const generation = owner.openCodeDbGeneration ?? 0
  const paths = openCodeRuntimePaths(app.getPath('temp'), owner.id, generation)
  mkdirSync(paths.directory, { recursive: true })
  owner.openCodeRuntimeRoot = paths.root
  owner.openCodeDbGeneration = generation
  owner.openCodeDbPath = paths.databasePath
  return paths.databasePath
}

function rotateOpenCodeRuntime(job: JobRecord): string {
  const owner = openCodeTransportOwner(job)
  const generation = (owner.openCodeDbGeneration ?? 0) + 1
  const paths = openCodeRuntimePaths(app.getPath('temp'), owner.id, generation)
  mkdirSync(paths.directory, { recursive: true })
  owner.openCodeRuntimeRoot = paths.root
  owner.openCodeDbGeneration = generation
  owner.openCodeDbPath = paths.databasePath
  return paths.databasePath
}

function cleanupOpenCodeRuntime(job: JobRecord): void {
  const owner = openCodeTransportOwner(job)
  const root = owner.openCodeRuntimeRoot
  owner.openCodeRuntimeRoot = undefined
  owner.openCodeDbPath = undefined
  owner.openCodeDbGeneration = undefined
  if (!root) return
  try { rmSync(root, { recursive: true, force: true }) } catch { /* Windows may release the final SQLite handle a moment later; the unique next-run path still prevents reuse. */ }
}

function openCodeClientDatabase(job: JobRecord): { directory: string; databasePath: string } {
  const owner = openCodeTransportOwner(job)
  const serverDb = ensureOpenCodeRuntime(owner)
  const runtimeDir = dirname(serverDb)
  const directory = join(runtimeDir, `client-${randomUUID()}`)
  mkdirSync(directory, { recursive: true })
  return { directory, databasePath: join(directory, 'opencode.db') }
}

async function ensureOpenCodeServer(job: JobRecord, cwd: string): Promise<string | undefined> {
  if (job.agentProvider !== 'opencode') return undefined
  const owner = openCodeTransportOwner(job)
  if (owner.openCodeServer && owner.openCodeServerUrl && owner.openCodeServer.exitCode === null) return owner.openCodeServerUrl
  if (owner.openCodeServerStarting) return owner.openCodeServerStarting

  // Parallel groups can reach this function in the same event-loop turn. The
  // first await used to let every worker observe "no server yet" and each
  // spawn its own SQLite writer. Keep startup single-flight on the root job.
  const starting = (async (): Promise<string | undefined> => {
    let lastError = ''
    for (let startupAttempt = 1; startupAttempt <= OPENCODE_SERVER_START_ATTEMPTS; startupAttempt += 1) {
      const databasePath = startupAttempt === 1 ? ensureOpenCodeRuntime(owner) : rotateOpenCodeRuntime(owner)
      const port = await reserveLocalPort()
      const url = `http://127.0.0.1:${port}`
      const commandEnv = commandEnvironment(openCodeDatabaseEnv(process.env, databasePath))
      const opencodeInvocation = resolveSpawnInvocation('opencode', ['serve', '--hostname', '127.0.0.1', '--port', String(port)], { env: commandEnv })
      const server = spawn(opencodeInvocation.command, opencodeInvocation.args, {
        cwd,
        shell: false,
        detached: processTreeDetached(),
        windowsHide: true,
        windowsVerbatimArguments: opencodeInvocation.windowsVerbatimArguments,
        env: commandEnv,
      })
      owner.openCodeServer = server
      owner.openCodeServerUrl = url
      owner.openCodeServerError = ''
      server.stdout.on('data', (chunk: Buffer) => {
        // Keep server plumbing out of the user-facing activity feed; only retain
        // a short tail for a useful startup error.
        owner.openCodeServerError = `${owner.openCodeServerError ?? ''}${decodeProcessOutputChunk(chunk)}`.slice(-3000)
      })
      server.stderr.on('data', (chunk: Buffer) => {
        owner.openCodeServerError = `${owner.openCodeServerError ?? ''}${decodeProcessOutputChunk(chunk)}`.slice(-3000)
      })
      server.on('error', (error) => {
        owner.openCodeServerError = `${owner.openCodeServerError ?? ''}\n${error.message}`.slice(-3000)
      })

      for (let attempt = 0; attempt < 40; attempt += 1) {
        if (server.exitCode !== null) break
        try {
          const health = await fetchWithTimeout(`${url}/global/health`)
          if (health.ok) {
            send('flow:job-output', {
              jobId: job.id,
              stream: 'system',
              line: `OpenCode live session transport готов: ${url}. Используется изолированная runtime DB; параллельные агенты подключаются к одному sidecar вместо конкурирующих SQLite writers.`,
            })
            return url
          }
        } catch {
          // Server is still starting.
        }
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
      }

      if (server.exitCode === null) killProcessTree(server)
      lastError = owner.openCodeServerError?.trim() ?? ''
      owner.openCodeServer = undefined
      owner.openCodeServerUrl = undefined
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 150 * startupAttempt))
      if (openCodeDatabaseLocked(lastError) && startupAttempt < OPENCODE_SERVER_START_ATTEMPTS) {
        send('flow:job-output', {
          jobId: job.id,
          stream: 'system',
          line: `OpenCode SQLite занят другим процессом. Это INFRA, а не ошибка migration plan: переключаю sidecar на свежую изолированную DB и повторяю запуск (${startupAttempt + 1}/${OPENCODE_SERVER_START_ATTEMPTS}).`,
        })
        continue
      }
      break
    }

    throw new Error(`OPENCODE_SERVER_START_FAILED: не удалось запустить локальный opencode serve.${lastError ? `\n\n${lastError}` : ''}`)
  })()
  owner.openCodeServerStarting = starting
  try {
    return await starting
  } finally {
    if (owner.openCodeServerStarting === starting) owner.openCodeServerStarting = undefined
  }
}

function stopOpenCodeServer(job: JobRecord): void {
  const owner = openCodeTransportOwner(job)
  // Parallel workers borrow the root migration sidecar. Cleaning one worker
  // must never tear down transport for its still-running siblings.
  if (owner !== job) return
  if (owner.openCodeServer?.exitCode === null) killProcessTree(owner.openCodeServer)
  owner.openCodeServer = undefined
  owner.openCodeServerUrl = undefined
  owner.openCodeServerError = undefined
  owner.openCodeServerStarting = undefined
  cleanupOpenCodeRuntime(owner)
}

async function sendOpenCodeLiveMessage(job: JobRecord, note: string): Promise<boolean> {
  const owner = openCodeTransportOwner(job)
  if (job.agentProvider !== 'opencode' || !owner.openCodeServerUrl || !job.agentSessionId) return false
  try {
    const response = await fetchWithTimeout(
      `${owner.openCodeServerUrl}/session/${encodeURIComponent(job.agentSessionId)}/prompt_async`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ parts: [{ type: 'text', text: note }] }),
      },
      8_000,
    )
    return response.ok
  } catch {
    return false
  }
}

function agentStartSpec(provider: AgentProvider, project: ProjectSpec, promptPath: string, promptText: string, model?: string, userNote?: string, openCodeServerUrl?: string, gateFeedback?: string): CommandSpec {
  assertAgentContextBudget([promptText, gateFeedback, userNote], `${provider} start ${basename(promptPath)}`)
  if (provider === 'opencode') return { label: 'OpenCode migration agent', command: 'opencode', cwd: project.path, args: buildOpenCodeAgentArgs(project.path, promptPath, model, userNote, openCodeServerUrl, gateFeedback) }
  // opencode gets the note folded into its `run` message (above); claude/codex
  // get the whole prompt as literal stdin instead of a separate message
  // argument, so the note is appended there -- the saved prompt file on disk
  // (promptPath) is untouched either way, only what's actually sent changes.
  const feedbackText = gateFeedback ? `${promptText}\n\n## Фактическое состояние по данным оркестратора\n\n${gateFeedback}\n\nНе повторяй уже завершённую работу; доведи только оставшийся scope и повтори проверки.\n` : promptText
  const stdin = userNote ? `${feedbackText}\n\n## Сообщение от пользователя\n\n${userNote}\n\nУчти это как приоритетный контекст, но не как повод выйти за рамки неизменяемого Branch plan/manifest выше.\n` : feedbackText
  if (provider === 'claude') return { label: 'Claude migration agent', command: 'claude', cwd: project.path, args: buildClaudeAgentArgs(model), stdin }
  return { label: 'Codex migration agent', command: 'codex', cwd: project.path, args: buildCodexAgentArgs(project.path, model), stdin }
}

function agentResumeSpec(provider: AgentProvider, project: ProjectSpec, sessionId: string, promptPath: string, model?: string, gateFeedback?: string, labelPrefix = 'Продолжение', userNote?: string, openCodeServerUrl?: string): CommandSpec {
  // Resume never re-attaches the original prompt. Bound the application-owned
  // recovery message even though provider-owned historical tool context is not
  // observable here.
  assertAgentContextBudget([promptPath, gateFeedback, userNote], `${provider} resume ${basename(promptPath)}`)
  if (provider === 'opencode') return { label: `${labelPrefix} OpenCode migration agent`, command: 'opencode', cwd: project.path, args: buildOpenCodeResumeArgs(project.path, sessionId, model, promptPath, gateFeedback, userNote, openCodeServerUrl) }
  if (provider === 'claude') return { label: `${labelPrefix} Claude migration agent`, command: 'claude', cwd: project.path, args: buildClaudeResumeArgs(sessionId, model, promptPath, gateFeedback, userNote) }
  return { label: `${labelPrefix} Codex migration agent`, command: 'codex', cwd: project.path, args: buildCodexResumeArgs(sessionId, model, promptPath, gateFeedback, userNote) }
}

// Populates the model suggestion list from whatever the agent itself reports
// as configured/available, instead of a value we'd have to guess and keep in
// sync by hand. `opencode models` prints one `provider/model` id per line —
// exactly the format its own `--model` flag expects. Codex has no equivalent
// discovery command; Claude Code has no such command either, but its short
// aliases (opus/sonnet/haiku) are a stable part of the CLI's own public
// surface, not an environment-specific value, so they're safe to offer.
async function listAgentModels(agentProvider: AgentProvider, cwd: string): Promise<string[]> {
  if (agentProvider === 'opencode') {
    const runtime = openCodeRuntimePaths(app.getPath('temp'), `models-${randomUUID()}`)
    mkdirSync(runtime.directory, { recursive: true })
    try {
      const result = await spawnCapture('opencode', ['models'], cwd, 15_000, { OPENCODE_DB: runtime.databasePath })
      return result.code === 0 ? parseOpencodeModelsOutput(result.stdout) : []
    } finally {
      try { rmSync(runtime.root, { recursive: true, force: true }) } catch { /* model discovery runtime is disposable */ }
    }
  }
  if (agentProvider === 'claude') return ['opus', 'sonnet', 'haiku']
  return []
}

function promptStaleAgainstDashboard(workspace: WorkspaceRecord, projectName: string, dashboardPath: string): boolean {
  const promptPath = promptPathForProject(workspace, projectName)
  if (!promptPath || !existsSync(promptPath) || !existsSync(dashboardPath)) return false
  try {
    return statSync(dashboardPath).mtimeMs > statSync(promptPath).mtimeMs
  } catch {
    return false
  }
}

async function materializeContinuationPrompt(
  job: JobRecord,
  project: ProjectSpec,
  promptPath: string,
  markdown: string,
): Promise<{ promptPath: string; markdown: string }> {
  const parsedPlan = migrationPlanFromPrompt(markdown, project.name)
  if (!parsedPlan?.mergedBranch) return { promptPath, markdown }
  let plan = recoverContinuationScopeBranches(markdown, project.name, parsedPlan)
  const mergedLocal = await spawnCapture('git', ['-C', project.path, 'show-ref', '--verify', '--quiet', 'refs/heads/' + plan.mergedBranch], project.path, 15_000)
  const mergedRemote = mergedLocal.code === 0
    ? undefined
    : await spawnCapture('git', ['-C', project.path, 'show-ref', '--verify', '--quiet', 'refs/remotes/origin/' + plan.mergedBranch], project.path, 15_000)
  const mergedRef = mergedLocal.code === 0 ? plan.mergedBranch : mergedRemote?.code === 0 ? 'origin/' + plan.mergedBranch : undefined
  if (!mergedRef) return { promptPath, markdown }

  const { packageRelativePath } = await resolveProjectGitLayout(project)
  const packageResult = await spawnCapture('git', ['-C', project.path, 'show', mergedRef + ':' + projectTreePath(packageRelativePath, 'package.json')], project.path, 15_000)
  if (packageResult.code !== 0) return { promptPath, markdown }
  let satisfied: Set<string> | undefined
  try {
    satisfied = satisfiedScopePackagesFromPrompt(markdown, project.name, JSON.parse(packageResult.stdout))
  } catch {
    return { promptPath, markdown }
  }
  if (!satisfied) return { promptPath, markdown }

  const refsResult = await spawnCapture('git', ['-C', project.path, 'for-each-ref', '--format=%(refname:short)', 'refs/heads', 'refs/remotes/origin'], project.path, 15_000)
  if (refsResult.code !== 0) return { promptPath, markdown }
  const refs = refsResult.stdout.split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
  const refSet = new Set(refs)
  const emptyResult = await spawnCapture('git', ['-C', project.path, 'for-each-ref', '--points-at=' + mergedRef, '--format=%(refname:short)', 'refs/heads', 'refs/remotes/origin'], project.path, 15_000)
  if (emptyResult.code !== 0) return { promptPath, markdown }
  const emptyBranches = new Set(emptyResult.stdout.split(/\r?\n/).map((value) => value.trim()).filter(Boolean).map((value) => value.startsWith('origin/') ? value.slice('origin/'.length) : value))

  // A fresh parallel wave may have useful uncommitted work under the logical
  // scope branch just before a residual refresh allocates a continuation
  // alias. Prefer whichever tool-owned worktree proves more package targets;
  // otherwise a restart resumes an older alias and silently strands newer work.
  const worktreeTargetCounts: Record<string, number> = {}
  for (const branch of plan.branches) {
    for (const candidate of new Set([branch.branch, branch.scopeBranch].filter((value): value is string => Boolean(value)))) {
      const worktreePath = await worktreePathForBranch(project.path, candidate)
      if (!worktreePath || !isToolManagedWorktreePath(worktreePath, app.getPath('temp'))) continue
      try {
        const candidateProjectPath = projectPathInWorktree(worktreePath, packageRelativePath)
        const candidateSatisfied = satisfiedScopePackagesFromPrompt(markdown, project.name, JSON.parse(readFileSync(join(candidateProjectPath, 'package.json'), 'utf8')))
        if (candidateSatisfied) worktreeTargetCounts[candidate] = branch.packages.filter((name) => candidateSatisfied.has(name)).length
      } catch { /* a malformed worker remains untouched and is not adopted */ }
    }
  }
  const preferredScopePlan = adoptPreferredScopeBranches(plan, worktreeTargetCounts)
  const preferredScopeAdopted = preferredScopePlan !== plan
  if (preferredScopeAdopted) plan = preferredScopePlan

  // A continuation branch can outlive the exact prompt revision that created
  // it. Before declaring it "unexpected", recover tool-created continuation
  // topology from persisted plan history when the exact package scope still
  // matches and the branch has not already landed in merged.
  const continuationRoot = plan.mergedBranch.replace(/-merged(?:-\d+)?$/, '') || plan.mergedBranch
  const continuationPrefix = continuationRoot + '-continuation-'
  const reusableContinuationBranches = new Set<string>()
  for (const ref of refs) {
    const local = ref.startsWith('origin/') ? ref.slice('origin/'.length) : ref
    if (!local.startsWith(continuationPrefix) || reusableContinuationBranches.has(local)) continue
    const ancestor = await spawnCapture('git', ['-C', project.path, 'merge-base', '--is-ancestor', ref, mergedRef], project.path, 15_000)
    if (ancestor.code === 1) reusableContinuationBranches.add(local)
  }
  const historicalPlans: MigrationPlan[] = []
  const planHistoryDir = join(job.workspace.path, '.dependency-roadmap', 'desktop', 'plans')
  if (existsSync(planHistoryDir)) {
    for (const name of readdirSync(planHistoryDir).filter((value) => value.endsWith('.md')).sort().reverse().slice(0, 80)) {
      try {
        const historical = migrationPlanFromPrompt(readFileSync(join(planHistoryDir, name), 'utf8'), project.name)
        if (historical) historicalPlans.push(historical)
      } catch {
        // Corrupt/stale historical plans are advisory only; current prompt remains source of truth.
      }
    }
  }
  const historicalAdoptedPlan = adoptHistoricalContinuationBranches(plan, historicalPlans, reusableContinuationBranches)

  const persistPlan = (nextPlan: MigrationPlan, message: string): { promptPath: string; markdown: string } => {
    const rewritten = replaceMigrationPlanInPrompt(markdown, project.name, nextPlan)
    if (!rewritten) throw new Error('AGENT_CONTINUATION_PLAN_FAILED: could not safely rewrite Branch plan.')
    const plansDir = join(job.workspace.path, '.dependency-roadmap', 'desktop', 'plans')
    mkdirSync(plansDir, { recursive: true })
    const nextPath = join(plansDir, new Date().toISOString().replace(/[:.]/g, '-') + '-' + project.name.replace(/[^a-zA-Z0-9._-]+/g, '-') + '-continuation.md')
    writeFileSync(nextPath, rewritten, 'utf8')
    persistLatestPrompt(job.workspace, project.name, nextPath)
    send('flow:job-output', { jobId: job.id, stream: 'system', line: message })
    return { promptPath: nextPath, markdown: rewritten }
  }
  if (preferredScopeAdopted) {
    return persistPlan(
      plan,
      'Recovered newer uncommitted progress from a tool-managed scope worktree: ' + plan.branches.map((branch) => branch.branch).join(', ') + '. The older continuation alias remains untouched.',
    )
  }
  if (historicalAdoptedPlan !== plan) {
    return persistPlan(
      historicalAdoptedPlan,
      'Recovered a previously persisted continuation branch with the same package scope: ' + historicalAdoptedPlan.branches.map((branch) => branch.branch).join(', ') + '. Existing agent work is reused; no ref was invented or deleted.',
    )
  }
  const adoptedPlan = adoptEmptyContinuationBranches(plan, refs, emptyBranches)
  if (adoptedPlan !== plan) {
    return persistPlan(adoptedPlan, 'Recovered continuation mapping and reused empty branch ' + adoptedPlan.branches.map((branch) => branch.branch).join(', ') + ' from merged baseline; no historical refs were changed.')
  }
  const mergedButUnmet = new Set<string>()
  for (const branch of plan.branches) {
    if (branch.packages.every((packageName) => satisfied?.has(packageName))) continue
    const branchRef = refSet.has(branch.branch) ? branch.branch : refSet.has('origin/' + branch.branch) ? 'origin/' + branch.branch : undefined
    if (!branchRef || emptyBranches.has(branch.branch)) continue
    const ancestry = await spawnCapture('git', ['-C', project.path, 'merge-base', '--is-ancestor', branchRef, mergedRef], project.path, 15_000)
    if (ancestry.code === 0) mergedButUnmet.add(branch.branch)
  }
  if (!mergedButUnmet.size) return { promptPath, markdown }

  const generatedPlan = continuationMigrationPlan(plan, mergedButUnmet, refs)
  const continuationPlan = adoptEmptyContinuationBranches(generatedPlan, refs, emptyBranches)
  const continuationBranches = continuationPlan.branches
    .filter((_, index) => mergedButUnmet.has(plan.branches[index].branch))
    .map((branch) => branch.branch)
  return persistPlan(
    continuationPlan,
    'Residual plan uses ' + plan.mergedBranch + ' as baseline and continues in ' + continuationBranches.join(', ') + '; historical group branches remain untouched.',
  )
}

async function ensureCurrentAgentPrompt(workspace: WorkspaceRecord, project: ProjectSpec, target: ClosureTarget): Promise<string> {
  const dashboardPath = projectDashboardPath(workspace, project.name)
  const current = promptPathForProject(workspace, project.name)
  const currentPlan = current && existsSync(current) ? migrationPlanFromPrompt(readFileSync(current, 'utf8'), project.name) : undefined
  if (current && currentPlan && dashboardPath && !promptStaleAgainstDashboard(workspace, project.name, dashboardPath)) return current
  if (!dashboardPath || !existsSync(dashboardPath)) throw new Error('AGENT_PROMPT_AUTOBUILD_FAILED: Dashboard этого проекта ещё не построен. Сначала нужен baseline/roadmap.')
  const dashboardUrl = `${DASHBOARD_SCHEME}://dashboard/${encodeURIComponent(workspace.id)}?desktop-export=1&autopilot=1&project=${encodeURIComponent(project.name)}&v=${Date.now()}`
  const markdown = await buildProjectPrompt(dashboardUrl, project.name, target)
  const generatedPlan = migrationPlanFromPrompt(markdown, project.name)
  if (!generatedPlan) {
    const closure = readTargetClosure(workspace, project, target)
    const needsGoalSeekingSupervisor = shouldUseSupervisorSeed(closure, target, Boolean(currentPlan))
    if (current && needsGoalSeekingSupervisor) {
      send('flow:job-output', { jobId: 'system', stream: 'system', workspaceId: workspace.id, projectName: project.name, line: `Свежий Dashboard не содержит executable Branch plan для ${project.name}: сохраняю валидный предыдущий plan только как seed для goal-seeking Supervisor. Пустой scope не будет отправлен Executor.` })
      return current
    }
    throw new Error(`AGENT_PROMPT_AUTOBUILD_FAILED: Dashboard не вернул Branch plan для ${project.name}.`)
  }
  const plansDir = join(workspace.path, '.dependency-roadmap', 'desktop', 'plans')
  mkdirSync(plansDir, { recursive: true })
  const path = join(plansDir, `${new Date().toISOString().replace(/[:.]/g, '-')}-${project.name.replace(/[^a-zA-Z0-9._-]+/g, '-')}-${target}-auto.md`)
  writeFileSync(path, markdown, 'utf8')
  persistLatestPrompt(workspace, project.name, path)
  return path
}

// The same closure the FLOW gate itself evaluates, handed to the UI so a
// blocked goal can be explained package-by-package instead of as a bare
// percentage the user has to reverse-engineer.
function readTargetClosure(workspace: WorkspaceRecord, project: ProjectSpec | undefined, target: ClosureTarget): TargetClosure | undefined {
  if (!project) return undefined
  const roadmapPath = projectRoadmapPath(workspace, project.name)
  if (!roadmapPath || !existsSync(roadmapPath)) return undefined
  try {
    const roadmap = JSON.parse(readFileSync(roadmapPath, 'utf8')) as unknown
    const promptPath = promptPathForProject(workspace, project.name)
    if (!promptPath || !existsSync(promptPath)) return targetClosureFromRoadmap(roadmap, project.name, target)
    const markdown = readFileSync(promptPath, 'utf8')
    const plannedTargets = migrationPlanFromPrompt(markdown, project.name) ? scopeTargetsFromPrompt(markdown, project.name) : {}
    return targetClosureFromRoadmapWithTargets(roadmap, project.name, target, plannedTargets)
  } catch {
    return undefined
  }
}

async function workspaceDetails(workspace: WorkspaceRecord): Promise<WorkspaceDetails> {
  const settingsPath = resolveSettingsPath(workspace)
  const projects = readProjects(workspace)
  const project = projects.find((item) => item.name === workspace.selectedProject) ?? projects[0]
  const dashboardPath = project ? projectDashboardPath(workspace, project.name) : undefined
  const projectDashboardExists = Boolean(project && dashboardPath && existsSync(dashboardPath) && dashboardHasProject(workspace, project.name))
  const migrationProgress = project ? await readMigrationProgress(workspace, project) : undefined
  let teamState = reconcileTeamState(workspace, project, migrationProgress)
  // Upgrade legacy/on-disk states in memory so the UI can offer recovery even
  // when the failure happened before recovery metadata existed (the concrete
  // v0.1.57 case is a prepared dirty release branch after all groups merged).
  if (project && teamState?.projects[project.name] && !teamState.projects[project.name].recovery) {
    const inferred = await inferredRecoveryIssue(workspace, project)
    if (inferred) {
      teamState = {
        ...teamState,
        projects: { ...teamState.projects, [project.name]: { ...teamState.projects[project.name], recovery: inferred } },
      }
    }
  }
  const savedTarget = project ? teamState?.projects[project.name]?.target : undefined
  return {
    workspace,
    projects,
    settingsExists: existsSync(settingsPath),
    dashboardPath,
    dashboardUrl: projectDashboardExists && dashboardPath ? `${DASHBOARD_SCHEME}://dashboard/${encodeURIComponent(workspace.id)}?project=${encodeURIComponent(project?.name || '')}&v=${Math.trunc(statSync(dashboardPath).mtimeMs)}` : undefined,
    dashboardExists: projectDashboardExists,
    projectPromptPath: project ? promptPathForProject(workspace, project.name) : undefined,
    promptStale: project && dashboardPath ? promptStaleAgainstDashboard(workspace, project.name, dashboardPath) : false,
    git: await gitOverview(workspace.path),
    teamState,
    projectLevels: readProjectLevels(workspace),
    targetClosure: readTargetClosure(workspace, project, savedTarget === 'green' ? 'green' : 'yellow'),
    migrationProgress,
    baselineRecovery: baselineRecoveryInfo(workspace, project?.name),
  }
}

async function environmentInfo(): Promise<Record<string, { available: boolean; version: string }>> {
  const checks = await Promise.all([
    ['git', ['--version']],
    ['python', ['--version']],
    ['node', ['--version']],
    ['npm', ['--version']],
    ['codex', ['--version']],
    ['opencode', ['--version']],
    ['claude', ['--version']],
  ].map(async ([command, args]) => {
    const result = await spawnCapture(command as string, args as string[], process.cwd())
    return [command, { available: result.code === 0, version: (result.stdout || result.stderr).trim().split(/\r?\n/).at(-1) ?? '' }] as const
  }))
  return Object.fromEntries(checks)
}

function jobById(jobId: string): JobRecord | undefined {
  const direct = jobs.get(jobId)
  if (direct) return direct
  for (const root of jobs.values()) {
    const nested = root.parallelJobs ? [...root.parallelJobs.values()].find((job) => job.id === jobId) : undefined
    if (nested) return nested
  }
  return undefined
}

function send(channel: string, payload: unknown): void {
  let outgoing = payload
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    if (typeof record.jobId === 'string') {
      const job = jobById(record.jobId)
      if (job) {
        const enriched: Record<string, unknown> = {
          ...record,
          workspaceId: record.workspaceId ?? job.workspace.id,
          projectName: record.projectName ?? job.projectName,
        }
        if (channel === 'flow:job-output' && !enriched.source && job.logSource) enriched.source = job.logSource
        outgoing = enriched
      }
    }
  }
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, outgoing)
}

function migrationRootJob(job: JobRecord): JobRecord {
  return job.parallelParent ?? job
}

function setBranchRuntime(job: JobRecord, branch: string, phase?: MigrationBranchRuntimePhase, detail?: string): void {
  const root = migrationRootJob(job)
  root.branchRuntime ??= new Map<string, MigrationBranchRuntime>()
  if (phase) root.branchRuntime.set(branch, { phase, ...(detail ? { detail } : {}), updatedAt: new Date().toISOString() })
  else root.branchRuntime.delete(branch)
  send('flow:migration-progress-changed', { jobId: root.id, workspaceId: root.workspace.id, projectName: root.projectName, branch, phase })
}

function migrationRuntime(workspace: WorkspaceRecord, projectName: string): Record<string, MigrationBranchRuntime> {
  for (const job of jobs.values()) {
    const root = migrationRootJob(job)
    if (root.workspace.id === workspace.id && root.projectName === projectName && root.branchRuntime?.size) {
      return Object.fromEntries(root.branchRuntime)
    }
  }
  return {}
}

function publishUpdateStatus(status: UpdateStatus): void {
  currentUpdateStatus = status
  send('flow:update-status', status)
}

function publishUpdaterError(error: unknown): UpdateStatus {
  const status = updateErrorStatus(error)
  const visibleStatus: UpdateStatus = downloadedUpdateVersion
    ? { ...status, state: 'ready', version: downloadedUpdateVersion }
    : status
  publishUpdateStatus(visibleStatus)
  return visibleStatus
}

function checkForLatestUpdate(): ReturnType<typeof autoUpdater.checkForUpdates> {
  if (updateCheckInFlight) return updateCheckInFlight
  const request = autoUpdater.checkForUpdates().finally(() => {
    if (updateCheckInFlight === request) updateCheckInFlight = undefined
  })
  updateCheckInFlight = request
  return request
}

async function installLatestUpdate(): Promise<void> {
  if (!app.isPackaged || updateInstallInProgress) return
  updateInstallInProgress = true
  try {
    // A package may have been downloaded before a newer release appeared.
    // Refresh the feed and wait for that newest package instead of installing
    // the stale cached one and making the user upgrade again on next launch.
    const result = await checkForLatestUpdate()
    if (!result?.isUpdateAvailable) return
    const latestVersion = result.updateInfo.version
    if (downloadedUpdateVersion !== latestVersion) {
      await (result.downloadPromise ?? autoUpdater.downloadUpdate())
    }
    if (downloadedUpdateVersion !== latestVersion) {
      throw new Error(`Update ${latestVersion} did not finish downloading.`)
    }
    autoUpdater.quitAndInstall(false, true)
  } catch (error) {
    publishUpdaterError(error)
  } finally {
    updateInstallInProgress = false
  }
}

function findWorkspace(state: DesktopState, id?: string): WorkspaceRecord {
  const workspace = state.workspaces.find((item) => item.id === id) ?? selectedWorkspace(state)
  if (!workspace) throw new Error('Сначала выберите или создайте рабочий набор команды.')
  return workspace
}

function findProject(workspace: WorkspaceRecord, projectName?: string): ProjectSpec {
  const projects = readProjects(workspace)
  const project = projects.find((item) => item.name === projectName) ?? projects.find((item) => item.name === workspace.selectedProject) ?? projects[0]
  if (!project) throw new Error('В settings.project.json не найден ни один проект.')
  return project
}

function bundledToolDir(): string {
  return app.isPackaged ? join(process.resourcesPath, 'tool') : resolve(app.getAppPath(), '..')
}

function generatorPath(): string {
  return join(bundledToolDir(), 'dependency_live_roadmap_generator.py')
}

function actionCommands(input: ActionInput, workspace: WorkspaceRecord, project: ProjectSpec): CommandSpec[] {
  const toolDir = bundledToolDir()
  const settingsPath = resolveSettingsPath(workspace)
  const commonGeneratorArgs = [generatorPath(), '--project-settings', settingsPath, '--only-project', project.name]
  switch (input.action) {
    case 'preflight': {
      const manager = projectPackageManager(project)
      return [
        { label: 'Проверка Git рабочего набора', command: 'git', args: ['-C', workspace.path, 'status', '--short', '--branch'], cwd: workspace.path },
        { label: 'Проверка Git проекта', command: 'git', args: ['-C', project.path, 'status', '--short', '--branch'], cwd: project.path },
        { label: 'Проверка Python', command: 'python', args: ['--version'], cwd: workspace.path },
        { label: 'Проверка Node.js', command: 'node', args: ['--version'], cwd: project.path },
        { label: `Проверка package manager (${manager})`, command: manager, args: ['--version'], cwd: project.path },
        ...(app.isPackaged ? [{ label: 'Проверка bundled Z3', command: 'python', args: ['-c', 'import z3; print(z3.get_version_string())'], cwd: workspace.path }] : []),
        { label: 'Проверка генератора', command: 'python', args: [generatorPath(), '--help'], cwd: workspace.path },
      ]
    }
    case 'sync-tool':
      return [{ label: 'Проверка встроенного tool', command: 'python', args: [generatorPath(), '--help'], cwd: workspace.path }]
    case 'baseline': {
      const persistedIntent = loadBaselineIntent(workspace, project.name)
      const explicitIntent = input.baselineIntent ? normalizeBaselineIntent(input.baselineIntent) : undefined
      const effectiveIntent = explicitIntent ?? persistedIntent
      if (explicitIntent) saveBaselineIntent(workspace, project.name, explicitIntent)
      const executionMode: BaselineExecutionMode = effectiveIntent.executionMode === 'BACKGROUND' ? 'BACKGROUND' : 'FAST'
      const automaticBudgetSeconds = executionMode === 'FAST' ? 15 * 60 : executionMode === 'BACKGROUND' ? 2 * 60 * 60 : 30 * 60
      const maxExpensiveAttempts = executionMode === 'FAST' ? 2 : executionMode === 'BACKGROUND' ? 8 : 4

      // Concurrent project Baselines must not race on the workspace's shared
      // dependency-roadmap.{md,json,html} publication. Baseline evidence and
      // verification caches stay on their normal exact/project identities; only
      // human-facing generator outputs are redirected to a project-private sink.
      const baselineProjectOutput = baselineProjectOutputDir(workspace, project.name)
      mkdirSync(baselineProjectOutput, { recursive: true })
      const baselineOutputArgs = [
        '--out', join(baselineProjectOutput, 'dependency-roadmap.md'),
        '--json-out', join(baselineProjectOutput, 'dependency-roadmap.json'),
        '--html-out', join(baselineProjectOutput, 'dependency-roadmap.html'),
        // The canonical Baseline history entry is still captured below. The
        // automatic dashboard snapshot is workspace-global and is deferred to
        // the normal generate stage to avoid cross-project publication races.
        '--no-history-snapshot',
      ]

      return [{
        label: 'Создание исходного baseline', command: 'python', cwd: workspace.path,
        args: [...commonGeneratorArgs, ...baselineOutputArgs, '--capture-baseline', '--baseline-label', input.label?.trim() || `dependency-flow-${new Date().toISOString().slice(0, 10)}`],
        env: {
          DEPLOOM_BASELINE_RESUME: input.baselineResume === 'restart' ? 'restart' : input.baselineResume === 'continue' ? 'continue' : 'auto',
          DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE: input.baselineResume === 'continue' ? '1' : '0',
          DEPLOOM_BASELINE_INTENT_JSON: JSON.stringify({ schemaVersion: 1, policies: effectiveIntent.policies, executionMode }),
          DEPLOOM_BASELINE_INTERACTIVE: '1',
          DEPLOOM_BASELINE_EXECUTION_MODE: executionMode,
          DEPLOOM_BASELINE_EXTRA_ITERATIONS: String(effectiveIntent.extraIterations ?? 0),
          DEPLOOM_BASELINE_DECISION_GRANT_ITERATIONS: String(explicitIntent?.decisionGrantIterations ?? 0),
          DEPLOOM_BASELINE_SEARCH_MODE: explicitIntent?.searchMode ?? 'AUTO',
          DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS: String(automaticBudgetSeconds),
          DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS: String(maxExpensiveAttempts),
          ...(executionMode === 'BACKGROUND' ? { DEPLOOM_IO_COPY_SLOTS: '1', DEPLOOM_IO_HASH_SLOTS: '1', DEPLOOM_IO_PM_SLOTS: '1' } : {}),
        },
        stallWarningMs: 2 * 60_000,
        stallAbortMs: 15 * 60_000,
      }]
    }
    case 'generate':
      return [{
        label: 'Построение свежего roadmap', command: 'python', cwd: workspace.path,
        args: [...commonGeneratorArgs, '--history-snapshot-label', input.label?.trim() || 'DepLoom: после итерации'],
        stallWarningMs: 2 * 60_000,
        stallAbortMs: 15 * 60_000,
      }]
    case 'generate-all':
      return [{
        label: 'Актуализация roadmap всех проектов', command: 'python', cwd: workspace.path,
        args: [generatorPath(), '--project-settings', settingsPath, '--history-snapshot-label', input.label?.trim() || 'DepLoom: все проекты'],
        stallWarningMs: 2 * 60_000,
        stallAbortMs: 15 * 60_000,
      }]
    case 'audit': {
      const slug = project.name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-')
      const artifacts = artifactPath(workspace, 'artifactsDir', '.dependency-roadmap/artifacts')
      return [{
        label: 'Независимый audit', command: 'python', cwd: workspace.path,
        args: [join(toolDir, 'manual_dependency_audit.py'), '--project-dir', project.path, '--project-name', project.name,
          '--dashboard-state', artifactPath(workspace, 'dashboardState', '.dependency-roadmap/state/dashboard-state.json'),
          '--audit-workspace', join(artifacts, `manual-audit-${slug}-workspace`),
          '--json-out', join(artifacts, `manual-audit-${slug}.json`), '--md-out', join(artifacts, `manual-audit-${slug}.md`)],
      }]
    }
    case 'recover':
      return []
    case 'agent': {
      // A manual migration launch after a fresh Baseline intentionally has no
      // saved prompt yet: Step 3 is the explicit planning boundary. The
      // registered agent job builds a fresh project-scoped prompt itself via
      // ensureCurrentAgentPrompt(). Only a user-supplied prompt path needs to
      // be validated synchronously here.
      if (input.promptPath) {
        if (!existsSync(input.promptPath)) throw new Error(`Выбранный prompt не найден: ${input.promptPath}`)
        const promptMarkdown = readFileSync(input.promptPath, 'utf8')
        if (!migrationPlanFromPrompt(promptMarkdown, project.name)) {
          throw new Error(`Выбранный prompt не содержит Branch plan для проекта ${project.name}. Выберите prompt именно этого проекта.`)
        }
        persistLatestPrompt(workspace, project.name, input.promptPath)
      }
      // runMigrationAgentLoop (invoked from executeJob) owns prompt creation,
      // branch creation, agent sessions and merge; no fixed command is needed.
      return []
    }
    case 'release': {
      const sourceBranch = project.git?.sourceBranch || 'master'
      const mergedBranch = project.git?.mergedBranch || 'libs-merged'
      const releaseBranch = input.releaseBranch?.trim() || project.git?.releaseBranch || 'libs-release'
      if (!input.sourceCommit) throw new Error('Не удалось определить source commit для release.')
      const settings = readSettings(workspace)
      const releasePolicy = releasePolicyForProject(settings, project.name)
      const args = [join(toolDir, 'dependency_release_branch.py'), '--project-dir', project.path,
        '--source-branch', sourceBranch, '--source-commit', input.sourceCommit,
        '--merged-branch', mergedBranch, '--release-branch', releaseBranch,
        '--commit-message', releasePolicy.commitMessage]
      for (const gateCommand of releaseGateCommands(settings, project.name, input.gateCommand)) {
        args.push('--gate-command', gateCommand)
      }
      return [{ label: 'Создание чистой release-ветки', command: 'python', cwd: workspace.path, args }]
    }
    case 'commit-state': {
      const settings = readSettings(workspace)
      const trackedRoots = ['.dependency-roadmap', 'knowledge'].filter((value) => existsSync(resolve(workspace.path, value)))
      const newStatePaths = teamStatePaths(workspace.settingsPath, settings).filter((value) => existsSync(resolve(workspace.path, value)))
      return [
        ...(trackedRoots.length ? [{ label: 'Обновление отслеживаемых state-файлов', command: 'git', cwd: workspace.path, args: ['-C', workspace.path, 'add', '-u', '--', ...trackedRoots] }] : []),
        ...(newStatePaths.length ? [{ label: 'Добавление новых state-файлов', command: 'git', cwd: workspace.path, args: ['-C', workspace.path, 'add', '--', ...newStatePaths] }] : []),
        { label: 'Коммит состояния команды', command: 'git', cwd: workspace.path, args: ['-C', workspace.path, 'commit', '-m', input.commitMessage?.trim() || `chore(deps): save ${project.name} roadmap state`], skipWhenNoStagedChanges: true },
      ]
    }
    case 'push-workspace': {
      const releaseBranch = input.releaseBranch?.trim() || project.git?.releaseBranch || 'libs-release'
      return [
        { label: `Публикация release-ветки ${releaseBranch}`, command: 'git', cwd: project.path, args: ['-C', project.path, 'push', '-u', 'origin', releaseBranch] },
        { label: 'Подготовка финального состояния FLOW', command: 'git', cwd: workspace.path, args: ['-C', workspace.path, 'add', '--', '.dependency-roadmap/desktop/flow-state.json'], finalizeTeamStateBeforeRun: true },
        { label: 'Коммит финального состояния FLOW', command: 'git', cwd: workspace.path, args: ['-C', workspace.path, 'commit', '-m', `chore(deps): complete ${project.name} dependency flow`], skipWhenNoStagedChanges: true },
        { label: 'Публикация командного state', command: 'git', cwd: workspace.path, args: ['-C', workspace.path, 'push', 'origin', 'HEAD'] },
      ]
    }
  }
}

function emptyHooksPath(): string {
  const path = join(app.getPath('userData'), 'empty-git-hooks')
  mkdirSync(path, { recursive: true })
  return path
}

// A genuine fresh restart ("Начать заново") must leave nothing from a
// superseded attempt for the next run to trip over: an old work or merged
// branch still on disk would make the orchestrator think that group is
// already done, or resume building on stale merged content instead of a
// clean base. Scoped strictly to branches named in the *current* saved
// Branch plan (work branches + merged) -- never the source/base branch,
// never a release branch, never anything outside the plan -- and only ever
// issued for branches confirmed to exist locally, so a missing branch never
// turns into a failed `git branch -D`.
async function migrationBranchCleanupCommands(
  project: ProjectSpec,
  plan: MigrationPlan | undefined,
  preserveCurrentCheckout = false,
): Promise<CommandSpec[]> {
  // Even without a readable old prompt, stale DepLoom worktrees are
  // still ours and can safely be removed. Branch deletion remains scoped to
  // the exact saved plan when one exists.
  const candidates = plan ? [...new Set([...plan.branches.map((branch) => branch.branch), plan.mergedBranch].filter(Boolean))] : []

  // `git branch -D` refuses to delete a branch that is still registered in a
  // linked worktree. Fresh restart owns DepLoom's temp worktrees, so
  // remove those first. Stale/prunable registrations are handled by prune.
  // User-owned worktrees are never deleted automatically.
  const worktreeList = await spawnCapture('git', ['-C', project.path, 'worktree', 'list', '--porcelain'], project.path, 30_000)
  if (worktreeList.code !== 0) throw new Error(worktreeList.stderr.trim() || 'Не удалось прочитать git worktree перед новой миграцией.')
  const cleanup = restartWorktreeCleanupTargets(
    liveGitWorktreeRecords(worktreeList.stdout),
    candidates,
    project.path,
    app.getPath('temp'),
  )
  if (cleanup.blockedUserWorktrees.length) {
    const details = cleanup.blockedUserWorktrees.map((item) => `${item.branch}: ${item.path}`).join('; ')
    throw new Error(`MIGRATION_RESTART_USER_WORKTREE_BLOCKED: ветка предыдущей попытки используется внешним worktree. DepLoom не удаляет пользовательские worktree автоматически: ${details}`)
  }

  const current = preserveCurrentCheckout
    ? await spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000)
    : undefined
  if (current && current.code !== 0) {
    throw new Error(current.stderr.trim() || 'Не удалось определить текущую ветку перед очисткой старого Baseline epoch.')
  }
  const preservedBranch = current?.stdout.trim() || ''
  const existing = await Promise.all(candidates.map(async (branch) => {
    const result = await spawnCapture('git', ['-C', project.path, 'show-ref', '--verify', '--quiet', `refs/heads/${branch}`], project.path)
    return result.code === 0 ? branch : undefined
  }))
  const hooksArg = `core.hooksPath=${emptyHooksPath()}`
  const commands: CommandSpec[] = []

  // Remove every still-present DepLoom worktree registered for this
  // repository, not only worktrees whose branch happens to remain in the saved
  // plan. Missing directories are intentionally left to `worktree prune`, which
  // makes restart idempotent after manual/partial cleanup. Repeating --force is
  // safe for our disposable temp worktrees and also removes a stale locked one.
  for (const path of cleanup.toolManagedPaths.filter((candidate) => existsSync(candidate))) {
    commands.push({
      label: `Удаление временного DepLoom worktree: ${path}`,
      command: 'git',
      cwd: project.path,
      args: ['-c', hooksArg, '-C', project.path, 'worktree', 'remove', '--force', path],
    })
  }
  commands.push({
    label: 'Очистка устаревших worktree-регистраций предыдущих попыток',
    command: 'git',
    cwd: project.path,
    args: ['-c', hooksArg, '-C', project.path, 'worktree', 'prune', '--expire', 'now'],
  })
  for (const branch of existing.filter((value): value is string => Boolean(value))) {
    const backupRef = `refs/deploom/restart-backups/${Date.now()}/${branch}`
    commands.push({
      label: `Safety backup ????? ${branch} ????? restart`,
      command: 'git',
      cwd: project.path,
      args: ['-c', hooksArg, '-C', project.path, 'update-ref', backupRef, `refs/heads/${branch}`],
    })
    if (branch !== preservedBranch) {
      commands.push({
        label: `Удаление ветки ${branch} из предыдущей попытки`,
        command: 'git',
        cwd: project.path,
        args: ['-c', hooksArg, '-C', project.path, 'branch', '-D', branch],
      })
    }
  }
  return commands
}


async function cleanupToolManagedProjectWorktreesAfterRelease(job: JobRecord, project: ProjectSpec): Promise<void> {
  const tempPath = app.getPath('temp')
  const list = await spawnCapture('git', ['-C', project.path, 'worktree', 'list', '--porcelain'], project.path, 30_000)
  if (list.code !== 0) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Предупреждение: release создан, но не удалось прочитать git worktree для финальной уборки: ${list.stderr.trim() || `exit ${list.code}`}` })
    return
  }

  const paths = toolManagedWorktreePaths(liveGitWorktreeRecords(list.stdout), project.path, tempPath)
  if (!paths.length) {
    // Prune even when no live directory is present: an interrupted older run
    // may have left only administrative metadata under .git/worktrees.
    await spawnCapture('git', ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'worktree', 'prune', '--expire', 'now'], project.path, 30_000)
    return
  }

  for (const path of paths) {
    if (existsSync(path)) {
      const status = await spawnCapture('git', ['-C', path, 'status', '--porcelain=v1', '--untracked-files=all'], path, 30_000)
      if (status.code !== 0) {
        send('flow:job-output', {
          jobId: job.id,
          stream: 'system',
          line: `RELEASE_CLEANUP_STATUS_UNKNOWN: ????????? worktree ${path} ????????, ?????? ??? ?? ??????? ???????? ??? ???????: ${status.stderr.trim() || `exit ${status.code}`}`,
        })
        continue
      }
      const dirty = relevantGitStatus(status.stdout)
      if (dirty) {
        send('flow:job-output', {
          jobId: job.id,
          stream: 'system',
          line: `RELEASE_CLEANUP_DIRTY_WORKTREE: ${path} ???????? ??????????????? ????????? ? ?? ????? force-??????. ${dirty.slice(-1200)}`,
        })
        continue
      }
      const removed = await spawnCapture('git', ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'worktree', 'remove', '--force', path], project.path, 60_000)
      if (removed.code !== 0 && existsSync(path)) {
        // This directory is inside DepLoom's own temp root, therefore
        // it is safe to remove physically. `worktree prune` below reconciles
        // any stale Git administrative record left by an interrupted process.
        try { rmSync(path, { recursive: true, force: true }) } catch { /* re-check below */ }
      }
    }
  }

  await spawnCapture('git', ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'worktree', 'prune', '--expire', 'now'], project.path, 30_000)
  const after = await spawnCapture('git', ['-C', project.path, 'worktree', 'list', '--porcelain'], project.path, 30_000)
  const remaining = after.code === 0 ? toolManagedWorktreePaths(liveGitWorktreeRecords(after.stdout), project.path, tempPath) : paths.filter(existsSync)
  if (remaining.length) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Предупреждение: release-ветка создана, но Windows удерживает ${remaining.length} временных DepLoom worktree; следующий restart повторит уборку: ${remaining.join('; ')}` })
    return
  }
  send('flow:job-output', { jobId: job.id, stream: 'system', line: `Release cleanup: удалены временные DepLoom worktree (${paths.length}); git worktree prune выполнен.` })
}

// Drops any recorded agent session (legacy whole-migration shape from a
// pre-v0.1.39 install, or the current per-branch map) for this project so a
// fresh restart is actually fresh -- without this, an old on-disk session id
// would just linger, unused but never cleared.
function clearAgentSessionState(workspace: WorkspaceRecord, projectName: string): void {
  const state = readTeamState(workspace)
  const run = state?.projects[projectName]
  if (!state || !run || (!run.agentSession && !run.agentSessions && !run.activeAgentBranch)) return
  const { agentSession: _agentSession, agentSessions: _agentSessions, activeAgentBranch: _activeAgentBranch, ...rest } = run
  state.projects[projectName] = rest
  state.updatedAt = new Date().toISOString()
  const path = teamStatePath(workspace)
  atomicWriteJsonSync(path, state)
}

function forgetProjectPromptState(workspace: WorkspaceRecord, projectName: string, expectedPromptPath?: string): void {
  const state = loadState()
  const stored = state.workspaces.find((item) => item.id === workspace.id)
  if (!stored) return
  forgetScopedPromptPath(stored, projectName)
  forgetScopedPromptPath(workspace, projectName)
  // Pre-project-isolation state may have only the legacy scalar. The caller
  // already proved `expectedPromptPath` belongs to this project, so it is safe
  // to invalidate that compatibility pointer too.
  if (expectedPromptPath && stored.latestPromptPath === expectedPromptPath) stored.latestPromptPath = undefined
  if (expectedPromptPath && workspace.latestPromptPath === expectedPromptPath) workspace.latestPromptPath = undefined
  saveState(state)
}

async function cleanupSupersededMigrationAfterBaseline(job: JobRecord, project: ProjectSpec): Promise<void> {
  const oldPromptPath = promptPathForProject(job.workspace, project.name)
  let oldPlan: MigrationPlan | undefined
  if (oldPromptPath && existsSync(oldPromptPath)) {
    try { oldPlan = migrationPlanFromPrompt(readFileSync(oldPromptPath, 'utf8'), project.name) } catch { oldPlan = undefined }
  }

  // SourceSnapshot captured the live checkout bytes (dirty, untracked,
  // ignored, or detached). Cleanup must preserve that exact checkout instead
  // of switching branches after the proof subject has been chosen.
  for (const spec of await migrationBranchCleanupCommands(project, oldPlan, true)) {
    const result = await executeCommand(job, { ...spec, captureAgentSession: false })
    if (result.code !== 0) {
      throw new Error(`BASELINE_EXECUTION_RESET_FAILED: ${spec.label}: exit ${result.code}.${result.stderr.trim() ? `\n\n${result.stderr.trim()}` : ''}`)
    }
  }

  clearAgentSessionState(job.workspace, project.name)
  // A prompt belongs to one Baseline planning epoch. Keeping the old prompt
  // would make Step 3 look completed and could let the next Executor reuse an
  // immutable scope generated for the previous epoch. The file itself may be
  // user-chosen/downloaded, so forget the pointer but never delete the file.
  forgetProjectPromptState(job.workspace, project.name, oldPromptPath)
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: 'Новый Baseline принят: старые migration worktree/ветки и agent-session очищены. План предыдущего цикла забыт; FLOW остановлен на шаге «План обновления» до явного запуска миграции.',
  })
}


function clearAgentBranchSessionState(workspace: WorkspaceRecord, projectName: string, branchName: string): void {
  const state = readTeamState(workspace)
  const run = state?.projects[projectName]
  if (!state || !run) return
  const sessions = { ...(run.agentSessions ?? {}) }
  delete sessions[branchName]
  state.projects[projectName] = {
    ...run,
    ...(Object.keys(sessions).length ? { agentSessions: sessions } : {}),
    ...(!Object.keys(sessions).length ? { agentSessions: undefined } : {}),
    ...(run.activeAgentBranch === branchName ? { activeAgentBranch: undefined } : {}),
  }
  state.updatedAt = new Date().toISOString()
  const path = teamStatePath(workspace)
  atomicWriteJsonSync(path, state, (_key, value) => value === undefined ? undefined : value)
}

function completedAgentBatch(workspace: WorkspaceRecord, projectName: string, fingerprint: string): boolean {
  const run = readTeamState(workspace)?.projects[projectName]
  return run?.executionContractVersion === 2 && (run.completedAgentBatchFingerprints ?? []).includes(fingerprint)
}

function markAgentBatchCompleted(workspace: WorkspaceRecord, projectName: string, fingerprint: string): void {
  const state = readTeamState(workspace)
  const run = state?.projects[projectName]
  if (!state || !run || run.executionContractVersion !== 2) return
  const completed = [...new Set([...(run.completedAgentBatchFingerprints ?? []), fingerprint])].slice(-256)
  state.projects[projectName] = { ...run, completedAgentBatchFingerprints: completed }
  state.updatedAt = new Date().toISOString()
  const path = teamStatePath(workspace)
  atomicWriteJsonSync(path, state)
}

function markMigrationBranchCompleted(workspace: WorkspaceRecord, projectName: string, branchName: string): void {
  const state = readTeamState(workspace)
  const run = state?.projects[projectName]
  if (!state || !run || run.executionContractVersion !== 2) return
  const completed = [...new Set([...(run.completedMigrationBranches ?? []), branchName])]
  state.projects[projectName] = { ...run, completedMigrationBranches: completed }
  state.updatedAt = new Date().toISOString()
  const path = teamStatePath(workspace)
  atomicWriteJsonSync(path, state)
}

function promptRoadmapFactsPath(prompt: string): string | undefined {
  const match = /^Roadmap facts:\s*(.+)$/m.exec(prompt)
  return match?.[1]?.trim() || undefined
}

function promptScopeHash(prompt: string): string | undefined {
  const manifest = migrationScopeManifestFromPrompt(prompt)
  const value = manifest?.scopeHash
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function promptVersion(prompt: string): string {
  const manifest = migrationScopeManifestFromPrompt(prompt)
  const generatedAt = manifest?.generatedAt
  return typeof generatedAt === 'string' ? generatedAt : ''
}

function readProjectPackageJson(project: ProjectSpec): unknown {
  try { return JSON.parse(readFileSync(join(project.path, 'package.json'), 'utf8')) as unknown } catch { return undefined }
}

function projectPackageManager(project: ProjectSpec): 'yarn' | 'npm' | 'pnpm' {
  if (existsSync(join(project.path, 'yarn.lock'))) return 'yarn'
  if (existsSync(join(project.path, 'pnpm-lock.yaml'))) return 'pnpm'
  return 'npm'
}

function dependencyFilesForProject(project: ProjectSpec): string[] {
  return ['package.json', 'yarn.lock', 'pnpm-lock.yaml', 'package-lock.json'].filter((name) => existsSync(join(project.path, name)))
}

function porcelainTouchesDependencyFiles(status: string): boolean {
  return relevantGitStatusLines(status).some((line) => /(?:^|\s)(?:package\.json|yarn\.lock|pnpm-lock\.yaml|package-lock\.json)(?:$|\s)/.test(line))
}


async function dependencyRuntimeIdentity(project: ProjectSpec, manager: 'yarn' | 'npm' | 'pnpm'): Promise<{ nodeVersion: string; packageManagerVersion: string }> {
  const version = await spawnCapture(manager, ['--version'], project.path, 20_000)
  if (version.code !== 0 || !version.stdout.trim()) {
    throw new Error(`INFRA_PACKAGE_MANAGER_NOT_FOUND: ${manager} --version failed while building dependency materialization proof. ${version.stderr.trim()}`)
  }
  return { nodeVersion: process.version, packageManagerVersion: version.stdout.trim().split(/\r?\n/)[0] }
}

function compatibilityEvidencePath(workspace: WorkspaceRecord, projectName: string, branchName: string): string {
  const safe = (value: string) => value.replace(/[^a-zA-Z0-9._-]+/g, '-')
  return join(workspace.path, '.dependency-roadmap', 'state', 'compatibility-evidence', safe(projectName), `${new Date().toISOString().replace(/[:.]/g, '-')}-${safe(branchName)}.json`)
}

function captureCompatibilitySourceSnapshot(
  projectPath: string,
  destination: string,
): { sourceSnapshotLocator: string; sourceSnapshotKey: string; toolBuildId: string; projectRelative: string } {
  const pythonPath = join(bundledToolDir(), 'vendor')
  const env = commandEnvironment({
    ...process.env,
    FORCE_COLOR: '0',
    PYTHONUNBUFFERED: '1',
    PYTHONPATH: [pythonPath, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
  })
  const invocation = resolveSpawnInvocation('python', [
    join(bundledToolDir(), 'source_snapshot.py'),
    '--capture-durable', projectPath,
    '--destination', destination,
    '--timeout-seconds', '1800',
    '--json',
  ], { env })
  const result = spawnSync(invocation.command, invocation.args, {
    cwd: projectPath,
    shell: false,
    windowsHide: true,
    windowsVerbatimArguments: invocation.windowsVerbatimArguments,
    env,
    encoding: 'utf8',
    timeout: 1_800_000,
    maxBuffer: 16 * 1024 * 1024,
  })
  if (result.error || result.status !== 0) {
    const detail = String(result.stderr ?? '') + '\n' + String(result.stdout ?? '')
    const fallback = result.error?.message || detail.trim() || `exit ${result.status}`
    throw new Error(`DEPENDENCY_COMPATIBILITY_SOURCE_SNAPSHOT_FAILED: ${fallback}`)
  }
  const line = String(result.stdout ?? '').trim().split(/\r?\n/).at(-1)
  try {
    const parsed = JSON.parse(line || '') as Record<string, unknown>
    const locator = String(parsed.sourceSnapshotLocator ?? '')
    const key = String(parsed.sourceSnapshotKey ?? '')
    const toolBuildId = String(parsed.toolBuildId ?? '')
    const projectRelative = String(parsed.projectRelative ?? '.')
    if (!locator || !key || !/^[0-9a-f]{64}$/i.test(toolBuildId)) throw new Error('capture returned no locator/key/toolBuildId')
    return { sourceSnapshotLocator: locator, sourceSnapshotKey: key, toolBuildId, projectRelative }
  } catch (error) {
    throw new Error(`DEPENDENCY_COMPATIBILITY_SOURCE_SNAPSHOT_INVALID: ${String(error)}`)
  }
}
function writeDependencyCompatibilityEvidence(input: {
  workspace: WorkspaceRecord
  project: ProjectSpec
  canonicalProjectPath: string
  branch: string
  fullGroupPrompt: string
  targetMode: string
  commands: readonly string[]
  reason: string
  materializationProof?: string
  includePackages?: ReadonlySet<string>
}): string {
  const proofEnvelopeValidation = validateScopeProofEnvelope(
    input.fullGroupPrompt,
    input.project.name,
  )
  if (!proofEnvelopeValidation.ok || !proofEnvelopeValidation.envelope) {
    throw new Error(
      `DEPENDENCY_COMPATIBILITY_EVIDENCE_INVALID: ${input.branch}: ${proofEnvelopeValidation.reason}`,
    )
  }
  const actions = scopeActionsFromPrompt(input.fullGroupPrompt, input.project.name)
    .filter((item) => item.action === 'update' && item.current && item.target)
    .filter((item) => !input.includePackages || input.includePackages.has(item.package))
    .map((item) => ({ package: item.package, current: item.current, target: item.target, action: item.action }))
  if (!actions.length) {
    throw new Error(`DEPENDENCY_COMPATIBILITY_EVIDENCE_INVALID: ${input.branch}: immutable scope contains no update actions with current/target versions.`)
  }
  if (!input.commands.length) {
    throw new Error(`DEPENDENCY_COMPATIBILITY_EVIDENCE_INVALID: ${input.branch}: no deterministic verification commands are available for evidence reproduction.`)
  }
  const path = compatibilityEvidencePath(input.workspace, input.project.name, input.branch)
  mkdirSync(dirname(path), { recursive: true })
  const snapshotDestination = path.replace(/\.json$/i, '.source-snapshot')
  const snapshot = captureCompatibilitySourceSnapshot(
    input.project.path,
    snapshotDestination,
  )
  const payload = {
    schemaVersion: 2,
    project: input.project.name,
    projectPath: input.canonicalProjectPath,
    branchRef: input.branch,
    sourceSnapshotLocator: snapshot.sourceSnapshotLocator,
    sourceSnapshotKey: snapshot.sourceSnapshotKey,
    toolBuildId: snapshot.toolBuildId,
    projectRelative: snapshot.projectRelative,
    targetMode: input.targetMode,
    commands: [...input.commands],
    actions,
    reason: input.reason.slice(0, 6000),
    materializationProof: input.materializationProof ?? '',
    proofEnvelope: proofEnvelopeValidation.envelope,
    createdAt: new Date().toISOString(),
  }
  const temporary = `${path}.tmp`
  try {
    writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
    renameSync(temporary, path)
    return path
  } catch (error) {
    try { rmSync(snapshot.sourceSnapshotLocator, { recursive: true, force: true }) } catch { /* best effort */ }
    throw error
  }
}
function compatibilityEvidencePathFromFailure(failure: string): string | undefined {
  const match = /DEPENDENCY_COMPATIBILITY_EVIDENCE:[^\n]*evidenceFile=([^;\s]+)/.exec(failure)
  if (!match) return undefined
  try { return decodeURIComponent(match[1]) } catch { return undefined }
}

async function restoreImmutableDependencyState(
  job: JobRecord,
  project: ProjectSpec,
  branchName: string,
  proof: DependencyMaterializationProof,
): Promise<void> {
  const packagePath = join(project.path, 'package.json')
  const raw = JSON.parse(readFileSync(packagePath, 'utf8')) as Record<string, unknown>
  for (const section of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
    if (proof.dependencySections[section]) raw[section] = { ...proof.dependencySections[section] }
    else delete raw[section]
  }
  for (const key of DEPENDENCY_CONTROL_KEYS) {
    if (Object.prototype.hasOwnProperty.call(proof.dependencyControlFields, key)) {
      raw[key] = structuredClone(proof.dependencyControlFields[key])
    } else {
      delete raw[key]
    }
  }
  writeFileSync(packagePath, `${JSON.stringify(raw, null, 2)}\n`, 'utf8')
  const lockfilesToRestore = new Set([
    ...Object.keys(proof.lockfiles),
    proof.provenResolvedLockfilePath,
  ].filter(Boolean))
  for (const lockfile of lockfilesToRestore) {
    const restored = await spawnCapture('git', ['-C', project.path, 'checkout', proof.gitHead, '--', lockfile], project.path, 30_000)
    if (restored.code !== 0) {
      throw new Error(`AGENT_DEPENDENCY_STATE_MUTATION: ${branchName}: immutable ${lockfile} changed and Control Plane could not restore it from materialization commit ${proof.gitHead}.`)
    }
  }
  const dependencyFiles = [...new Set([
    ...dependencyFilesForProject(project),
    proof.provenResolvedLockfilePath,
  ].filter(Boolean))]
  const add = await spawnCapture('git', ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'add', '--', ...dependencyFiles], project.path, 30_000)
  if (add.code !== 0) throw new Error(`AGENT_DEPENDENCY_STATE_MUTATION: ${branchName}: failed to stage deterministic dependency-state restoration.`)
  const staged = await spawnCapture('git', ['-C', project.path, 'diff', '--cached', '--quiet', '--', ...dependencyFiles], project.path, 20_000)
  if (staged.code === 1) {
    const commit = await executeCommand(job, {
      label: `Restore immutable dependency assignment ${branchName}`,
      command: 'git',
      args: ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'commit', '-m', `chore(deps): restore immutable assignment ${branchName}`],
      cwd: project.path,
      captureAgentSession: false,
    })
    if (commit.code !== 0) throw new Error(`AGENT_DEPENDENCY_STATE_MUTATION: ${branchName}: dependency state restored in files but restoration commit failed.`)
  } else if (staged.code !== 0) {
    throw new Error(`AGENT_DEPENDENCY_STATE_MUTATION: ${branchName}: could not verify restored dependency diff.`)
  }
  send('flow:job-output', {
    jobId: job.id, stream: 'system',
    line: `${branchName}: Executor attempted to mutate immutable dependency state; Control Plane restored package dependency sections/lockfile from materialization proof without touching source/config edits.`,
  })
}

async function enforceMaterializationProofAfterAgent(
  job: JobRecord,
  project: ProjectSpec,
  branchName: string,
  proofPath: string | undefined,
): Promise<void> {
  if (!proofPath) return
  const proof = readMaterializationProof(proofPath)
  if (!proof) throw new Error(`DEPENDENCY_MATERIALIZATION_PROOF_MISSING: ${branchName}: ${proofPath}`)
  const runtime = await dependencyRuntimeIdentity(project, proof.packageManager as 'yarn' | 'npm' | 'pnpm')
  let validation = validateMaterializationProof({
    projectPath: project.path,
    proof,
    project: project.name,
    branch: branchName,
    actions: proof.actions,
    packageManager: proof.packageManager,
    packageManagerVersion: runtime.packageManagerVersion,
    nodeVersion: runtime.nodeVersion,
  })
  if (validation.ok) return
  await restoreImmutableDependencyState(job, project, branchName, proof)
  validation = validateMaterializationProof({
    projectPath: project.path,
    proof,
    project: project.name,
    branch: branchName,
    actions: proof.actions,
    packageManager: proof.packageManager,
    packageManagerVersion: runtime.packageManagerVersion,
    nodeVersion: runtime.nodeVersion,
  })
  if (!validation.ok) throw new Error(`AGENT_DEPENDENCY_STATE_MUTATION: ${branchName}: deterministic restoration did not re-establish materialization proof (${validation.reason}).`)
}

async function materializeApprovedDependencyAssignment(
  job: JobRecord,
  project: ProjectSpec,
  fullGroupPrompt: string,
  branchName: string,
): Promise<{ safeToSplitSemanticBatches: boolean; changedPackages: string[]; proofPath?: string }> {
  const actions = scopeActionsFromPrompt(fullGroupPrompt, project.name)
  if (!actions.length) return { safeToSplitSemanticBatches: false, changedPackages: [] }

  const proofEnvelopeValidation = validateScopeProofEnvelope(fullGroupPrompt, project.name)
  if (!proofEnvelopeValidation.ok || !proofEnvelopeValidation.envelope) {
    throw new Error(
      `MIGRATION_REPLAN_REQUIRED: PROVEN_DEPENDENCY_PROOF_INVALID: ${branchName}: ${proofEnvelopeValidation.reason}. `
      + 'Dashboard/prompt no longer carries the exact Baseline proof identity; Executor is not allowed to guess dependency state.',
    )
  }
  const proofEnvelope = proofEnvelopeValidation.envelope
  let provenResolvedState: ReturnType<typeof loadProvenResolvedState>
  try {
    provenResolvedState = loadProvenResolvedState(job.workspace.path, proofEnvelope)
  } catch (error) {
    throw new Error(
      `MIGRATION_REPLAN_REQUIRED: PROVEN_RESOLVED_STATE_INVALID: ${branchName}: ${error instanceof Error ? error.message : String(error)}. `
      + 'Baseline resolver proof no longer carries the exact package-manager state that ProjectProof verified.',
    )
  }

  let packageText: string
  let packageJson: unknown
  try {
    packageText = readFileSync(join(project.path, 'package.json'), 'utf8')
    packageJson = JSON.parse(packageText) as unknown
  } catch (error) {
    throw new Error(`DEPENDENCY_MATERIALIZATION_SCOPE_INVALID: ${branchName}: package.json не читается: ${error instanceof Error ? error.message : String(error)}`)
  }

  const manager = projectPackageManager(project)
  const runtime = await dependencyRuntimeIdentity(project, manager)
  if (provenResolvedState.manager !== manager) {
    throw new Error(`MIGRATION_REPLAN_REQUIRED: PROVEN_RESOLVED_STATE_MANAGER_DRIFT: ${branchName}: baseline=${provenResolvedState.manager}, current=${manager}.`)
  }
  const gitRootResult = await spawnCapture('git', ['-C', project.path, 'rev-parse', '--show-toplevel'], project.path, 20_000)
  if (gitRootResult.code !== 0 || !gitRootResult.stdout.trim()) {
    throw new Error(`PROVEN_RESOLVED_STATE_GIT_ROOT_UNREADABLE: ${branchName}`)
  }
  const gitRoot = gitRootResult.stdout.trim()
  const resolvedLockfileAbsolute = resolvedStateTargetPath(gitRoot, provenResolvedState)
  const resolvedLockfileProjectRelative = relative(project.path, resolvedLockfileAbsolute).replace(/\\/g, '/')
  const proofPath = materializationProofPath(job.workspace.path, project.name, branchName)
  const satisfiedBefore = satisfiedScopePackagesFromPrompt(fullGroupPrompt, project.name, packageJson) ?? new Set<string>()
  const allTargetsPresent = actions.every((action) => satisfiedBefore.has(action.package))
  const statusBefore = await spawnCapture('git', ['-C', project.path, 'status', '--porcelain=v1', '--untracked-files=all'], project.path, 20_000)
  if (statusBefore.code !== 0) throw new Error(`DEPENDENCY_MATERIALIZATION_GIT_STATE_FAILED: ${branchName}: ${statusBefore.stderr.trim() || 'git status failed'}`)
  const dependencyFilesDirty = porcelainTouchesDependencyFiles(statusBefore.stdout)

  if (dependencyFilesDirty) {
    // Recovery is not allowed to silently downgrade the v2 ownership boundary.
    // Preserve the branch exactly as-is and require deterministic reconciliation
    // instead of handing a dirty package/lockfile back to Executor.
    throw new Error(
      `MIGRATION_REPLAN_REQUIRED: DEPENDENCY_MATERIALIZATION_RECONCILE_REQUIRED: ${branchName}: package.json/lockfile already contains uncommitted dependency edits before Control Plane materialization. `
      + 'The worktree was preserved; Executor will not inherit dependency ownership. Restart/reconcile this branch from the approved assignment.',
    )
  }

  if (allTargetsPresent) {
    const existingProof = readMaterializationProof(proofPath)
    const existingValidation = validateMaterializationProof({
      projectPath: project.path,
      proof: existingProof,
      project: project.name,
      branch: branchName,
      actions,
      packageManager: manager,
      packageManagerVersion: runtime.packageManagerVersion,
      nodeVersion: runtime.nodeVersion,
      provenEnvelopeKey: proofEnvelope.envelopeKey,
      provenAssignmentKey: proofEnvelope.assignmentKey,
      resolverInputKey: proofEnvelope.resolverInputKey,
      fixedResolverInputsKey: proofEnvelope.fixedResolverInputsKey,
      sourceSnapshotKey: proofEnvelope.sourceSnapshotKey,
      projectProofKey: proofEnvelope.projectProofKey,
    provenExactDirectAssignment: proofEnvelope.exactDirectAssignment,
    provenRemovals: proofEnvelope.removals,
    provenObservedResolvedHash: proofEnvelope.observedResolvedHash,
    provenResolvedStateKey: provenResolvedState.key,
    provenResolvedLockfilePath: resolvedLockfileProjectRelative,
    provenResolvedLockfileHash: provenResolvedState.lockfileHash,
    })
    if (existingValidation.ok) {
      send('flow:job-output', {
        jobId: job.id,
        stream: 'system',
        line: `${branchName}: immutable dependency assignment подтверждён materialization proof; semantic Executor batches можно дробить независимо от compatibility cohort.`,
      })
      return { safeToSplitSemanticBatches: true, changedPackages: [], proofPath }
    }
    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: `${branchName}: targets уже записаны, но materialization proof отсутствует/устарел (${existingValidation.reason}). Повторно доказываю lockfile/resolved state детерминированным install до LLM.`,
    })
  }

  const nonDependencyDirty = relevantGitStatus(statusBefore.stdout)
  if (nonDependencyDirty) {
    throw new Error(
      `MIGRATION_REPLAN_REQUIRED: PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_DIRTY: ${branchName}: `
      + `source/config changed before Control Plane materialization: ${nonDependencyDirty.split('\n').slice(0, 8).join('; ')}. `
      + 'A fresh Baseline ProofEnvelope is required.',
    )
  }
  const sourceHead = await spawnCapture('git', ['-C', project.path, 'rev-parse', 'HEAD'], project.path, 20_000)
  if (sourceHead.code !== 0 || !sourceHead.stdout.trim()) {
    throw new Error(`PROVEN_DEPENDENCY_SOURCE_UNREADABLE: ${branchName}: cannot read pre-materialization HEAD.`)
  }
  if (sourceHead.stdout.trim() !== proofEnvelope.sourceHead) {
    throw new Error(
      `MIGRATION_REPLAN_REQUIRED: PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_DRIFT: ${branchName}: `
      + `Baseline source=${proofEnvelope.sourceHead.slice(0, 12)}, current=${sourceHead.stdout.trim().slice(0, 12)}. `
      + 'A fresh ProofEnvelope is required before dependency materialization.',
    )
  }

  try {
    restoreProvenResolvedStateLockfile(gitRoot, provenResolvedState)
  } catch (error) {
    throw new Error(
      `MIGRATION_REPLAN_REQUIRED: PROVEN_RESOLVED_STATE_RESTORE_FAILED: ${branchName}: ${error instanceof Error ? error.message : String(error)}.`,
    )
  }

  const applied = applyDependencyActionsToPackageJson(packageText, actions)
  if (applied.changedPackages.length) writeFileSync(join(project.path, 'package.json'), applied.text, 'utf8')

  const install = dependencyMaterializationInstallSpec(manager)
  setBranchRuntime(job, branchName, 'bootstrapping', `Control plane materializes ${actions.length} dependency target(s)`)
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: `${branchName}: до запуска LLM materialize-ю и доказываю весь immutable dependency assignment (${actions.length} целей), manifest + lockfile одним ${manager} install. Executor версии не выбирает.`,
  })
  const installResult = await executeCommand(job, {
    label: `Materialize dependency assignment ${branchName}`,
    command: install.command,
    args: install.args,
    cwd: project.path,
    captureAgentSession: false,
  })
  if (installResult.code !== 0) {
    const output = `${installResult.stderr}\n${installResult.stdout}`
    const kind = classifyDependencyMaterializationFailure(output)
    if (kind === 'infrastructure') {
      throw new Error(`INFRA_PACKAGE_MATERIALIZATION_FAILED: ${branchName}: ${manager} install не смог materialize утверждённый assignment из-за инфраструктуры. Targets не перепланируются. ${output.trim().slice(-2400)}`)
    }
    if (kind === 'dependency') {
      throw new Error(`MIGRATION_REPLAN_REQUIRED: DEPENDENCY_MATERIALIZATION_CONFLICT: ${branchName}: real ${manager} install отклонил immutable assignment до Executor. Deterministic Baseline/Verifier должен пересчитать assignment; LLM repair запрещён. ${output.trim().slice(-2400)}`)
    }
    throw new Error(`DEPENDENCY_MATERIALIZATION_FAILED: ${branchName}: ${manager} install завершился неизвестной ошибкой; hard solver constraint не обучается без классификации. ${output.trim().slice(-2400)}`)
  }

  try {
    verifyProvenResolvedStateLockfile(gitRoot, provenResolvedState)
  } catch (error) {
    throw new Error(
      `MIGRATION_REPLAN_REQUIRED: PROVEN_RESOLVED_STATE_MATERIALIZATION_DRIFT: ${branchName}: ${error instanceof Error ? error.message : String(error)}. `
      + 'Frozen materialization did not preserve the exact lockfile verified by Baseline.',
    )
  }

  let materializedJson: unknown
  try { materializedJson = JSON.parse(readFileSync(join(project.path, 'package.json'), 'utf8')) as unknown } catch { materializedJson = undefined }
  const satisfiedAfter = satisfiedScopePackagesFromPrompt(fullGroupPrompt, project.name, materializedJson) ?? new Set<string>()
  const missing = actions.filter((action) => !satisfiedAfter.has(action.package)).map((action) => action.package)
  if (missing.length) {
    throw new Error(`MIGRATION_REPLAN_REQUIRED: DEPENDENCY_MATERIALIZATION_POSTCONDITION: ${branchName}: control-plane install завершился, но package.json не содержит immutable target(s): ${missing.join(', ')}.`)
  }

  const dependencyFiles = [...new Set([
    ...dependencyFilesForProject(project),
    resolvedLockfileProjectRelative,
  ])]
  const add = await executeCommand(job, {
    label: `Stage immutable dependency assignment ${branchName}`,
    command: 'git',
    args: ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'add', '--', ...dependencyFiles],
    cwd: project.path,
    captureAgentSession: false,
  })
  if (add.code !== 0) throw new Error(`DEPENDENCY_MATERIALIZATION_GIT_STATE_FAILED: ${branchName}: не удалось stage package/lockfile.`)
  const staged = await spawnCapture('git', ['-C', project.path, 'diff', '--cached', '--quiet', '--', ...dependencyFiles], project.path, 20_000)
  if (staged.code === 1) {
    const commit = await executeCommand(job, {
      label: `Commit immutable dependency assignment ${branchName}`,
      command: 'git',
      args: ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'commit', '-m', `chore(deps): materialize ${branchName}`],
      cwd: project.path,
      captureAgentSession: false,
    })
    if (commit.code !== 0) throw new Error(`DEPENDENCY_MATERIALIZATION_GIT_STATE_FAILED: ${branchName}: package assignment установлен, но control plane не смог создать intermediate commit.`)
  } else if (staged.code !== 0) {
    throw new Error(`DEPENDENCY_MATERIALIZATION_GIT_STATE_FAILED: ${branchName}: не удалось проверить staged dependency diff.`)
  }

  const head = await spawnCapture('git', ['-C', project.path, 'rev-parse', 'HEAD'], project.path, 20_000)
  if (head.code !== 0 || !head.stdout.trim()) throw new Error(`DEPENDENCY_MATERIALIZATION_GIT_STATE_FAILED: ${branchName}: cannot read materialization commit.`)
  const proof = createMaterializationProof({
    projectPath: project.path,
    project: project.name,
    branch: branchName,
    actions,
    provenEnvelopeKey: proofEnvelope.envelopeKey,
    provenAssignmentKey: proofEnvelope.assignmentKey,
    resolverInputKey: proofEnvelope.resolverInputKey,
    fixedResolverInputsKey: proofEnvelope.fixedResolverInputsKey,
    sourceSnapshotKey: proofEnvelope.sourceSnapshotKey,
    projectProofKey: proofEnvelope.projectProofKey,
    provenExactDirectAssignment: proofEnvelope.exactDirectAssignment,
    provenRemovals: proofEnvelope.removals,
    provenObservedResolvedHash: proofEnvelope.observedResolvedHash,
    provenResolvedStateKey: provenResolvedState.key,
    provenResolvedLockfilePath: resolvedLockfileProjectRelative,
    provenResolvedLockfileHash: provenResolvedState.lockfileHash,
    packageManager: manager,
    packageManagerVersion: runtime.packageManagerVersion,
    nodeVersion: runtime.nodeVersion,
    gitHead: head.stdout.trim(),
  })
  writeMaterializationProof(proofPath, proof)
  const proofValidation = validateMaterializationProof({
    projectPath: project.path,
    proof,
    project: project.name,
    branch: branchName,
    actions,
    packageManager: manager,
    packageManagerVersion: runtime.packageManagerVersion,
    nodeVersion: runtime.nodeVersion,
    provenEnvelopeKey: proofEnvelope.envelopeKey,
    provenAssignmentKey: proofEnvelope.assignmentKey,
    resolverInputKey: proofEnvelope.resolverInputKey,
    fixedResolverInputsKey: proofEnvelope.fixedResolverInputsKey,
    sourceSnapshotKey: proofEnvelope.sourceSnapshotKey,
    projectProofKey: proofEnvelope.projectProofKey,
    provenExactDirectAssignment: proofEnvelope.exactDirectAssignment,
    provenRemovals: proofEnvelope.removals,
    provenObservedResolvedHash: proofEnvelope.observedResolvedHash,
    provenResolvedStateKey: provenResolvedState.key,
    provenResolvedLockfilePath: resolvedLockfileProjectRelative,
    provenResolvedLockfileHash: provenResolvedState.lockfileHash,
  })
  if (!proofValidation.ok) throw new Error(`DEPENDENCY_MATERIALIZATION_PROOF_INVALID: ${branchName}: ${proofValidation.reason}`)
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: `${branchName}: materialization proof записан (${proof.assignmentHash}); ResolvedState=${provenResolvedState.key.slice(0, 12)}, exact frozen lockfile + Node ${proof.nodeVersion} + ${manager} ${proof.packageManagerVersion}.`,
  })
  return { safeToSplitSemanticBatches: true, changedPackages: applied.changedPackages, proofPath }
}

async function ensureParallelWorkerEnvironment(job: JobRecord, project: ProjectSpec, canonicalProjectPath: string, branch: string): Promise<void> {
  if (portablePathKey(project.path) === portablePathKey(canonicalProjectPath) || existsSync(join(project.path, 'node_modules', '.bin'))) return
  const manager = projectPackageManager(project)
  const install = manager === 'yarn'
    ? { command: 'yarn', args: ['install', '--frozen-lockfile'] }
    : manager === 'pnpm'
      ? { command: 'pnpm', args: ['install', '--frozen-lockfile'] }
      : existsSync(join(project.path, 'package-lock.json'))
        ? { command: 'npm', args: ['ci'] }
        : { command: 'npm', args: ['install'] }
  setBranchRuntime(job, branch, 'bootstrapping', `Установка зависимостей (${manager})`)
  send('flow:job-output', { jobId: job.id, stream: 'system', line: `${branch}: во временном worktree нет локальных CLI-инструментов. Подготавливаю зависимости до verification; это инфраструктурный шаг, а не agent repair.` })
  let result: { code: number; stderr: string; stdout: string }
  try {
    result = await executeCommand(job, {
      label: `Подготовка окружения ${branch}`,
      command: install.command,
      args: install.args,
      cwd: project.path,
      captureAgentSession: false,
    })
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    throw new Error(`PARALLEL_WORKER_BOOTSTRAP_FAILED: ${branch}: ${message}. Это инфраструктурная ошибка; package targets и Baseline plan не изменены.`)
  }
  if (result.code !== 0 || !existsSync(join(project.path, 'node_modules', '.bin'))) {
    throw new Error(`PARALLEL_WORKER_BOOTSTRAP_FAILED: ${branch}: ${manager} install не подготовил локальные инструменты; verification и repair не запускались.${result.stderr.trim() ? `\n\n${result.stderr.trim()}` : ''}`)
  }
  setBranchRuntime(job, branch, 'verifying', 'Окружение готово, запускаю проверки')
}

function shellCommandSpec(label: string, commandText: string, cwd: string): CommandSpec {
  return process.platform === 'win32'
    ? { label, command: 'cmd', args: ['/d', '/s', '/c', commandText], cwd, captureAgentSession: false }
    : { label, command: 'sh', args: ['-lc', commandText], cwd, captureAgentSession: false }
}

type VerificationRun = { ok: boolean; feedback: string; failures: Array<{ command: string; code: number }> }

async function runProjectVerification(job: JobRecord, project: ProjectSpec, commands: readonly string[], labelPrefix: string): Promise<VerificationRun> {
  const failures: Array<{ command: string; code: number }> = []
  const feedback: string[] = []
  const cleaned = cleanEphemeralVerificationCaches(project.path)
  if (cleaned.length) {
    send('flow:job-output', {
      jobId: job.id, stream: 'system',
      line: `${labelPrefix}: deterministic cleanup удалил generated cache: ${cleaned.join(', ')}`,
    })
  }
  for (const commandText of commands) {
    const result = await executeCommand(job, shellCommandSpec(`${labelPrefix}: ${commandText}`, commandText, project.path))
    const tail = `${result.stderr}\n${result.stdout}`.trim().slice(-2200)
    if (result.code !== 0) failures.push({ command: commandText, code: result.code })
    feedback.push(`${commandText}: exit ${result.code}${tail ? `\n${tail}` : ''}`)
  }
  return { ok: failures.length === 0, failures, feedback: feedback.join('\n\n').slice(-9000) }
}

function checkpointAssessment(path: string | undefined, plan: MigrationPlan) {
  if (!path || !existsSync(path)) return assessMigrationCheckpoint(undefined, plan.branches.map((entry) => entry.branch))
  try {
    return assessMigrationCheckpoint(JSON.parse(readFileSync(path, 'utf8')) as unknown, plan.branches.map((entry) => entry.branch))
  } catch {
    return assessMigrationCheckpoint(undefined, plan.branches.map((entry) => entry.branch))
  }
}

function allowKnownBaselineFailures(run: VerificationRun, assessment: MigrationVerificationAssessment): VerificationRun {
  if (run.ok || !assessment.evidence.length) return run
  const baselineFailed = new Set(
    assessment.evidence
      .filter((item) => item.baselineExit !== undefined && item.baselineExit !== 0)
      .map((item) => verificationCommandKey(item.command)),
  )
  const newFailures = run.failures.filter((item) => !baselineFailed.has(verificationCommandKey(item.command)))
  if (newFailures.length === run.failures.length) return run
  const tolerated = run.failures.filter((item) => baselineFailed.has(verificationCommandKey(item.command)))
  return {
    ok: newFailures.length === 0,
    failures: newFailures,
    feedback: `${run.feedback}\n\nBaseline note: pre-existing non-zero checks are not treated as dependency regressions here: ${tolerated.map((item) => `${item.command}=exit${item.code}`).join(', ')}. They may still block the real release hook and should remain documented.`,
  }
}

function materializeBatchRoadmap(prompt: string, projectName: string, packages: readonly string[], promptDir: string, stem: string): string {
  const originalPath = promptRoadmapFactsPath(prompt)
  if (!originalPath || !existsSync(originalPath)) return prompt
  try {
    const roadmap = JSON.parse(readFileSync(originalPath, 'utf8')) as unknown
    const filtered = batchRoadmapDocument(roadmap, projectName, packages)
    const batchPath = join(promptDir, `${stem}-roadmap.json`)
    writeFileSync(batchPath, `${JSON.stringify(filtered, null, 2)}\n`, 'utf8')
    return replaceRoadmapPath(prompt, originalPath, batchPath)
  } catch {
    // Keep the canonical full roadmap path on any extraction problem. This is
    // a token optimization only; inability to create the smaller view must
    // never remove release intelligence or block a migration.
    return prompt
  }
}

async function batchExecutionState(job: JobRecord, project: ProjectSpec, branchName: string, batchPrompt: string, packages: readonly string[]): Promise<{ ready: boolean; remaining: string[]; feedback: string }> {
  let packageJson: unknown
  try { packageJson = JSON.parse(readFileSync(join(project.path, 'package.json'), 'utf8')) as unknown } catch { packageJson = undefined }
  const satisfied = satisfiedScopePackagesFromPrompt(batchPrompt, project.name, packageJson) ?? new Set<string>()
  const remaining = packages.filter((packageName) => !satisfied.has(packageName))
  const branch = await spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000)
  const status = await spawnCapture('git', ['-C', project.path, 'status', '--porcelain'], project.path, 15_000)
  const onBranch = branch.code === 0 && branch.stdout.trim() === branchName
  const clean = status.code === 0 && !relevantGitStatus(status.stdout)
  const ready = remaining.length === 0 && onBranch && clean
  const details = [
    remaining.length ? `не достигнуты package targets: ${remaining.join(', ')}` : 'package targets batch достигнуты',
    onBranch ? `HEAD на ${branchName}` : `HEAD на ${branch.stdout.trim() || 'неизвестной ветке'}, ожидалась ${branchName}`,
    clean ? 'рабочее дерево чистое' : 'в рабочем дереве остались незакоммиченные изменения',
  ]
  return { ready, remaining, feedback: details.join('; ') }
}

async function baselineStartCommands(input: ActionInput, project: ProjectSpec): Promise<CommandSpec[]> {
  if (input.action !== 'baseline') return []
  void project
  // SOURCE_SNAPSHOT_BASELINE_LIVE_CHECKOUT: Git is provenance, not source
  // authority. The generator seals the current filesystem subject, including
  // dirty, untracked and ignored bytes, without stash or branch switching.
  return []
}

async function cleanAgentStartCommands(input: ActionInput, workspace: WorkspaceRecord, project: ProjectSpec): Promise<CommandSpec[]> {
  if (input.action !== 'agent' || input.resumeAgent) return []
  const status = await spawnCapture('git', ['-C', project.path, 'status', '--porcelain'], project.path)
  if (status.code !== 0) throw new Error(status.stderr.trim() || 'Не удалось проверить изменения проекта перед запуском агента.')
  const commands: CommandSpec[] = []
  if (relevantGitStatus(status.stdout)) {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    commands.push({
      label: 'Safety stash перед запуском агента',
      command: 'git',
      cwd: project.path,
      args: ['-C', project.path, 'stash', 'push', '--message', `dependency-flow-before-agent-${stamp}`, '--', '.', ...workspaceNoiseGitExcludePathspecs()],
    })
  }
  // Everything below is destructive (deletes branches, forgets the saved
  // session) and must stay opt-in to an explicit "Начать заново" click.
  // `runMigrationAgentLoop` already re-reads live git facts on every entry,
  // so simply *not* wiping anything is enough for it to correctly resume a
  // plan that's mid-flight for a reason that isn't an interrupted agent CLI
  // session -- e.g. the orchestrator's own merge step stopped on a real
  // conflict, the user resolved and committed it by hand, and clicked the
  // default (non-"Продолжить агента") button because there was no CLI
  // session left to resume. Wiping branches here previously discarded that
  // already-merged work outright.
  if (!input.restartMigration) return commands
  commands.push({
    label: 'Переход на исходную ветку перед новой миграцией',
    command: 'git',
    cwd: project.path,
    args: ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'switch', project.git?.sourceBranch || 'master'],
  })
  const promptPath = input.promptPath || promptPathForProject(workspace, project.name)
  const plan = promptPath && existsSync(promptPath) ? migrationPlanFromPrompt(readFileSync(promptPath, 'utf8'), project.name) : undefined
  commands.push(...await migrationBranchCleanupCommands(project, plan))
  clearAgentSessionState(workspace, project.name)
  return commands
}

async function ensureMigrationBaseBranch(
  job: JobRecord,
  project: ProjectSpec,
  plan: MigrationPlan,
  savedPromptMarkdown: string,
): Promise<void> {
  const baseBranch = String(plan.baseBranch || '').trim()
  if (!baseBranch) throw new Error('MIGRATION_BASE_REF_MISSING: Branch plan has no baseBranch.')

  const local = await spawnCapture(
    'git',
    ['-C', project.path, 'show-ref', '--verify', '--quiet', `refs/heads/${baseBranch}`],
    project.path,
    15_000,
  )
  if (local.code === 0) return

  // If the configured base exists remotely, materialize exactly that ref
  // locally. This preserves an explicitly maintained base branch instead of
  // silently rebasing it onto another checkout.
  const remoteRef = `origin/${baseBranch}`
  const remote = await spawnCapture(
    'git',
    ['-C', project.path, 'show-ref', '--verify', '--quiet', `refs/remotes/${remoteRef}`],
    project.path,
    15_000,
  )
  if (remote.code === 0) {
    const created = await executeCommand(job, {
      label: `Восстановление локальной base-ветки ${baseBranch} из ${remoteRef}`,
      command: 'git',
      cwd: project.path,
      args: ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'branch', '--no-track', baseBranch, remoteRef],
      captureAgentSession: false,
    })
    if (created.code !== 0) {
      throw new Error(`MIGRATION_BASE_REF_CREATE_FAILED: ${baseBranch} <- ${remoteRef}: ${created.stderr.trim()}`)
    }
    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: `Base ref ${baseBranch} отсутствовал локально; восстановлен из существующего ${remoteRef}.`,
    })
    return
  }

  // Default `libs`/custom prefixes are allowed to be logical branch names,
  // not pre-existing refs. Bind a newly created base to the exact provenance
  // commit carried by the reviewed ProofEnvelope, never to an arbitrary live
  // HEAD/source branch that may have moved since Baseline.
  const proof = validateScopeProofEnvelope(savedPromptMarkdown, project.name)
  const sourceHead = proof.ok ? String(proof.envelope?.sourceHead || '').trim() : ''
  if (!sourceHead) {
    throw new Error(
      `MIGRATION_BASE_REF_MISSING: ${baseBranch} does not exist locally or as ${remoteRef}; ` +
      `the current reviewed prompt cannot provide a proven sourceHead (${proof.reason}).`,
    )
  }
  const sourceCommit = await spawnCapture(
    'git',
    ['-C', project.path, 'rev-parse', '--verify', `${sourceHead}^{commit}`],
    project.path,
    15_000,
  )
  if (sourceCommit.code !== 0) {
    throw new Error(
      `MIGRATION_BASE_SOURCE_COMMIT_MISSING: ${baseBranch} requires proven sourceHead ${sourceHead}, ` +
      `but that commit is not available in the local repository.`,
    )
  }
  const created = await executeCommand(job, {
    label: `Создание base-ветки ${baseBranch} от proven source ${sourceHead.slice(0, 12)}`,
    command: 'git',
    cwd: project.path,
    args: ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'branch', baseBranch, sourceHead],
    captureAgentSession: false,
  })
  if (created.code !== 0) {
    throw new Error(`MIGRATION_BASE_REF_CREATE_FAILED: ${baseBranch} <- ${sourceHead}: ${created.stderr.trim()}`)
  }
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: `Base ref ${baseBranch} ещё не существовал. Создал его детерминированно от proven sourceHead ${sourceHead.slice(0, 12)}; текущий HEAD для выбора базы не использовался.`,
  })
}

// The orchestrator, not the agent, owns branch creation and merge under the
// per-branch-group loop -- this is the same `-c core.hooksPath=` bypass
// cleanAgentStartCommands already uses for its own lifecycle git calls, not a
// new mechanism.
async function ensureGroupBranchCheckedOut(job: JobRecord, project: ProjectSpec, plan: MigrationPlan, branch: MigrationBranchProgress): Promise<void> {
  if (branch.checkedOut) return
  const hooksArg = `core.hooksPath=${emptyHooksPath()}`
  const exists = branch.status !== 'waiting'
  const spec: CommandSpec = exists
    ? { label: `Переход на ветку ${branch.branch}`, command: 'git', cwd: project.path, args: ['-c', hooksArg, '-C', project.path, 'switch', branch.branch] }
    : { label: `Создание ветки ${branch.branch} от ${plan.baseBranch}`, command: 'git', cwd: project.path, args: ['-c', hooksArg, '-C', project.path, 'switch', '-c', branch.branch, plan.baseBranch] }
  let result = await executeCommand(job, spec)
  if (result.code !== 0 && /already used by worktree/i.test(result.stderr)) {
    send('flow:job-output', { jobId: job.id, stream: 'system', source: { kind: 'group', id: branch.branch, label: branch.label || branch.branch }, line: `${branch.branch}: Git хранит исчезнувший tool-managed worktree. Автоматически очищаю prunable registration и повторяю переход; commits/evidence ветки сохраняются.` })
    await spawnCapture('git', ['-C', project.path, 'worktree', 'prune', '--expire', 'now'], project.path, 30_000)
    result = await executeCommand(job, spec)
  }
  if (result.code !== 0) throw new Error(`${spec.label}: код ${result.code}.${result.stderr.trim() ? `\n\n${result.stderr.trim()}` : ''}`)
}

const LOCKFILE_REGENERATE_COMMANDS: Record<string, { command: string; args: string[] }> = {
  'yarn.lock': { command: 'yarn', args: ['install'] },
  'package-lock.json': { command: 'npm', args: ['install', '--package-lock-only'] },
  'pnpm-lock.yaml': { command: 'pnpm', args: ['install', '--lockfile-only'] },
}

async function readGitStage(project: ProjectSpec, stage: 1 | 2 | 3, path: string): Promise<string | undefined> {
  const result = await spawnCapture('git', ['-C', project.path, 'show', `:${stage}:${path}`], project.path)
  return result.code === 0 ? result.stdout : undefined
}

// A merge conflict confined to package.json and/or one recognized lockfile
// is mechanical, not a judgment call -- and it's the *normal* shape for this
// tool's merges, not an edge case: two branch-plan groups bump disjoint sets
// of packages, but both edits land in the same dependencies block, so git's
// line-based merge reports a conflict even though nothing semantically
// overlaps (confirmed on a real one: package.json + yarn.lock conflicted,
// zero actually-overlapping packages between the two groups). package.json
// is resolved per dependency key against the merge base
// (mergePackageJsonThreeWay bails out on any real same-key conflict, or any
// difference outside the four dependency fields); the lockfile, once
// package.json is settled, is a fully derived artifact safe to regenerate.
// Any file outside {package.json, one lockfile} conflicting, either step
// failing, or conflict markers somehow surviving all fall through to the
// bounded agent recovery loop below. Git state remains inspectable throughout;
// the orchestrator alone owns the merge commit.
async function autoResolveMergeConflict(job: JobRecord, project: ProjectSpec, mergeMessage: string): Promise<boolean> {
  const conflicted = await spawnCapture('git', ['-C', project.path, 'diff', '--name-only', '--diff-filter=U'], project.path)
  if (conflicted.code !== 0) return false
  const paths = new Set(conflicted.stdout.split('\n').map((line) => line.trim()).filter(Boolean))
  if (paths.size === 0 || paths.size > 2) return false
  const lockfileCandidates = [...paths].filter((path) => path !== 'package.json')
  if (lockfileCandidates.length > 1) return false
  const lockfile = lockfileCandidates[0]
  if (lockfile && !LOCKFILE_REGENERATE_COMMANDS[lockfile]) return false
  if (!paths.has('package.json') && !lockfile) return false

  if (paths.has('package.json')) {
    const [base, ours, theirs] = await Promise.all([
      readGitStage(project, 1, 'package.json'),
      readGitStage(project, 2, 'package.json'),
      readGitStage(project, 3, 'package.json'),
    ])
    if (ours === undefined || theirs === undefined) return false
    const merged = mergePackageJsonThreeWay(base ?? '{}', ours, theirs)
    if (!merged) return false
    send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Конфликт merge в package.json — целевые пакеты обеих сторон не пересекаются, сливаем автоматически вместо остановки на ручное разрешение.' })
    writeFileSync(join(project.path, 'package.json'), merged, 'utf8')
    const addPackageJson = await executeCommand(job, { label: 'git add package.json', command: 'git', cwd: project.path, args: ['-C', project.path, 'add', '--', 'package.json'] })
    if (addPackageJson.code !== 0) return false
  }

  if (lockfile) {
    const regenerate = LOCKFILE_REGENERATE_COMMANDS[lockfile]
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Конфликт merge в ${lockfile} — перегенерируем его через «${regenerate.command} ${regenerate.args.join(' ')}» вместо остановки на ручное разрешение.` })
    const installResult = await executeCommand(job, { label: `Регенерация ${lockfile}`, command: regenerate.command, cwd: project.path, args: regenerate.args })
    if (installResult.code !== 0) return false
    const lockfilePath = join(project.path, lockfile)
    if (!existsSync(lockfilePath) || readFileSync(lockfilePath, 'utf8').includes('<<<<<<<')) return false
    const addLockfile = await executeCommand(job, { label: `git add ${lockfile}`, command: 'git', cwd: project.path, args: ['-C', project.path, 'add', '--', lockfile] })
    if (addLockfile.code !== 0) return false
  }

  const stillConflicted = await spawnCapture('git', ['-C', project.path, 'diff', '--name-only', '--diff-filter=U'], project.path)
  if (stillConflicted.code !== 0 || stillConflicted.stdout.trim()) return false
  const hooksArg = `core.hooksPath=${emptyHooksPath()}`
  const commitResult = await executeCommand(job, { label: 'Завершение merge после автоматического разрешения конфликта', command: 'git', cwd: project.path, args: ['-c', hooksArg, '-C', project.path, 'commit', '-m', mergeMessage] })
  return commitResult.code === 0
}


async function recoverMergeConflictWithAgent(
  job: JobRecord,
  project: ProjectSpec,
  plan: MigrationPlan,
  branch: MigrationBranchProgress,
  mergeMessage: string,
): Promise<boolean> {
  if (!(await mergeInProgress(project.path))) return false

  const currentBranch = await spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000)
  if (currentBranch.code !== 0 || currentBranch.stdout.trim() !== plan.mergedBranch) {
    throw new Error(`MERGE_RECOVERY_UNSAFE_BRANCH: ожидалась ${plan.mergedBranch}, фактически ${currentBranch.stdout.trim() || 'неизвестно'}. Git-состояние сохранено без дальнейших изменений.`)
  }

  const autonomy = autonomyPolicy(readSettings(job.workspace), project.name)
  const promptDir = join(app.getPath('userData'), 'merge-recovery-prompts')
  mkdirSync(promptDir, { recursive: true })
  const promptPath = join(promptDir, `${`${project.name}-${plan.mergedBranch}-${branch.branch}`.replace(/[^a-zA-Z0-9._-]+/g, '-')}.md`)
  let orchestratorFeedback = ''

  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: `Механическое разрешение конфликта не подошло. Передаю незавершённый merge агенту в ограниченный recovery-loop; ветки и target-план менять запрещено.`,
  })

  for (let attempt = 1; attempt <= autonomy.maxMergeRecoveryAttempts; attempt += 1) {
    const unresolved = await spawnCapture('git', ['-C', project.path, 'diff', '--name-only', '--diff-filter=U'], project.path, 15_000)
    if (unresolved.code !== 0) throw new Error(`MERGE_RECOVERY_GIT_STATE_FAILED: ${unresolved.stderr.trim() || 'не удалось прочитать unresolved paths'}`)
    const conflictFiles = unresolved.stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
    const basePrompt = buildMergeRecoveryPrompt({
      projectName: project.name,
      projectPath: project.path,
      mergedBranch: plan.mergedBranch,
      sourceBranch: branch.branch,
      conflictFiles,
      savedPromptPath: promptPathForProject(job.workspace, project.name),
    })
    const userNote = job.agentNote
    job.agentNote = undefined
    const recoveryPrompt = [
      basePrompt,
      userNote ? `## User recovery instruction\n\n${userNote}` : '',
      orchestratorFeedback ? `## Orchestrator feedback from the previous recovery attempt\n\n${orchestratorFeedback}` : '',
    ].filter(Boolean).join('\n\n') + '\n'
    writeFileSync(promptPath, recoveryPrompt, 'utf8')

    const openCodeServerUrl = await ensureOpenCodeServer(job, project.path)
    const spec = {
      ...agentStartSpec(job.workspace.agent, project, promptPath, recoveryPrompt, job.workspace.agentModel, undefined, openCodeServerUrl),
      label: `Автовосстановление merge ${branch.branch} (попытка ${attempt}/${autonomy.maxMergeRecoveryAttempts})`,
      captureAgentSession: false,
    }
    const result = await executeCommand(job, spec)

    const branchAfter = await spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000)
    if (branchAfter.code !== 0 || branchAfter.stdout.trim() !== plan.mergedBranch) {
      throw new Error(`MERGE_RECOVERY_SCOPE_VIOLATION: агент покинул ${plan.mergedBranch}. Фактически: ${branchAfter.stdout.trim() || 'неизвестно'}.`)
    }
    if (!(await mergeInProgress(project.path))) {
      throw new Error('MERGE_RECOVERY_SCOPE_VIOLATION: агент завершил или отменил merge сам, хотя merge-коммит принадлежит оркестратору. Проверьте историю Git перед повторным запуском.')
    }

    const remaining = await spawnCapture('git', ['-C', project.path, 'diff', '--name-only', '--diff-filter=U'], project.path, 15_000)
    const remainingFiles = remaining.stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
    if (result.code !== 0 || remaining.code !== 0 || remainingFiles.length) {
      orchestratorFeedback = [
        result.code !== 0 ? `agent exit code=${result.code}${result.stderr.trim() ? `: ${result.stderr.trim().slice(-1200)}` : ''}` : '',
        remaining.code !== 0 ? `не удалось перечитать unresolved paths: ${remaining.stderr.trim()}` : '',
        remainingFiles.length ? `остались unresolved paths: ${remainingFiles.join(', ')}` : '',
      ].filter(Boolean).join('; ')
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Merge recovery ${attempt}/${autonomy.maxMergeRecoveryAttempts} не завершён: ${orchestratorFeedback}` })
      continue
    }

    const diffCheck = await spawnCapture('git', ['-C', project.path, 'diff', '--cached', '--check'], project.path, 30_000)
    const markerLines = leftoverConflictMarkerLines(`${diffCheck.stdout}\n${diffCheck.stderr}`)
    if (markerLines.length) {
      orchestratorFeedback = `в проиндексированных файлах остались маркеры конфликта: ${markerLines.join('; ').slice(-1600)}`
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Merge recovery ${attempt}/${autonomy.maxMergeRecoveryAttempts} требует ещё одной правки: ${orchestratorFeedback}` })
      continue
    }

    const hooksArg = `core.hooksPath=${emptyHooksPath()}`
    const commitResult = await executeCommand(job, {
      label: 'Завершение merge после агентского recovery',
      command: 'git',
      cwd: project.path,
      args: ['-c', hooksArg, '-C', project.path, 'commit', '-m', mergeMessage],
      captureAgentSession: false,
    })
    if (commitResult.code !== 0) {
      orchestratorFeedback = `оркестратор не смог создать merge-коммит: ${commitResult.stderr.trim().slice(-1600)}`
      continue
    }
    if (await mergeInProgress(project.path)) {
      orchestratorFeedback = 'после merge-коммита всё ещё существует MERGE_HEAD'
      continue
    }
    const integrated = await spawnCapture('git', ['-C', project.path, 'merge-base', '--is-ancestor', branch.branch, plan.mergedBranch], project.path, 30_000)
    if (integrated.code !== 0) {
      throw new Error(`MIGRATION_REPLAN_REQUIRED: MERGE_RECOVERY_POSTCONDITION_FAILED: ${branch.branch} не является предком ${plan.mergedBranch} после recovery-коммита. Supervisor должен перечитать ancestry и построить continuation/reconciliation plan; не проси пользователя чинить ref вручную.`)
    }

    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: `Агент разрешил содержательный конфликт, git diff --check прошёл, merge ${branch.branch} завершён оркестратором.`,
    })
    return true
  }

  throw new Error(`MERGE_RECOVERY_EXHAUSTED: конфликт ${branch.branch} → ${plan.mergedBranch} не удалось безопасно разрешить за ${autonomy.maxMergeRecoveryAttempts} попытки. MERGE_HEAD и все диагностические изменения сохранены; повторный запуск снова начнёт с recovery, а не удалит прогресс.${orchestratorFeedback ? `\n\nПоследнее состояние: ${orchestratorFeedback}` : ''}`)
}

async function mergeGroupIntoMergedBranch(job: JobRecord, project: ProjectSpec, plan: MigrationPlan, branch: MigrationBranchProgress): Promise<void> {
  setBranchRuntime(job, branch.branch, 'merging', `Merge в ${plan.mergedBranch}`)
  const hooksArg = `core.hooksPath=${emptyHooksPath()}`
  const hookArgs = (args: string[]) => ['-c', hooksArg, '-C', project.path, ...args]
  const mergedExists = (await spawnCapture('git', ['-C', project.path, 'show-ref', '--verify', '--quiet', `refs/heads/${plan.mergedBranch}`], project.path)).code === 0
  const switchSpec: CommandSpec = mergedExists
    ? { label: `Переход на ${plan.mergedBranch}`, command: 'git', cwd: project.path, args: hookArgs(['switch', plan.mergedBranch]) }
    : { label: `Создание ${plan.mergedBranch} от ${plan.baseBranch}`, command: 'git', cwd: project.path, args: hookArgs(['switch', '-c', plan.mergedBranch, plan.baseBranch]) }
  const switchResult = await executeCommand(job, switchSpec)
  if (switchResult.code !== 0) throw new Error(`${switchSpec.label}: код ${switchResult.code}.${switchResult.stderr.trim() ? `\n\n${switchResult.stderr.trim()}` : ''}`)
  const mergeMessage = `Merge ${branch.branch} into ${plan.mergedBranch}`
  const mergeSpec: CommandSpec = { label: `Merge ${branch.branch} в ${plan.mergedBranch}`, command: 'git', cwd: project.path, args: hookArgs(['merge', '--no-ff', branch.branch, '-m', mergeMessage]) }
  const mergeResult = await executeCommand(job, mergeSpec)
  if (mergeResult.code !== 0) {
    if (await autoResolveMergeConflict(job, project, mergeMessage)) {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Конфликт merge разрешён автоматически, merge ${branch.branch} завершён.` })
      return
    }
    if (await recoverMergeConflictWithAgent(job, project, plan, branch, mergeMessage)) return
    throw new Error(`${mergeSpec.label}: код ${mergeResult.code}. Автоматическое восстановление merge не смогло подтвердить безопасное завершение.${mergeResult.stderr.trim() ? `\n\n${mergeResult.stderr.trim()}` : ''}`)
  }
}

// Runs one branch through one or more short execution batches. Compatibility
// cohorts stay atomic; normal retries use fresh sessions, while an exact
// fingerprinted session may resume only after a real external interruption.
// The prompt is regenerated fresh from the live dashboard so
// its manifest hash is computed by the real generator code rather than
// reimplemented here, but the branch plan the user actually reviewed and
// saved is the prompt file on disk -- if the roadmap moved between those two
// moments, migrationGroupScopeDriftIssues refuses to hand the agent a
// silently drifted scope.
async function runGroupAgentSession(job: JobRecord, workspace: WorkspaceRecord, project: ProjectSpec, dashboardUrl: string, savedPromptMarkdown: string, branch: MigrationBranchProgress, canonicalProjectPath = project.path): Promise<void> {
  const plan = migrationPlanFromPrompt(savedPromptMarkdown, project.name)
  if (!plan) throw new Error('не удалось прочитать Branch plan из сохранённого prompt')
  const settings = readSettings(workspace)
  const gatePolicy = migrationGatePolicy(settings, project.name, readProjectPackageJson(project), projectPackageManager(project))
  const autonomy = autonomyPolicy(settings, project.name)
  const scopeBranch = branch.scopeBranch ?? branch.branch
  const savedManifest = migrationScopeManifestFromPrompt(savedPromptMarkdown)
  const targetMode = savedManifest?.targetMode === 'default' || savedManifest?.targetMode === 'yellow' || savedManifest?.targetMode === 'green'
    ? savedManifest.targetMode
    : job.target === 'green' ? 'green' : 'yellow'
  const savedPlanBranch = plan.branches.find((item) => item.branch === branch.branch)
  const pinnedPackages = savedPlanBranch?.packages?.length ? savedPlanBranch.packages : branch.packages
  // Package membership from the reviewed plan is the semantic scope identity.
  // Fresh roadmap bucket names are cosmetic and may be renamed/repartitioned
  // after successful siblings disappear from the residual graph.
  const rawFullGroupPrompt = await buildGroupScopedPrompt(dashboardUrl, project.name, scopeBranch, targetMode, pinnedPackages)
  const driftIssues = migrationGroupScopeDriftIssues(rawFullGroupPrompt, savedPromptMarkdown, project.name, branch.branch, scopeBranch)
  if (driftIssues.length) {
    throw new Error('MIGRATION_REPLAN_REQUIRED: GROUP_SCOPE_DRIFT for ' + branch.branch + ' (scope ' + scopeBranch + '): ' + driftIssues.join('; ') + '. Saved prompt is stale; Supervisor must regenerate residual scope from the cumulative merged state.')
  }
  const bindPromptToGitBranch = (rawPrompt: string): string => {
    const rawPlan = migrationPlanFromPrompt(rawPrompt, project.name)
    const rawBranch = rawPlan?.branches.find((item) => item.branch === scopeBranch)
    if (!rawPlan || !rawBranch) throw new Error('GROUP_ALIAS_PLAN_MISSING: ' + project.name + '/' + scopeBranch)
    const rebound = rebindMigrationPromptBranchIdentity(rawPrompt, project.name, scopeBranch, {
      ...rawPlan,
      baseBranch: plan.baseBranch,
      mergedBranch: plan.mergedBranch,
      branches: [{ ...rawBranch, branch: branch.branch, scopeBranch, label: branch.label }],
    })
    if (!rebound) throw new Error('GROUP_ALIAS_REWRITE_FAILED: ' + project.name + '/' + scopeBranch + ' -> ' + branch.branch)
    return rebound
  }
  // The Dashboard was generated for the canonical checkout. Parallel workers
  // run in dedicated worktrees, so every operational path in both the full
  // group prompt and the materialized batch prompt must follow the worker.
  const bindPromptToWorkerPath = (prompt: string) => rebindOperationalProjectPath(prompt, canonicalProjectPath, project.path)
  const fullGroupPrompt = bindPromptToWorkerPath(bindPromptToGitBranch(rawFullGroupPrompt))
  const fullManifest = migrationScopeManifestFromPrompt(fullGroupPrompt)
  const executionContractV2 = readTeamState(workspace)?.projects[project.name]?.executionContractVersion === 2
  const materialization = executionContractV2
    ? await materializeApprovedDependencyAssignment(job, project, fullGroupPrompt, branch.branch)
    : { safeToSplitSemanticBatches: false, changedPackages: [] }
  const splitSemanticBatches = executionContractV2 && materialization.safeToSplitSemanticBatches
  if (executionContractV2) await ensureParallelWorkerEnvironment(job, project, canonicalProjectPath, branch.branch)
  let batches = buildAgentExecutionBatches(fullManifest, AGENT_EXECUTION_BATCH_MAX_PACKAGES, {
    splitCompatibilityCohorts: splitSemanticBatches,
  })
  if (!batches.length) throw new Error(`MIGRATION_REPLAN_REQUIRED: GROUP_SCOPE_EMPTY: ветка ${branch.branch} больше не содержит actionable package rows; Supervisor должен удалить stale branch из residual plan и продолжить.`)

  const promptDir = join(app.getPath('userData'), 'group-prompts')
  mkdirSync(promptDir, { recursive: true })
  const safeStem = `${project.name}-${branch.branch}`.replace(/[^a-zA-Z0-9._-]+/g, '-')
  const model = workspace.agentModel
  const openCodeServerUrl = await ensureOpenCodeServer(job, project.path)
  const savedPromptVersion = promptVersion(savedPromptMarkdown)
  let dashboardRevision = ''
  try { const path = projectDashboardPath(workspace, project.name); if (path) dashboardRevision = String(statSync(path).mtimeMs) } catch { /* scope hash + saved prompt version still protect resume */ }
  const sessionPromptVersion = `${savedPromptVersion}|dashboard:${dashboardRevision}|worktree:${portablePathKey(project.path)}`

  job.agentProvider = workspace.agent
  job.agentBranch = branch.branch
  setBranchRuntime(job, branch.branch, 'starting', 'Подготовка agent-сессии')
  job.agentSessionResumable = false

  if (batches.length > 1) {
    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: splitSemanticBatches
        ? `Группа ${branch.branch}: dependency assignment уже materialized атомарно; ${branch.packages.length} пакетов будут выполнены в ${batches.length} bounded semantic agent-batch. Compatibility cohort больше не определяет размер LLM-контекста.`
        : `Группа ${branch.branch}: ${branch.packages.length} пакетов будут выполнены в ${batches.length} agent-batch. Для legacy/dirty branch compatibility cohort остаются атомарными, пока control plane не может безопасно materialize assignment отдельно.`,
    })
  }

  for (let batchIndex = 0; batchIndex < batches.length; batchIndex += 1) {
    const batch = batches[batchIndex]
    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: `Batch ${batchIndex + 1}/${batches.length}: ${batch.packages.length} пакетов${batch.compatibilityCohorts.length ? `; cohort=${batch.compatibilityCohorts.join(', ')}` : ''}. Контекст и release facts ограничены этим batch; Git-ветка и общий Branch plan остаются прежними.`,
    })
    const rawBatchPrompt = batches.length === 1
      ? rawFullGroupPrompt
      : await buildGroupScopedPrompt(dashboardUrl, project.name, scopeBranch, targetMode, batch.packages)
    const batchDrift = migrationBatchScopeDriftIssues(rawBatchPrompt, rawFullGroupPrompt, batch.packages)
    if (batchDrift.length) {
      throw new Error('MIGRATION_REPLAN_REQUIRED: BATCH_SCOPE_DRIFT for ' + branch.branch + ' batch ' + (batchIndex + 1) + ': ' + batchDrift.join('; ') + '. Supervisor must rebuild this residual batch from current cumulative state.')
    }
    const batchStem = safeStem + '-batch-' + (batchIndex + 1)
    const materializedPrompt = materializeBatchRoadmap(bindPromptToWorkerPath(bindPromptToGitBranch(rawBatchPrompt)), project.name, batch.packages, promptDir, batchStem)
    const batchPrompt = splitSemanticBatches
      ? `${materializedPrompt}\n\n## Deterministic dependency-state contract\n\nThe orchestrator already materialized and committed the complete immutable dependency assignment for branch ${branch.branch} before this semantic batch. This batch may be only a subset of one compatibility cohort. Do not revert package targets outside this subset, do not choose versions, and do not edit overrides/resolutions. Focus on source/config migration and verification for the package subset above.\n`
      : materializedPrompt
    const promptBudget = assessAgentContextBudget([batchPrompt], 26_000, 2_000)
    if (!promptBudget.ok && splitSemanticBatches && batch.packages.length > 1) {
      const midpoint = Math.ceil(batch.packages.length / 2)
      const left = batch.packages.slice(0, midpoint)
      const right = batch.packages.slice(midpoint)
      batches.splice(batchIndex, 1,
        { packages: left, compatibilityCohorts: batch.compatibilityCohorts },
        { packages: right, compatibilityCohorts: batch.compatibilityCohorts },
      )
      send('flow:job-output', {
        jobId: job.id,
        stream: 'system',
        line: `Batch ${batchIndex + 1} оценён в ~${promptBudget.estimatedInputTokens} input tokens; автоматически дроблю semantic scope ${batch.packages.length} → ${left.length}+${right.length} до вызова модели. Dependency assignment остаётся тем же.`,
      })
      batchIndex -= 1
      continue
    }
    assertAgentContextBudget([batchPrompt], `${branch.branch} batch ${batchIndex + 1}/${batches.length}`)
    const promptPath = join(promptDir, `${batchStem}.md`)
    writeFileSync(promptPath, batchPrompt, 'utf8')

    const scopeHash = promptScopeHash(batchPrompt)
    if (!scopeHash) throw new Error(`MIGRATION_REPLAN_REQUIRED: BATCH_SCOPE_HASH_MISSING: ${branch.branch} batch ${batchIndex + 1}. Saved prompt/manifest cannot prove this batch identity; regenerate the residual prompt from cumulative merged instead of asking the user.`)
    const fingerprint = agentScopeFingerprint({
      provider: workspace.agent,
      project: project.name,
      branch: branch.branch,
      scopeHash,
      model,
      promptVersion: sessionPromptVersion,
    })
    const completionFingerprint = agentBatchCompletionFingerprint({
      project: project.name,
      branch: branch.branch,
      scopeHash,
      promptVersion: `${savedPromptVersion}|dashboard:${dashboardRevision}`,
    })
    job.agentScopeFingerprint = fingerprint

    let batchState = await batchExecutionState(job, project, branch.branch, batchPrompt, batch.packages)
    if (batchState.ready && (!executionContractV2 || completedAgentBatch(workspace, project.name, completionFingerprint))) {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Batch ${batchIndex + 1}/${batches.length} уже подтверждён control-plane marker (${batch.packages.join(', ')}), повторный вызов модели не нужен.` })
      continue
    }
    if (batchState.ready && splitSemanticBatches) {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Batch ${batchIndex + 1}/${batches.length}: package targets уже достигнуты control plane, но semantic completion marker отсутствует — Executor всё равно запускается для source/config migration и evidence.` })
    }

    const savedSession = readTeamState(workspace)?.projects[project.name]?.agentSessions?.[branch.branch]
    const resumeId = resumableAgentSessionId(savedSession, workspace.agent, fingerprint)
    if (savedSession?.interrupted && !resumeId) {
      send('flow:job-output', {
        jobId: job.id,
        stream: 'system',
        line: `Старая сессия ${branch.branch} не используется: её fingerprint не совпадает с текущим batch/prompt. Запускаю свежий контекст.`,
      })
    }
    job.agentSessionId = resumeId
    const batchStartedAt = Date.now()
    let feedback = resumeId
      ? `Возобновление действительно прерванного batch ${batchIndex + 1}/${batches.length}: ${batchState.feedback}.`
      : batchState.feedback
    let completed = false

    for (let attempt = 1; attempt <= autonomy.maxAgentAttemptsPerBatch; attempt += 1) {
      setBranchRuntime(job, branch.branch, 'running', `Batch ${batchIndex + 1}/${batches.length} · попытка ${attempt}/${autonomy.maxAgentAttemptsPerBatch}`)
      const userNote = job.agentNote
      job.agentNote = undefined
      if (userNote) {
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `Вы → агент: ${userNote}` })
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `Сообщение доставляется ${branch.branch}, batch ${batchIndex + 1}/${batches.length}, попытка ${attempt}.` })
      }

      // Resume is reserved for a real external interruption only. Once a CLI
      // invocation returns on its own, every autonomous retry starts a fresh
      // session grounded by the immutable batch prompt + current Git facts.
      // This prevents retry 2..6 from repeatedly billing the entire tool/chat
      // history accumulated by retry 1.
      const sessionToResume = attempt === 1 ? job.agentSessionId : undefined
      job.agentSessionResumable = false
      if (!sessionToResume) {
        job.agentSessionId = undefined
        job.stdoutBuffer = ''
      }
      const spec = sessionToResume
        ? agentResumeSpec(workspace.agent, project, sessionToResume, promptPath, model, feedback, 'Возобновление после прерывания', userNote, openCodeServerUrl)
        : agentStartSpec(workspace.agent, project, promptPath, batchPrompt, model, userNote, openCodeServerUrl, feedback)
      const result = await executeCommand(job, spec)
      const activeBranch = await spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000)
      if (activeBranch.code !== 0 || activeBranch.stdout.trim() !== branch.branch) {
        throw new Error(`MIGRATION_REPLAN_REQUIRED: AGENT_BRANCH_SCOPE_VIOLATION: агент должен был завершить batch в ${branch.branch}, но оставил HEAD в ${activeBranch.stdout.trim() || 'detached/unknown'}. Supervisor должен сопоставить фактическую ветку/scope, сохранить полезные коммиты и продолжить без пользовательского stop.`)
      }
      if (executionContractV2) await enforceMaterializationProofAfterAgent(job, project, branch.branch, materialization.proofPath)
      batchState = await batchExecutionState(job, project, branch.branch, batchPrompt, batch.packages)
      setBranchRuntime(job, branch.branch, 'verifying', `Проверка batch ${batchIndex + 1}/${batches.length}`)
      const checkpoint = latestAgentCheckpoint(join(artifactPath(workspace, 'historyDir', '.dependency-roadmap/history'), 'runs'), project.name, branch.branch, batchStartedAt)
      const verification = checkpointAssessment(checkpoint, plan)
      if (verification.status === 'replan-required') {
        if (executionContractV2) await enforceMaterializationProofAfterAgent(job, project, branch.branch, materialization.proofPath)
        const evidencePath = writeDependencyCompatibilityEvidence({
          workspace, project, canonicalProjectPath, branch: branch.branch, fullGroupPrompt, targetMode,
          commands: gatePolicy.verificationCommands, reason: verification.feedback, materializationProof: materialization.proofPath,
        })
        throw new Error(`MIGRATION_REPLAN_REQUIRED: DEPENDENCY_COMPATIBILITY_EVIDENCE: ${branch.branch}, batch ${batchIndex + 1}/${batches.length}; evidenceFile=${encodeURIComponent(evidencePath)}; Executor сообщил противоречие immutable targets. Control Plane сохранит ветку, детерминированно воспроизведёт candidate-vs-control, локализует nogood и только затем вернёт его Z3. ${verification.feedback}`)
      }
      if (batchState.ready && verification.status === 'pass') {
        completed = true
        markAgentBatchCompleted(workspace, project.name, completionFingerprint)
        clearAgentBranchSessionState(workspace, project.name, branch.branch)
        job.agentSessionId = undefined
        job.stdoutBuffer = ''
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `Batch ${batchIndex + 1}/${batches.length} завершён: ${batch.packages.join(', ')}. Verification checkpoint не содержит новых baseline→post регрессий.` })
        break
      }

      const verificationFeedback = verification.status === 'repair-required'
        ? `; ВАЖНО: ${verification.feedback}. Это новая регрессия относительно baseline — исправь её сейчас и повтори конкретные проверки; нельзя ссылаться на будущую группу, которой нет в текущем Branch plan.`
        : verification.status === 'unknown'
          ? '; verification checkpoint не найден/не читается — запиши state JSON с baseline/post evidence и migrationOutcome перед завершением'
          : ''
      feedback = `Batch ${batchIndex + 1}/${batches.length} не завершён после попытки ${attempt}: ${batchState.feedback}${result.code !== 0 ? `; CLI exit=${result.code}` : ''}${verificationFeedback}.${checkpoint ? ` Последний checkpoint этого запуска: ${checkpoint}. Прочитай его перед продолжением и используй nextAction/evidence вместо восстановления рассуждений из старого чата.` : ''}`
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `${feedback} Следующая автономная попытка начнётся в свежей сессии.` })
      clearAgentBranchSessionState(workspace, project.name, branch.branch)
      job.agentSessionId = undefined
      job.stdoutBuffer = ''
    }

    if (!completed) {
      throw new Error(`MIGRATION_REPAIR_EXHAUSTED: ${branch.branch}, batch ${batchIndex + 1}/${batches.length} не завершён после ${autonomy.maxAgentAttemptsPerBatch} свежих попыток. Baseline targets считаются утверждёнными и НЕ перепланируются автоматически; ветка и уже сделанный код сохранены. Последнее состояние: ${batchState.feedback}`)
    }
  }

  setBranchRuntime(job, branch.branch, 'verifying', 'Проверка группы')
  if (gatePolicy.verificationCommands.length) {
    await ensureParallelWorkerEnvironment(job, project, canonicalProjectPath, branch.branch)
    let gateAssessment = checkpointAssessment(latestAgentCheckpoint(join(artifactPath(workspace, 'historyDir', '.dependency-roadmap/history'), 'runs'), project.name, branch.branch, 0), plan)
    let verificationRun = allowKnownBaselineFailures(await runProjectVerification(job, project, gatePolicy.verificationCommands, `Проверка группы ${branch.branch}`), gateAssessment)
    if (!verificationRun.ok) verificationRun = await allowLiveBaselineFailures(job, project, plan, verificationRun, gateAssessment)
    for (let repairAttempt = 1; !verificationRun.ok && repairAttempt <= autonomy.maxGroupRepairAttempts; repairAttempt += 1) {
      setBranchRuntime(job, branch.branch, 'repairing', `Repair ${repairAttempt}/${autonomy.maxGroupRepairAttempts}`)
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Группа ${branch.branch} красная после собственной verification. Возвращаю конкретные ошибки агенту (repair ${repairAttempt}/${autonomy.maxGroupRepairAttempts}), merge пока запрещён.` })
      const groupRepairPath = join(promptDir, `${safeStem}-verification-repair.md`)
      const groupRepairPrompt = buildGroupVerificationRepairPrompt({
        projectName: project.name,
        projectPath: project.path,
        branch: branch.branch,
        savedPromptPath: promptPathForProject(workspace, project.name) ?? groupRepairPath,
        fullGroupPrompt,
        failure: verificationRun.feedback,
        verificationCommands: gatePolicy.verificationCommands,
      })
      writeFileSync(groupRepairPath, groupRepairPrompt, 'utf8')
      job.agentSessionId = undefined
      job.stdoutBuffer = ''
      const repairStartedAt = Date.now()
      const result = await executeCommand(job, {
        // The bounded repair prompt already contains the compact failure tail.
        // Do not append the same verification log again as gateFeedback.
        ...agentStartSpec(workspace.agent, project, groupRepairPath, groupRepairPrompt, model, undefined, openCodeServerUrl),
        label: `Repair verification ${branch.branch} (${repairAttempt}/${autonomy.maxGroupRepairAttempts})`,
      })
      if (executionContractV2) await enforceMaterializationProofAfterAgent(job, project, branch.branch, materialization.proofPath)
      const checkpoint = latestAgentCheckpoint(join(artifactPath(workspace, 'historyDir', '.dependency-roadmap/history'), 'runs'), project.name, branch.branch, repairStartedAt)
      const assessment = checkpointAssessment(checkpoint, plan)
      gateAssessment = assessment
      if (assessment.status === 'replan-required') {
        const evidencePath = writeDependencyCompatibilityEvidence({
          workspace, project, canonicalProjectPath, branch: branch.branch, fullGroupPrompt, targetMode,
          commands: gatePolicy.verificationCommands, reason: assessment.feedback, materializationProof: materialization.proofPath,
        })
        throw new Error(`MIGRATION_REPLAN_REQUIRED: DEPENDENCY_COMPATIBILITY_EVIDENCE: ${branch.branch}; evidenceFile=${encodeURIComponent(evidencePath)}; bounded repair подтвердил, что зелень требует изменения immutable dependency assignment. Deterministic Verifier воспроизведёт/локализует evidence до Z3. ${assessment.feedback}`)
      }
      const branchState = await batchExecutionState(job, project, branch.branch, fullGroupPrompt, branch.packages)
      if (!branchState.ready) {
        verificationRun = { ok: false, failures: [], feedback: `После repair агент оставил ветку неготовой: ${branchState.feedback}${result.code !== 0 ? `; CLI exit=${result.code}` : ''}` }
        continue
      }
      verificationRun = allowKnownBaselineFailures(await runProjectVerification(job, project, gatePolicy.verificationCommands, `Повтор проверки группы ${branch.branch}`), gateAssessment)
      if (!verificationRun.ok) verificationRun = await allowLiveBaselineFailures(job, project, plan, verificationRun, gateAssessment)
    }
    if (!verificationRun.ok) {
      throw new Error(`MIGRATION_REPAIR_EXHAUSTED: ${branch.branch} не прошла project verification после ${autonomy.maxGroupRepairAttempts} repair-попыток. Это failure миграции к уже утверждённым версиям, а не основание менять Baseline targets. Ветка сохранена.\n\n${verificationRun.feedback}`)
    }
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Группа ${branch.branch}: обязательная verification зелёная (${gatePolicy.source}).` })
  } else {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Для ${branch.branch} не найдены migration verification commands; используются только package-target + checkpoint gates. Рекомендуется задать migration.verificationCommands.` })
  }

  const finalExecutionState = await batchExecutionState(job, project, branch.branch, fullGroupPrompt, branch.packages)
  if (!finalExecutionState.ready) {
    throw new Error(`MIGRATION_GROUP_NOT_READY: все execution batch ${branch.branch} обработаны, но package/Git postcondition не выполнен: ${finalExecutionState.feedback}. Targets не меняются; продолжай repair этой же ветки.`)
  }
  markMigrationBranchCompleted(workspace, project.name, branch.branch)
  const progress = await readMigrationProgress(workspace, project)
  const finalBranch = progress?.branches.find((entry) => entry.branch === branch.branch)
  if (!finalBranch || (finalBranch.status !== 'ready' && finalBranch.status !== 'merged')) {
    throw new Error(`MIGRATION_GROUP_NOT_READY: все execution batch ${branch.branch} обработаны, но ветка не подтверждена как ready: ${finalBranch ? migrationBranchStateText(finalBranch) : 'состояние не прочитано'}. Targets не меняются; продолжай repair этой же ветки.`)
  }
  setBranchRuntime(job, branch.branch, 'ready', `Все ${branch.packages.length} целей подтверждены`)
  clearAgentBranchSessionState(workspace, project.name, branch.branch)
  job.agentSessionId = undefined
  job.agentScopeFingerprint = undefined
  job.agentSessionResumable = false
}

// Advances the whole remaining Branch plan one group at a time within a
// single FLOW-stage job: pick the next branch that isn't merged yet, get it
// to `ready` (skipping the agent session entirely if a prior run already
// finished it), merge it, and repeat -- so conversation context resets
// between groups instead of compounding across the whole migration the way
// the old single-session model did (removed in v0.1.39; this loop is now
// the only implementation of the 'agent' FlowAction).
async function pendingMergeBranch(project: ProjectSpec, progress: MigrationProgress): Promise<MigrationBranchProgress> {
  const mergeHead = await spawnCapture('git', ['-C', project.path, 'rev-parse', 'MERGE_HEAD'], project.path, 15_000)
  if (mergeHead.code !== 0) throw new Error(`MERGE_RECOVERY_GIT_STATE_FAILED: ${mergeHead.stderr.trim() || 'не удалось прочитать MERGE_HEAD'}`)
  const mergeHeads = mergeHead.stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (mergeHeads.length !== 1) {
    throw new Error(`MERGE_RECOVERY_SCOPE_VIOLATION: ожидался один MERGE_HEAD от группового merge, найдено ${mergeHeads.length}. Git-состояние сохранено.`)
  }
  const wanted = mergeHeads[0]
  for (const branch of progress.branches) {
    const head = await spawnCapture('git', ['-C', project.path, 'rev-parse', branch.branch], project.path, 15_000)
    if (head.code === 0 && head.stdout.trim() === wanted) return branch
  }
  throw new Error(`MERGE_RECOVERY_SCOPE_VIOLATION: MERGE_HEAD ${wanted.slice(0, 12)} не совпадает ни с одной веткой сохранённого Branch plan. Автовосстановление остановлено без изменения Git-состояния.`)
}

// allowKnownBaselineFailures only forgives a failing command when some
// earlier group's own focused checks happened to record its baseline. A gate
// command no group's scope ever touched (e.g. an eslint step unrelated to
// any updated package) has no evidence anywhere and can never be forgiven
// that way -- which is exactly what exhausted 3 real repair attempts on a
// chronic, migration-unrelated .eslintrc.js/tsconfig.json mismatch on a real
// deps-demo-merged run: the repair agent correctly diagnosed it as
// pre-existing and documented it, but documentation isn't the machine
// evidence this gate reads, so it stayed red anyway.
async function liveBaselineExitCodes(job: JobRecord, project: ProjectSpec, plan: MigrationPlan, commands: readonly string[]): Promise<Map<string, number>> {
  const root = migrationRootJob(job)
  root.baselineVerificationExitCodes ??= new Map<string, number>()
  const cache = root.baselineVerificationExitCodes
  const uniqueCommands = [...new Map(commands.map((command) => [verificationCommandKey(command), command])).values()]

  const collect = (): Map<string, number> => {
    const result = new Map<string, number>()
    for (const command of uniqueCommands) {
      const cached = cache.get(baselineVerificationCacheKey(project.name, plan.baseBranch, command))
      if (cached !== undefined) result.set(verificationCommandKey(command), cached)
    }
    return result
  }

  const missingCommands = (): string[] => uniqueCommands.filter((command) =>
    !cache.has(baselineVerificationCacheKey(project.name, plan.baseBranch, command)))

  if (!missingCommands().length) return collect()

  // Parallel workers share one root job. If another worker is already building
  // the base-branch proof, wait for it and reuse its results before deciding
  // whether anything remains to probe.
  if (root.baselineVerificationProbe) {
    await root.baselineVerificationProbe
    if (!missingCommands().length) return collect()
  }

  const toProbe = missingCommands()
  if (!toProbe.length) return collect()

  root.baselineVerificationProbe = (async () => {
    const parent = join(app.getPath('temp'), `dependency-flow-${root.id.replace(/[^a-zA-Z0-9._-]+/g, '-')}-baseline-${randomUUID()}`)
    const temporaryTree = join(parent, 'worktree')
    mkdirSync(parent, { recursive: true })
    const added = await spawnCapture('git', ['-C', project.path, 'worktree', 'add', '--detach', temporaryTree, plan.baseBranch], project.path, 300_000)
    if (added.code !== 0) {
      rmSync(parent, { recursive: true, force: true })
      return
    }
    try {
      const manager = projectPackageManager(project)
      const install = manager === 'yarn' ? { command: 'yarn', args: ['install', '--frozen-lockfile'] }
        : manager === 'pnpm' ? { command: 'pnpm', args: ['install', '--frozen-lockfile'] }
        : { command: 'npm', args: ['ci'] }
      const installResult = await executeCommand(job, { label: `Установка зависимостей baseline (${plan.baseBranch})`, command: install.command, args: install.args, cwd: temporaryTree, captureAgentSession: false })
      if (installResult.code !== 0) return
      cleanEphemeralVerificationCaches(temporaryTree)
      for (const commandText of toProbe) {
        const run = await executeCommand(job, { ...shellCommandSpec(`Baseline (${plan.baseBranch}): ${commandText}`, commandText, temporaryTree), captureAgentSession: false })
        cache.set(baselineVerificationCacheKey(project.name, plan.baseBranch, commandText), run.code)
      }
    } catch {
      // A baseline probe that itself fails to run must never be read as proof
      // of anything. Keep only complete command results already captured.
    } finally {
      await spawnCapture('git', ['-C', project.path, 'worktree', 'remove', '--force', temporaryTree], project.path, 120_000)
      rmSync(parent, { recursive: true, force: true })
    }
  })()

  try {
    await root.baselineVerificationProbe
  } finally {
    root.baselineVerificationProbe = undefined
  }
  return collect()
}

// Only probes commands `allowKnownBaselineFailures` couldn't judge at all
// (no group ever recorded a defined baseline exit code for them) -- a
// command some group DID baseline, whether tolerated or a genuine
// baseline=0-to-failing regression, is left entirely to that stricter check;
// this never overrides it.
async function allowLiveBaselineFailures(job: JobRecord, project: ProjectSpec, plan: MigrationPlan, run: VerificationRun, assessment: MigrationVerificationAssessment): Promise<VerificationRun> {
  if (run.ok) return run
  const unexplained = unexplainedFailures(run.failures, assessment.evidence)
  if (!unexplained.length) return run
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: `${unexplained.length} проверка(и) без baseline evidence ни в одной группе (${unexplained.map((item) => item.command).join(', ')}) — проверяю живой baseline на ${plan.baseBranch}.`,
  })
  const root = migrationRootJob(job)
  const gatePolicy = migrationGatePolicy(readSettings(root.workspace), project.name, readProjectPackageJson(project), projectPackageManager(project))
  // Probe the complete deterministic gate set once. Parallel groups then reuse
  // the same base-branch evidence instead of each performing a detached install.
  const baselineCommands = gatePolicy.verificationCommands.length
    ? gatePolicy.verificationCommands
    : unexplained.map((item) => item.command)
  const liveExitCodes = await liveBaselineExitCodes(job, project, plan, baselineCommands)
  const liveEvidence: VerificationEvidence[] = unexplained
    .filter((item) => liveExitCodes.has(verificationCommandKey(item.command)))
    .map((item) => ({ command: item.command, baselineExit: liveExitCodes.get(verificationCommandKey(item.command)) }))
  if (!liveEvidence.length) return run
  const retried = allowKnownBaselineFailures(run, { ...assessment, evidence: [...assessment.evidence, ...liveEvidence] })
  const confirmedPreExisting = liveEvidence.filter((item) => item.baselineExit !== 0)
  if (confirmedPreExisting.length) {
    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: `Живой baseline на ${plan.baseBranch} подтвердил: ${confirmedPreExisting.map((item) => `${item.command}=exit${item.baselineExit}`).join(', ')} уже падали до миграции — не считаю это регрессией.`,
    })
  }
  return retried
}

async function runMergedIntegrationVerification(
  job: JobRecord,
  project: ProjectSpec,
  plan: MigrationPlan,
  savedPromptPath: string,
  commands: readonly string[],
  sourceBranch: string,
): Promise<void> {
  setBranchRuntime(job, sourceBranch, 'integration-verifying', `Проверка ${plan.mergedBranch}`)
  if (!commands.length) {
    setBranchRuntime(job, sourceBranch)
    return
  }
  const before = await readMigrationProgress(job.workspace, project)
  if (!before || !before.trustworthy) throw new Error('MERGED_VERIFICATION_GIT_STATE_FAILED: не удалось надёжно прочитать состояние Branch plan перед integration gate.')
  const expectedMerged = new Set(before.branches.filter((entry) => entry.status === 'merged').map((entry) => entry.branch))

  const sourceAssessment = checkpointAssessment(latestAgentCheckpoint(join(artifactPath(job.workspace, 'historyDir', '.dependency-roadmap/history'), 'runs'), project.name, sourceBranch, 0), plan)
  let gate = allowKnownBaselineFailures(await runProjectVerification(job, project, commands, `Integration gate ${plan.mergedBranch}`), sourceAssessment)
  if (!gate.ok) gate = await allowLiveBaselineFailures(job, project, plan, gate, sourceAssessment)
  if (gate.ok) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Накопительный ${plan.mergedBranch}: integration verification зелёная.` })
    setBranchRuntime(job, sourceBranch)
    return
  }

  const promptDir = join(app.getPath('userData'), 'merged-repair-prompts')
  mkdirSync(promptDir, { recursive: true })
  const safeStem = `${project.name}-${plan.mergedBranch}`.replace(/[^a-zA-Z0-9._-]+/g, '-')
  const promptPath = join(promptDir, `${safeStem}.md`)
  const resultPath = join(promptDir, `${safeStem}-result.json`)
  const openCodeServerUrl = await ensureOpenCodeServer(job, project.path)
  const autonomy = autonomyPolicy(readSettings(job.workspace), project.name)
  let feedback = gate.feedback

  for (let attempt = 1; attempt <= autonomy.maxIntegrationRepairAttempts; attempt += 1) {
    try { if (existsSync(resultPath)) unlinkSync(resultPath) } catch { /* next attempt will overwrite */ }
    const repairPrompt = buildMergedRepairPrompt({
      projectName: project.name,
      projectPath: project.path,
      mergedBranch: plan.mergedBranch,
      savedPromptPath,
      resultPath,
      failure: feedback,
      verificationCommands: [...commands],
    })
    writeFileSync(promptPath, repairPrompt, 'utf8')
    job.agentProvider = job.workspace.agent
    job.agentBranch = plan.mergedBranch
    job.agentSessionId = undefined
    job.agentSessionResumable = false
    job.stdoutBuffer = ''
    setBranchRuntime(job, sourceBranch, 'repairing', `Integration repair ${attempt}/${autonomy.maxIntegrationRepairAttempts}`)
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `После merge накопительный ${plan.mergedBranch} красный. Запускаю migration integration repair ${attempt}/${autonomy.maxIntegrationRepairAttempts} до перехода к следующей группе/release.` })
    const agent = await executeCommand(job, {
      ...agentStartSpec(job.workspace.agent, project, promptPath, repairPrompt, job.workspace.agentModel, undefined, openCodeServerUrl, feedback),
      label: `Migration integration repair ${plan.mergedBranch} (${attempt}/${autonomy.maxIntegrationRepairAttempts})`,
      captureAgentSession: false,
    })
    const machine = readMergedRepairResult(resultPath)
    if (machine?.status === 'replan-required') {
      const fullPrompt = readFileSync(savedPromptPath, 'utf8')
      const mergedPackageJson = readProjectPackageJson(project)
      const materialized = satisfiedScopePackagesFromPrompt(fullPrompt, project.name, mergedPackageJson) ?? new Set<string>()
      const manifest = migrationScopeManifestFromPrompt(fullPrompt)
      const targetMode = manifest?.targetMode === 'yellow' || manifest?.targetMode === 'green' || manifest?.targetMode === 'default'
        ? manifest.targetMode
        : job.target === 'green' ? 'green' : 'yellow'
      const evidencePath = writeDependencyCompatibilityEvidence({
        workspace: job.workspace, project, canonicalProjectPath: project.path, branch: plan.mergedBranch, fullGroupPrompt: fullPrompt, targetMode,
        commands, reason: machine.reason, includePackages: materialized,
      })
      throw new Error(`MIGRATION_REPLAN_REQUIRED: DEPENDENCY_COMPATIBILITY_EVIDENCE: ${plan.mergedBranch}; evidenceFile=${encodeURIComponent(evidencePath)}; cumulative integration repair доказал, что уже materialized subset несовместим с immutable dependency state. Deterministic Verifier воспроизведёт candidate-vs-control, локализует nogood и только затем вернёт его Z3. ${machine.reason}`)
    }
    if (machine?.status === 'blocked') throw new Error(`MERGED_INTEGRATION_REPAIR_BLOCKED: ${machine.reason}`)

    const [branchAfter, mergeAfter, statusAfter] = await Promise.all([
      spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000),
      spawnCapture('git', ['-C', project.path, 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], project.path, 15_000),
      spawnCapture('git', ['-C', project.path, 'status', '--porcelain=v1', '--untracked-files=all'], project.path, 15_000),
    ])
    if (branchAfter.code !== 0 || branchAfter.stdout.trim() !== plan.mergedBranch) {
      throw new Error(`MIGRATION_REPLAN_REQUIRED: MERGED_INTEGRATION_REPAIR_SCOPE_VIOLATION: integration-repair агент покинул ${plan.mergedBranch}; фактически ${branchAfter.stdout.trim() || 'неизвестно'}. Supervisor должен карантинизировать фактический ref/diff, вернуть checkout на cumulative merged когда это безопасно и продолжить без пользователя.`)
    }
    if (mergeAfter.code === 0) throw new Error('MERGED_INTEGRATION_REPAIR_SCOPE_VIOLATION: агент начал новый merge во время integration repair.')
    if (machine?.status === 'repaired' && relevantGitStatus(statusAfter.stdout)) {
      feedback = `Агент сообщил repaired, но рабочее дерево не чистое. Сначала заверши/закоммить минимальный integration fix через skip-hooks wrapper.\n${relevantGitStatus(statusAfter.stdout)}`
      continue
    }

    gate = allowKnownBaselineFailures(await runProjectVerification(job, project, commands, `Повтор integration gate ${plan.mergedBranch}`), sourceAssessment)
    if (!gate.ok) {
      feedback = `После integration repair проверки всё ещё красные${agent.code !== 0 ? `; agent exit=${agent.code}` : ''}.\n\n${gate.feedback}`
      continue
    }

    const after = await readMigrationProgress(job.workspace, project)
    if (!after || !after.trustworthy) throw new Error('MERGED_INTEGRATION_REPAIR_GIT_STATE_FAILED: не удалось проверить Branch plan после repair.')
    const lost = [...expectedMerged].filter((branchName) => after.branches.find((entry) => entry.branch === branchName)?.status !== 'merged')
    if (lost.length) {
      throw new Error(`MIGRATION_REPLAN_REQUIRED: MERGED_INTEGRATION_REPAIR_SCOPE_VIOLATION: integration repair нарушил уже достигнутые direct targets: ${lost.join(', ')}. Supervisor должен восстановить подтверждённые targets или откатить только repair-delta детерминированным способом; красный пользовательский stop не нужен, пока Git-state надёжен.`)
    }
    const statusAfterGate = await spawnCapture('git', ['-C', project.path, 'status', '--porcelain=v1', '--untracked-files=all'], project.path, 15_000)
    if (statusAfterGate.code !== 0 || relevantGitStatus(statusAfterGate.stdout)) {
      throw new Error(`MERGED_INTEGRATION_REPAIR_DIRTY: verification зелёная, но ${plan.mergedBranch} содержит незакоммиченные изменения после проверки.`)
    }
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Integration repair завершён: ${plan.mergedBranch} снова зелёный, ранее достигнутые package targets сохранены.` })
    setBranchRuntime(job, sourceBranch)
    job.agentBranch = undefined
    return
  }

  throw new Error(`MERGED_INTEGRATION_REPAIR_EXHAUSTED: ${plan.mergedBranch} остаётся красным после 3 свежих repair-попыток.\n\n${gate.feedback}`)
}

type ParallelGroupWorker = {
  branch: MigrationBranchProgress
  worktreePath: string
  project: ProjectSpec
  job: JobRecord
}

type ParallelGroupFailure = { branch: MigrationBranchProgress; message: string }

async function worktreePathForBranch(projectPath: string, branchName: string): Promise<string | undefined> {
  const result = await spawnCapture('git', ['-C', projectPath, 'worktree', 'list', '--porcelain'], projectPath, 30_000)
  if (result.code !== 0) return undefined
  return liveGitWorktreeRecords(result.stdout).find((record) => record.branch === branchName && existsSync(record.path))?.path
}

function parallelWorkerJob(parent: JobRecord, branch: MigrationBranchProgress): JobRecord {
  const worker: JobRecord = {
    ...parent,
    // A distinct output id keeps concurrent provider streams separable in the
    // UI while parallelParent still owns cancellation and FLOW lifecycle.
    id: `${parent.id}::group::${branch.branch}`,
    child: undefined,
    cancelled: false,
    agentSessionId: undefined,
    agentScopeFingerprint: undefined,
    agentSessionResumable: false,
    agentBranch: branch.branch,
    agentNote: undefined,
    stdoutBuffer: '',
    openCodeServer: undefined,
    openCodeServerUrl: undefined,
    openCodeServerError: undefined,
    openCodeServerStarting: undefined,
    openCodeRuntimeRoot: undefined,
    openCodeDbPath: undefined,
    openCodeDbGeneration: undefined,
    parallelParent: parent,
    parallelChildren: undefined,
    parallelJobs: undefined,
    logSource: { kind: 'group', id: branch.branch, label: branch.label || branch.branch },
  }
  const root = migrationRootJob(parent)
  root.parallelJobs ??= new Map<string, JobRecord>()
  root.parallelJobs.set(branch.branch, worker)
  jobs.set(worker.id, worker)
  return worker
}

async function prepareParallelGroupWorker(
  parent: JobRecord,
  project: ProjectSpec,
  plan: MigrationPlan,
  branch: MigrationBranchProgress,
): Promise<ParallelGroupWorker | undefined> {
  const { packageRelativePath } = await resolveProjectGitLayout(project)
  const existing = await worktreePathForBranch(project.path, branch.branch)
  if (existing) {
    // Never commandeer a user's arbitrary worktree. Tool-managed worktrees are
    // under this prefix; anything else simply falls back to sequential mode.
    if (!isToolManagedWorktreePath(existing, app.getPath('temp'))) {
      send('flow:job-output', { jobId: parent.id, stream: 'system', source: { kind: 'group', id: branch.branch, label: branch.label || branch.branch }, line: `Параллельный worker ${branch.branch} пропущен: ветка уже checkout в пользовательском worktree ${existing}. Она будет обработана последовательно.` })
      return undefined
    }
    return { branch, worktreePath: existing, project: { ...project, path: projectPathInWorktree(existing, packageRelativePath) }, job: parallelWorkerJob(parent, branch) }
  }

  const root = join(app.getPath('temp'), 'dependency-flow-worktrees', parent.id)
  mkdirSync(root, { recursive: true })
  const worktreePath = join(root, branch.branch.replace(/[^a-zA-Z0-9._-]+/g, '-'))
  const branchExists = (await spawnCapture('git', ['-C', project.path, 'show-ref', '--verify', '--quiet', `refs/heads/${branch.branch}`], project.path, 15_000)).code === 0
  const args = branchExists
    ? ['-C', project.path, 'worktree', 'add', worktreePath, branch.branch]
    : ['-C', project.path, 'worktree', 'add', '-b', branch.branch, worktreePath, plan.baseBranch]
  let added = await spawnCapture('git', args, project.path, 120_000)
  if (added.code !== 0 && /already used by worktree/i.test(added.stderr)) {
    await spawnCapture('git', ['-C', project.path, 'worktree', 'prune', '--expire', 'now'], project.path, 30_000)
    added = await spawnCapture('git', args, project.path, 120_000)
  }
  if (added.code !== 0) {
    send('flow:job-output', { jobId: parent.id, stream: 'system', source: { kind: 'group', id: branch.branch, label: branch.label || branch.branch }, line: `Не удалось создать parallel worktree для ${branch.branch}; без остановки FLOW возвращаю группу в последовательный режим. ${added.stderr.trim().slice(-800)}` })
    return undefined
  }
  return { branch, worktreePath, project: { ...project, path: projectPathInWorktree(worktreePath, packageRelativePath) }, job: parallelWorkerJob(parent, branch) }
}

async function cleanupParallelGroupWorker(parent: JobRecord, rootProject: ProjectSpec, worker: ParallelGroupWorker): Promise<{ clean: boolean; details?: string }> {
  stopOpenCodeServer(worker.job)
  migrationRootJob(parent).parallelJobs?.delete(worker.branch.branch)
  jobs.delete(worker.job.id)
  const status = await spawnCapture('git', ['-C', worker.worktreePath, 'status', '--porcelain=v1', '--untracked-files=all'], worker.worktreePath, 20_000)
  if (status.code !== 0) return { clean: false, details: status.stderr.trim() || 'git status failed' }
  const relevantStatus = relevantGitStatus(status.stdout)
  if (relevantStatus) {
    return { clean: false, details: relevantStatus.slice(-1800) }
  }
  const removed = await spawnCapture('git', ['-C', rootProject.path, 'worktree', 'remove', '--force', worker.worktreePath], rootProject.path, 60_000)
  if (removed.code !== 0) {
    send('flow:job-output', { jobId: parent.id, stream: 'system', line: `Worker ${worker.branch.branch} завершён, но временный worktree не удалось удалить автоматически; Git-ветка сохранена. ${removed.stderr.trim().slice(-700)}` })
  }
  return { clean: true }
}

async function runParallelGroupWave(
  job: JobRecord,
  project: ProjectSpec,
  plan: MigrationPlan,
  progress: MigrationProgress,
  dashboardUrl: string,
  savedPromptMarkdown: string,
  promptPath: string,
  integrationCommands: readonly string[],
  maxParallelGroups: number,
): Promise<boolean> {
  const queue = selectParallelGroupQueue(progress.branches)
  if (maxParallelGroups <= 1 || queue.branches.length < 2) return false

  // The main checkout is reserved for deterministic integration while workers
  // own group branches. Park it on merged/base before adding worktrees.
  const mergedExists = (await spawnCapture('git', ['-C', project.path, 'show-ref', '--verify', '--quiet', `refs/heads/${plan.mergedBranch}`], project.path, 15_000)).code === 0
  const parkingBranch = mergedExists ? plan.mergedBranch : plan.baseBranch
  const status = await spawnCapture('git', ['-C', project.path, 'status', '--porcelain=v1', '--untracked-files=all'], project.path, 20_000)
  if (status.code !== 0 || relevantGitStatus(status.stdout)) return false
  const switched = await spawnCapture('git', ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'switch', parkingBranch], project.path, 30_000)
  if (switched.code !== 0) return false

  const prepared: ParallelGroupWorker[] = []
  for (const branch of queue.branches) {
    const worker = await prepareParallelGroupWorker(job, project, plan, branch)
    if (worker) prepared.push(worker)
  }
  if (prepared.length < 2) {
    for (const worker of prepared) await cleanupParallelGroupWorker(job, project, worker)
    return false
  }

  for (const worker of prepared) setBranchRuntime(worker.job, worker.branch.branch, 'queued', 'Ожидает свободный agent-слот')
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: `Параллельная миграция: configured slots=${maxParallelGroups}, очередь=${prepared.length}, открываю сейчас=${Math.min(maxParallelGroups, prepared.length)}. Каждый разрешённый слот получает группу немедленно; merge и cumulative verification останутся последовательными.`,
  })

  const outcomes = await runParallelQueue(prepared, maxParallelGroups, async (worker) => {
    const startedAt = Date.now()
    setBranchRuntime(worker.job, worker.branch.branch, 'starting', 'Слот открыт, подготовка agent-сессии')
    try {
      await runGroupAgentSession(worker.job, job.workspace, worker.project, dashboardUrl, savedPromptMarkdown, worker.branch, project.path)
      send('flow:job-output', { jobId: worker.job.id, stream: 'system', line: `${worker.branch.branch}: worker завершён за ${Math.max(1, Math.round((Date.now() - startedAt) / 1000))} с; слот освобождён.` })
      return { worker, ok: true as const }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setBranchRuntime(worker.job, worker.branch.branch, 'failed', message)
      send('flow:job-output', { jobId: worker.job.id, stream: 'stderr', line: `${worker.branch.branch}: worker завершился с blocker за ${Math.max(1, Math.round((Date.now() - startedAt) / 1000))} с. Причина: ${message.slice(0, 3000)}` })
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `${worker.branch.branch}: слот освобождён; после завершения активных siblings Supervisor автоматически обработает blocker.` })
      return { worker, ok: false as const, message }
    }
  })

  const failures: ParallelGroupFailure[] = []
  for (const outcome of outcomes) {
    const cleanup = await cleanupParallelGroupWorker(job, project, outcome.worker)
    if (!cleanup.clean) {
      failures.push({ branch: outcome.worker.branch, message: `PARALLEL_WORKTREE_DIRTY: ${outcome.worker.branch.branch} оставила незакоммиченные изменения в ${outcome.worker.worktreePath}. Внутренний agent-loop их не смог безопасно завершить. ${cleanup.details ?? ''}` })
    } else if (!outcome.ok) {
      failures.push({ branch: outcome.worker.branch, message: outcome.message })
    }
  }

  // Integrate every successfully prepared branch in the immutable Branch-plan
  // order. A failing sibling cannot prevent already-green work from landing.
  let latest = await readMigrationProgress(job.workspace, project)
  if (!latest || !latest.trustworthy) throw new Error('MERGED_VERIFICATION_GIT_STATE_FAILED: не удалось перечитать Branch plan после parallel workers.')
  const successfulNames = new Set(outcomes.filter((item) => item.ok).map((item) => item.worker.branch.branch))
  for (const planned of plan.branches) {
    if (!successfulNames.has(planned.branch)) continue
    const ready = latest.branches.find((item) => item.branch === planned.branch)
    if (!ready || ready.status === 'merged') continue
    if (ready.status !== 'ready') {
      failures.push({ branch: ready ?? ({ ...planned, status: 'created', checkedOut: false, metPackages: 0 } as MigrationBranchProgress), message: `MIGRATION_REPLAN_REQUIRED: parallel worker ${planned.branch} завершился без ready postcondition (${ready ? migrationBranchStateText(ready) : 'branch state missing'}).` })
      continue
    }
    await mergeGroupIntoMergedBranch(job, project, plan, ready)
    await runMergedIntegrationVerification(job, project, plan, promptPath, integrationCommands, ready.branch)
    showFlowNotification({ kind: 'group-complete', projectName: project.name, branch: ready.branch, index: plan.branches.findIndex((item) => item.branch === ready.branch) + 1, total: plan.branches.length, packages: ready.packages })
    latest = await readMigrationProgress(job.workspace, project) ?? latest
  }

  if (failures.length) {
    const first = failures[0]
    const remaining = failures.slice(1).map((item) => `${item.branch.branch}: ${item.message.split(/\r?\n/)[0]}`).join('; ')
    const firstClassification = classifyFlowRecovery(first.message, 'agent')
    const preserve = firstClassification.kind === 'infrastructure' || ['MIGRATION_REPLAN_REQUIRED:', 'PARALLEL_WORKER_BOOTSTRAP_FAILED:', 'INFRA_PACKAGE_MANAGER_NOT_FOUND:', 'BASELINE_PLAN_BROKEN:', 'MIGRATION_REPAIR_EXHAUSTED:', 'MIGRATION_GROUP_NOT_READY:']
      .some((prefix) => first.message.startsWith(prefix))
    const baseMessage = preserve
      ? first.message
      : `MIGRATION_GROUP_FAILED: ${first.branch.branch} не завершилась в parallel worker. Targets и полезная работа сохранены. ${first.message}`
    throw new Error(`${baseMessage}${remaining ? `\n\nДругие parallel blockers (зелёные siblings уже merged): ${remaining}` : ''}`)
  }
  return true
}

async function runMigrationAgentIteration(job: JobRecord): Promise<void> {
  const project = findProject(job.workspace, job.projectName as string)
  const selectedPromptPath = promptPathForProject(job.workspace, project.name)
  if (!selectedPromptPath || !existsSync(selectedPromptPath)) throw new Error('Сначала выгрузите prompt из Dashboard или выберите prompt-файл.')
  const selectedPromptMarkdown = readFileSync(selectedPromptPath, 'utf8')
  const continuation = await materializeContinuationPrompt(job, project, selectedPromptPath, selectedPromptMarkdown)
  const promptPath = continuation.promptPath
  const savedPromptMarkdown = continuation.markdown
  const dashboardPath = projectDashboardPath(job.workspace, project.name)
  if (!dashboardPath || !existsSync(dashboardPath)) throw new Error('Dashboard HTML этого проекта не найден — перестройте отчёт («Верификация») перед запуском по группам.')
  const dashboardUrl = `${DASHBOARD_SCHEME}://dashboard/${encodeURIComponent(job.workspace.id)}?desktop-export=1&project=${encodeURIComponent(project.name)}&v=${Date.now()}`
  const settings = readSettings(job.workspace)
  const gatePolicy = migrationGatePolicy(settings, project.name, readProjectPackageJson(project), projectPackageManager(project))
  if (gatePolicy.integrationVerificationCommands.length) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Migration verification: ${gatePolicy.integrationVerificationCommands.join(' | ')} (source=${gatePolicy.source}). Красная группа не merge-ится; красный merged ремонтируется до release.` })
  }
  const initialPlan = migrationPlanFromPrompt(savedPromptMarkdown, project.name)
  if (!initialPlan) throw new Error('не удалось прочитать Branch plan из сохранённого prompt')
  await ensureMigrationBaseBranch(job, project, initialPlan, savedPromptMarkdown)

  for (;;) {
    const progress = await readMigrationProgress(job.workspace, project)
    if (!progress) throw new Error('не удалось прочитать Branch plan из сохранённого prompt')
    if (!progress.trustworthy) throw new Error('Не удалось надёжно прочитать состояние Git. Повторите запуск.')
    if (progress.unexpectedBranches.length) {
      const currentIsExtra = progress.unexpectedBranches.includes(progress.currentBranch)
      send('flow:job-output', {
        jobId: job.id,
        stream: 'system',
        line: `Найдены дополнительные Git refs вне текущей ревизии Branch plan: ${progress.unexpectedBranches.join(', ')}. Это больше не пользовательский stop: refs карантинизированы и не будут merged автоматически. Supervisor проверит/усыновит полезную continuation-ветку при следующем residual replan.`,
      })
      if (currentIsExtra) {
        const status = await spawnCapture('git', ['-C', project.path, 'status', '--porcelain=v1', '--untracked-files=all'], project.path, 15_000)
        if (status.code !== 0) throw new Error('MERGED_VERIFICATION_GIT_STATE_FAILED: не удалось проверить дополнительную ветку перед автономным возвратом в план.')
        if (!relevantGitStatus(status.stdout)) {
          const parked = await spawnCapture('git', ['-c', `core.hooksPath=${emptyHooksPath()}`, '-C', project.path, 'switch', initialPlan.mergedBranch], project.path, 30_000)
          if (parked.code === 0) {
            send('flow:job-output', { jobId: job.id, stream: 'system', line: `HEAD находился на дополнительной чистой ветке ${progress.currentBranch}; автоматически вернулся на ${initialPlan.mergedBranch}. Ветка сохранена для возможного Supervisor adoption.` })
            continue
          }
        }
        throw new Error(`MIGRATION_REPLAN_REQUIRED: EXTRA_BRANCH_NEEDS_SUPERVISOR: текущая ветка ${progress.currentBranch} отсутствует в свежем Branch plan и содержит/может содержать полезную работу. Не проси пользователя разбираться: Supervisor должен сопоставить diff/пакеты с residual scope, усыновить безопасную continuation-работу либо карантинизировать её и продолжить.`)
      }
    }
    const plan = migrationPlanFromPrompt(savedPromptMarkdown, project.name)
    if (!plan) throw new Error('не удалось прочитать Branch plan из сохранённого prompt')
    if (await mergeInProgress(project.path)) {
      // Never guess that MERGE_HEAD belongs to `next`: after an interruption
      // the progress ordering can differ from the exact merge Git is holding.
      // Match MERGE_HEAD to a reviewed plan branch before giving the state to
      // an agent; an unknown head is a hard scope-safety stop.
      const pending = await pendingMergeBranch(project, progress)
      const mergeMessage = `Merge ${pending.branch} into ${plan.mergedBranch}`
      await recoverMergeConflictWithAgent(job, project, plan, pending, mergeMessage)
      await runMergedIntegrationVerification(job, project, plan, promptPath, gatePolicy.integrationVerificationCommands, pending.branch)
      showFlowNotification({ kind: 'group-complete', projectName: project.name, branch: pending.branch, index: progress.branches.indexOf(pending) + 1, total: progress.branches.length, packages: pending.packages })
      // Recovery commits the pending merge. Re-read progress instead of
      // continuing with the pre-recovery snapshot.
      continue
    }
    // Settings may change while a long migration/recovery loop is alive. Read
    // the slot limit for every residual queue instead of freezing the default
    // from the first iteration for the lifetime of the process.
    const liveMaxParallelGroups = autonomyPolicy(readSettings(job.workspace), project.name).maxParallelGroups
    if (liveMaxParallelGroups > 1) {
      const parallelRan = await runParallelGroupWave(job, project, plan, progress, dashboardUrl, savedPromptMarkdown, promptPath, gatePolicy.integrationVerificationCommands, liveMaxParallelGroups)
      if (parallelRan) continue
    }
    const next = nextIncompleteMigrationBranch(progress)
    if (!next) break
    if (next.status === 'integrated') {
      throw new Error(`MIGRATION_REPLAN_REQUIRED: ${next.branch} оказалась интегрирована не в ожидаемый merged (${migrationBranchStateText(next)}). Supervisor должен определить фактическую ancestry/topology и продолжить автоматически; пользовательское вмешательство не требуется, пока Git-state надёжен.`)
    }
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Группа ${progress.branches.indexOf(next) + 1} из ${progress.branches.length}: ${next.branch} (${next.label}).` })
    if (next.status !== 'ready') {
      const existingWorktree = await worktreePathForBranch(project.path, next.branch)
      if (existingWorktree && portablePathKey(existingWorktree) !== portablePathKey(project.path)) {
        if (!isToolManagedWorktreePath(existingWorktree, app.getPath('temp'))) {
          throw new Error(`MIGRATION_REPLAN_REQUIRED: PARALLEL_USER_WORKTREE_BLOCKED: ${next.branch} уже checkout в пользовательском worktree ${existingWorktree}. DepLoom не будет менять чужой checkout; Supervisor должен временно отложить эту branch/cohort и продолжить зелёных siblings, не показывая пользователю красный stop.`)
        }
        const { packageRelativePath } = await resolveProjectGitLayout(project)
        const worker: ParallelGroupWorker = { branch: next, worktreePath: existingWorktree, project: { ...project, path: projectPathInWorktree(existingWorktree, packageRelativePath) }, job: parallelWorkerJob(job, next) }
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `Продолжаю ${next.branch} в сохранённом tool-managed worktree ${existingWorktree}; ручное переключение ветки не требуется.` })
        let workerSucceeded = false
        try {
          await runGroupAgentSession(worker.job, job.workspace, worker.project, dashboardUrl, savedPromptMarkdown, next, project.path)
          workerSucceeded = true
        } finally {
          if (!workerSucceeded) {
            // Preserve useful partial edits for the next internal recovery loop.
            stopOpenCodeServer(worker.job)
          }
        }
        // Cleanup is deliberately outside finally: a cleanup failure after a
        // successful worker is actionable, but must never overwrite the
        // worker's original exception.
        const cleanup = await cleanupParallelGroupWorker(job, project, worker)
        if (!cleanup.clean) throw new Error(`MIGRATION_REPLAN_REQUIRED: ${next.branch} выполнила targets, но tool-managed worktree остался dirty. Внутренний агент должен завершить/зафиксировать эти изменения перед merge. ${cleanup.details ?? ''}`)
      } else {
        await ensureGroupBranchCheckedOut(job, project, plan, next)
        await runGroupAgentSession(job, job.workspace, project, dashboardUrl, savedPromptMarkdown, next)
      }
      job.agentSessionId = undefined
      job.agentBranch = undefined
    }
    // A note is about whatever group runGroupAgentSession actually ran for
    // (which consumes it itself, per attempt) -- if this group was already
    // `ready` and the agent never ran at all, drop any leftover note here
    // too, so it can't silently bleed into a *different* group the loop
    // goes on to reach afterward.
    job.agentNote = undefined
    await mergeGroupIntoMergedBranch(job, project, plan, next)
    await runMergedIntegrationVerification(job, project, plan, promptPath, gatePolicy.integrationVerificationCommands, next.branch)
    showFlowNotification({ kind: 'group-complete', projectName: project.name, branch: next.branch, index: progress.branches.indexOf(next) + 1, total: progress.branches.length, packages: next.packages })
  }

  const finalPlan = migrationPlanFromPrompt(savedPromptMarkdown, project.name)
  if (!finalPlan) throw new Error('не удалось повторно прочитать Branch plan перед финальной merged verification')
  if (gatePolicy.integrationVerificationCommands.length) {
    const currentBranch = await spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000)
    if (currentBranch.code !== 0 || currentBranch.stdout.trim() !== finalPlan.mergedBranch) {
      const status = await spawnCapture('git', ['-C', project.path, 'status', '--porcelain=v1', '--untracked-files=all'], project.path, 15_000)
      const mergeHead = await spawnCapture('git', ['-C', project.path, 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], project.path, 15_000)
      const mergedExists = await spawnCapture('git', ['-C', project.path, 'show-ref', '--verify', '--quiet', `refs/heads/${finalPlan.mergedBranch}`], project.path, 15_000)
      if (status.code === 0 && !relevantGitStatus(status.stdout) && mergeHead.code !== 0 && mergedExists.code === 0) {
        const switched = await executeCommand(job, { label: `Автовозврат на ${finalPlan.mergedBranch} перед финальной verification`, command: 'git', cwd: project.path, args: ['-C', project.path, 'switch', finalPlan.mergedBranch], captureAgentSession: false })
        if (switched.code !== 0) throw new Error(`MIGRATION_FINAL_VERIFICATION_WRONG_BRANCH: не удалось безопасно переключиться на ${finalPlan.mergedBranch}: ${switched.stderr.trim()}`)
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `Финальная verification ожидала ${finalPlan.mergedBranch}, но checkout был на другой чистой ветке. Переключился автоматически; пользовательское вмешательство не требуется.` })
      } else {
        throw new Error(`MIGRATION_FINAL_VERIFICATION_WRONG_BRANCH: перед финальной integration verification ожидалась ${finalPlan.mergedBranch}, фактически ${currentBranch.stdout.trim() || 'неизвестно'}, а worktree нельзя безопасно переключить автоматически.`)
      }
    }
    const evidenceBranch = finalPlan.branches.length ? finalPlan.branches[finalPlan.branches.length - 1].branch : finalPlan.mergedBranch
    await runMergedIntegrationVerification(job, project, finalPlan, promptPath, gatePolicy.integrationVerificationCommands, evidenceBranch)
  }

  const { issues, feedback } = await agentMigrationIssues(job)
  if (issues.length) throw new Error(`MIGRATION_REPLAN_REQUIRED: финальный migration gate нашёл незавершённые recoverable-факты: ${issues.join('; ')}.${feedback ? `\n\nСостояние: ${feedback}.` : ''}\n\nSupervisor должен использовать фактический Git/checkpoint как вход и продолжить либо сформировать plateau/handoff без красного пользовательского stop.`)
}

type PlannerGitSnapshot = { branch: string; head: string; status: string; refs: string }

async function plannerGitSnapshot(projectPath: string): Promise<PlannerGitSnapshot> {
  const [branch, head, status, mergeHead, refs] = await Promise.all([
    spawnCapture('git', ['-C', projectPath, 'branch', '--show-current'], projectPath, 15_000),
    spawnCapture('git', ['-C', projectPath, 'rev-parse', 'HEAD'], projectPath, 15_000),
    spawnCapture('git', ['-C', projectPath, 'status', '--porcelain=v1', '--untracked-files=all'], projectPath, 15_000),
    spawnCapture('git', ['-C', projectPath, 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], projectPath, 15_000),
    spawnCapture('git', ['-C', projectPath, 'for-each-ref', '--format=%(refname) %(objectname)'], projectPath, 15_000),
  ])
  if (branch.code !== 0 || head.code !== 0 || status.code !== 0 || refs.code !== 0) throw new Error('PLANNER_GIT_STATE_FAILED: не удалось надёжно зафиксировать Git.')
  if (mergeHead.code === 0) throw new Error('PLANNER_REPLAN_UNSAFE: автоматический replan запрещён во время merge.')
  return { branch: branch.stdout.trim(), head: head.stdout.trim(), status: relevantGitStatus(status.stdout), refs: refs.stdout.trim() }
}

async function runIndependentPlannerWithProgress(job: JobRecord, project: ProjectSpec, failure: string, savedPromptPath: string): Promise<PlannerResult> {
  const root = migrationRootJob(job)
  const previousSource = root.logSource
  root.logSource = { kind: 'planner', id: 'planner', label: 'Planner' }
  const plan = migrationPlanFromPrompt(readFileSync(savedPromptPath, 'utf8'), project.name)
  const planningBranches = plan?.branches.map((branch) => branch.branch) ?? []
  // `planning` is a global Supervisor gate. Branch runtime is marked only so
  // refresh/rendering knows execution is paused; do not repeat a fake
  // per-branch "Planner works here" detail for every cohort.
  for (const branch of planningBranches) setBranchRuntime(job, branch, 'planning')
  try {
    return await runIndependentPlanner(job, project, failure, savedPromptPath)
  } finally {
    root.logSource = previousSource
    const runtime = root.branchRuntime
    for (const branch of planningBranches) {
      if (runtime?.get(branch)?.phase === 'planning') setBranchRuntime(job, branch)
    }
  }
}

async function runIndependentPlanner(job: JobRecord, project: ProjectSpec, failure: string, savedPromptPath: string): Promise<PlannerResult> {
  const before = await plannerGitSnapshot(project.path)
  if (before.status) throw new Error('PLANNER_REPLAN_DIRTY: executor оставил изменения; planner не будет их скрывать.')
  const promptMarkdown = readFileSync(savedPromptPath, 'utf8')
  const cacheKey = plannerResultCacheKey({ projectName: project.name, promptMarkdown, normalizedFailure: normalizedPlannerFailure(failure), git: before })
  const cachePath = plannerResultCachePath(join(app.getPath('userData'), 'planner-results', job.workspace.id), cacheKey)
  const cached = readPlannerResult(cachePath)
  if (cached) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Planner machine-result переиспользован после перезапуска: status=${cached.status}; prompt, failure и Git snapshot совпадают.` })
    return cached
  }
  const plannerDir = join(app.getPath('temp'), `dependency-flow-planner-${job.id}-${Date.now()}`)
  const added = await spawnCapture('git', ['-C', project.path, 'worktree', 'add', '--detach', plannerDir, before.head], project.path, 120_000)
  if (added.code !== 0) throw new Error(`PLANNER_WORKTREE_FAILED: ${added.stderr.trim()}`)
  let machine: PlannerResult | undefined
  let cacheable = false
  try {
    const isolatedPromptPath = join(plannerDir, '.dependency-flow-planner-saved-prompt.md')
    const resultPath = join(plannerDir, '.dependency-flow-planner-result.json')
    writeFileSync(isolatedPromptPath, promptMarkdown, 'utf8')
    const plannerPrompt = buildPlannerPrompt({ projectName: project.name, projectPath: plannerDir, failure, savedPromptPath: isolatedPromptPath, resultPath })
    const plannerPromptPath = join(plannerDir, '.dependency-flow-planner-task.md')
    writeFileSync(plannerPromptPath, plannerPrompt, 'utf8')
    send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Executor остановлен gate. Запускаю независимую planner-сессию в detached worktree.' })
    const plannerAttempts = Math.min(2, autonomyPolicy(readSettings(job.workspace), project.name).maxAgentAttemptsPerBatch)
    let lastExit = 0
    for (let attempt = 1; attempt <= plannerAttempts; attempt += 1) {
      try { if (existsSync(resultPath)) unlinkSync(resultPath) } catch { /* stale result must never satisfy the next attempt */ }
      const feedback = attempt > 1
        ? `Предыдущая planner-сессия не оставила валидный machine JSON. Это autonomously recoverable protocol failure: перечитай задачу, исследуй только необходимое и обязательно запиши ровно один result JSON по указанному schema. Попытка ${attempt}/${plannerAttempts}.`
        : undefined
      const plannerDbPath = join(plannerDir, `.dependency-flow-opencode-planner-${attempt}.db`)
      const result = await executeCommand(job, {
        ...agentStartSpec(job.workspace.agent, { ...project, path: plannerDir }, plannerPromptPath, plannerPrompt, job.workspace.agentModel, undefined, undefined, feedback),
        label: `Independent migration planner (${attempt}/${plannerAttempts})`,
        captureAgentSession: false,
        timeoutMs: PLANNER_ATTEMPT_TIMEOUT_MS,
        ...(job.workspace.agent === 'opencode' ? { env: { OPENCODE_DB: plannerDbPath } } : {}),
      })
      lastExit = result.code
      machine = readPlannerResult(resultPath)
      if (machine) {
        cacheable = true
        break
      }
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Planner attempt ${attempt}/${plannerAttempts} не оставил валидный machine-result (exit=${result.code}); повторяю в свежем контексте вместо пользовательского stop.` })
    }
    if (!machine) {
      machine = {
        status: 'refresh-plan',
        reason: `Planner protocol не дал machine JSON после ${plannerAttempts} свежих attempts (last exit=${lastExit}). Вместо красного stop оркестратор выполняет детерминированный residual refresh от текущего cumulative merged state.`,
      }
      send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Planner не оставил machine-result. Это больше не пользовательская ошибка: использую deterministic refresh-plan fallback; verifier всё равно проверит новый scope и результат.' })
    }
  } finally {
    await spawnCapture('git', ['-C', project.path, 'worktree', 'remove', '--force', plannerDir], project.path, 120_000)
  }
  const after = await plannerGitSnapshot(project.path)
  if (JSON.stringify(after) !== JSON.stringify(before)) throw new Error('PLANNER_SCOPE_VIOLATION: planner изменила branch, HEAD, refs или основное рабочее дерево.')
  if (cacheable) writePlannerResultCache(cachePath, machine)
  return machine
}

function persistLatestPrompt(workspace: WorkspaceRecord, projectName: string, promptPath: string): void {
  const state = loadState()
  const stored = state.workspaces.find((item) => item.id === workspace.id)
  if (!stored) throw new Error('PLANNER_WORKSPACE_STATE_MISSING: workspace исчез из desktop state.')
  rememberScopedPromptPath(stored, projectName, promptPath)
  rememberScopedPromptPath(workspace, projectName, promptPath)
  saveState(state)
}

function applyPlannerDeferrals(workspace: WorkspaceRecord, projectName: string, packages: readonly string[], reason: string): string[] {
  const requested = [...new Set(packages.map((value) => value.trim()).filter(Boolean))]
  if (!requested.length) return []
  const statePath = artifactPath(workspace, 'dashboardState', '.dependency-roadmap/state/dashboard-state.json')
  let state: Record<string, unknown> = { schemaVersion: 1, packageOverrides: {} }
  if (existsSync(statePath)) {
    try { state = JSON.parse(readFileSync(statePath, 'utf8')) as Record<string, unknown> } catch { /* recreate only the unreadable state below */ }
  }
  const packageOverrides = state.packageOverrides && typeof state.packageOverrides === 'object' && !Array.isArray(state.packageOverrides)
    ? state.packageOverrides as Record<string, unknown>
    : {}
  const projectOverrides = packageOverrides[projectName] && typeof packageOverrides[projectName] === 'object' && !Array.isArray(packageOverrides[projectName])
    ? packageOverrides[projectName] as Record<string, unknown>
    : {}
  const timestamp = new Date().toISOString()
  for (const packageName of requested) {
    // Section-specific dashboard overrides (e.g. dev:@scope/pkg) have higher
    // precedence than the plain package fallback, so stamp both the fallback
    // and every existing section-specific key or the Supervisor deferral could
    // be silently shadowed by an older manual note/group override.
    const keys = new Set([packageName, ...Object.keys(projectOverrides).filter((key) => key.endsWith(`:${packageName}`))])
    for (const key of keys) {
      const previous = projectOverrides[key] && typeof projectOverrides[key] === 'object' && !Array.isArray(projectOverrides[key])
        ? projectOverrides[key] as Record<string, unknown>
        : {}
      projectOverrides[key] = {
        ...previous,
        plannerDefer: true,
        plannerDeferReason: reason,
        plannerDeferSource: 'independent-supervisor',
        plannerDeferUpdatedAt: timestamp,
      }
    }
  }
  packageOverrides[projectName] = projectOverrides
  state.packageOverrides = packageOverrides
  mkdirSync(dirname(statePath), { recursive: true })
  const temporary = `${statePath}.planner-tmp`
  writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, 'utf8')
  renameSync(temporary, statePath)
  return requested
}

function applySupervisorScopeAdditions(workspace: WorkspaceRecord, projectName: string, additions: readonly { package: string; target: string }[], reason: string, target: ClosureTarget): void {
  if (!additions.length) return
  const statePath = artifactPath(workspace, 'dashboardState', '.dependency-roadmap/state/dashboard-state.json')
  let state: Record<string, unknown> = { schemaVersion: 1, packageOverrides: {} }
  if (existsSync(statePath)) state = JSON.parse(readFileSync(statePath, 'utf8')) as Record<string, unknown>
  const packageOverrides = state.packageOverrides && typeof state.packageOverrides === 'object' && !Array.isArray(state.packageOverrides)
    ? state.packageOverrides as Record<string, unknown>
    : {}
  const projectOverrides = packageOverrides[projectName] && typeof packageOverrides[projectName] === 'object' && !Array.isArray(packageOverrides[projectName])
    ? packageOverrides[projectName] as Record<string, unknown>
    : {}
  const timestamp = new Date().toISOString()
  for (const addition of additions) {
    const keys = new Set([addition.package, ...Object.keys(projectOverrides).filter((key) => key.endsWith(`:${addition.package}`))])
    for (const key of keys) {
      const previous = projectOverrides[key] && typeof projectOverrides[key] === 'object' && !Array.isArray(projectOverrides[key])
        ? projectOverrides[key] as Record<string, unknown>
        : {}
      const next = { ...previous }
      delete next.plannerDefer
      delete next.plannerDeferReason
      delete next.plannerDeferSource
      delete next.plannerDeferUpdatedAt
      next.plannerTargetDefault = addition.target
      next[target === 'green' ? 'plannerTargetGreen' : 'plannerTargetYellow'] = addition.target
      next.plannerTargetReason = reason
      next.plannerTargetSource = 'independent-supervisor'
      next.plannerTargetUpdatedAt = timestamp
      projectOverrides[key] = next
    }
  }
  packageOverrides[projectName] = projectOverrides
  state.packageOverrides = packageOverrides
  mkdirSync(dirname(statePath), { recursive: true })
  const temporary = `${statePath}.planner-scope-tmp`
  writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, 'utf8')
  renameSync(temporary, statePath)
}

function clearPlannerDeferrals(workspace: WorkspaceRecord, projectName: string): void {
  const statePath = artifactPath(workspace, 'dashboardState', '.dependency-roadmap/state/dashboard-state.json')
  if (!existsSync(statePath)) return
  try {
    const state = JSON.parse(readFileSync(statePath, 'utf8')) as Record<string, unknown>
    const packageOverrides = state.packageOverrides as Record<string, unknown> | undefined
    const projectOverrides = packageOverrides?.[projectName] as Record<string, unknown> | undefined
    if (!projectOverrides) return
    let changed = false
    for (const [packageName, raw] of Object.entries(projectOverrides)) {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
      const entry = { ...(raw as Record<string, unknown>) }
      const plannerKeys = ['plannerDefer', 'plannerDeferReason', 'plannerDeferSource', 'plannerDeferUpdatedAt', 'plannerTargetDefault', 'plannerTargetYellow', 'plannerTargetGreen', 'plannerTargetReason', 'plannerTargetSource', 'plannerTargetUpdatedAt']
      if (!plannerKeys.some((key) => key in entry)) continue
      for (const key of plannerKeys) delete entry[key]
      projectOverrides[packageName] = entry
      changed = true
    }
    if (!changed) return
    const temporary = `${statePath}.planner-clear-tmp`
    writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, 'utf8')
    renameSync(temporary, statePath)
  } catch {
    // A malformed state is a diagnostics problem, not a reason to mutate it.
  }
}

function clearEphemeralToolWorktreeDeferrals(workspace: WorkspaceRecord, projectName: string): string[] {
  const statePath = artifactPath(workspace, 'dashboardState', '.dependency-roadmap/state/dashboard-state.json')
  if (!existsSync(statePath)) return []
  try {
    const state = JSON.parse(readFileSync(statePath, 'utf8')) as Record<string, unknown>
    const packageOverrides = state.packageOverrides as Record<string, unknown> | undefined
    const projectOverrides = packageOverrides?.[projectName] as Record<string, unknown> | undefined
    if (!projectOverrides) return []
    const cleared = new Set<string>()
    let changed = false
    for (const [key, raw] of Object.entries(projectOverrides)) {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
      const entry = { ...(raw as Record<string, unknown>) }
      const reason = String(entry.plannerDeferReason ?? '')
      if (entry.plannerDefer !== true || !/PARALLEL_USER_WORKTREE_BLOCKED/i.test(reason)) continue
      // Worktree occupancy is ephemeral orchestration state and must never be
      // persisted as a package-level compatibility decision. Old versions did
      // persist it, so a planner kept reading "user-owned worktree" long after
      // the current runtime could reclaim that exact DepLoom temp path.
      // Only auto-clear legacy reasons that visibly point into our own temp
      // namespace; a genuinely external/user worktree remains protected.
      const candidatePath = toolManagedWorktreeFromLegacyDeferral(reason, app.getPath('temp'))
      if (!candidatePath) continue
      delete entry.plannerDefer
      delete entry.plannerDeferReason
      delete entry.plannerDeferSource
      delete entry.plannerDeferUpdatedAt
      projectOverrides[key] = entry
      changed = true
      cleared.add(key.includes(':') ? key.slice(key.indexOf(':') + 1) : key)
    }
    if (!changed) return []
    const temporary = `${statePath}.planner-ephemeral-clear-tmp`
    writeFileSync(temporary, `${JSON.stringify(state, null, 2)}
`, 'utf8')
    renameSync(temporary, statePath)
    return [...cleared]
  } catch {
    return []
  }
}

function automaticSupervisorDeferralCandidates(
  workspace: WorkspaceRecord,
  project: ProjectSpec,
  plan: MigrationPlan,
  failure: string,
): string[] {
  const allowed = new Set(plan.branches.flatMap((branch) => branch.packages))
  const closure = readTargetClosure(workspace, project, 'yellow')
  const remaining = new Set((closure?.remainingPackages ?? []).filter((name) => allowed.has(name)))
  const mentioned = plan.branches.filter((branch) => failure.includes(branch.branch))
  const candidates = mentioned.flatMap((branch) => branch.packages).filter((name) => allowed.has(name) && (!remaining.size || remaining.has(name)))
  if (candidates.length) return [...new Set(candidates)]
  if (remaining.size) return [...remaining]
  // Last-resort autonomous narrowing is intentionally limited to the smallest
  // still-actionable branch.  It never changes exclusions/health and is fully
  // reversible on the next baseline/replan.
  const actionable = plan.branches
    .map((branch) => branch.packages.filter((name) => allowed.has(name)))
    .filter((packages) => packages.length)
    .sort((left, right) => left.length - right.length)[0]
  return actionable ? [...new Set(actionable)] : []
}

function markAutonomyPlateau(job: JobRecord, reason: string): void {
  job.autonomyPlateauReason = reason
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: `Supervisor исчерпал безопасные варианты текущего executable plan без аварийной остановки. ${reason} FLOW сохранит лучший зелёный merged и продолжит к best-effort финализации, если release gates это позволяют.`,
  })
}

async function refreshMigrationPlan(job: JobRecord, project: ProjectSpec, previousPromptPath: string, failure: string): Promise<void> {
  // Replanning is a semantic operation. Infrastructure failures carry zero
  // evidence about dependency compatibility and must never trigger generator
  // regeneration / Z3. Let the migration-stage infrastructure retry restore
  // the transport and resume the exact immutable plan instead.
  const failureClassification = classifyFlowRecovery(failure, 'agent')
  if (failureClassification.kind === 'infrastructure') {
    throw new Error(failure)
  }
  const previousMarkdown = readFileSync(previousPromptPath, 'utf8')
  const previousPlan = migrationPlanFromPrompt(previousMarkdown, project.name)
  const previousManifest = migrationScopeManifestFromPrompt(previousMarkdown)
  if (!previousPlan || !previousManifest) throw new Error('PLANNER_REPLAN_INPUT_INVALID: prompt не содержит plan/manifest.')
  const autonomy = autonomyPolicy(readSettings(job.workspace), project.name)
  const clearedEphemeralDeferrals = clearEphemeralToolWorktreeDeferrals(job.workspace, project.name)
  if (clearedEphemeralDeferrals.length) {
    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: `Удалил устаревший package-deferral от собственного temp worktree: ${clearedEphemeralDeferrals.join(', ')}. Worktree ownership будет проверяться live; перестраиваю residual без вызова Planner-модели.`,
    })
  }
  const failurePointsAtOwnedWorktree = Boolean(toolManagedWorktreeFromLegacyDeferral(failure, app.getPath('temp')))
  const deterministic = deterministicPlannerDecision(previousPlan, failure, {
    allowResidualPlanDeferral: autonomy.allowResidualPlanDeferral,
    ownedWorktree: clearedEphemeralDeferrals.length > 0 || failurePointsAtOwnedWorktree,
  })
  let planner: PlannerResult
  if (deterministic) {
    planner = deterministic.result
    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: deterministic.kind === 'defer-cohort'
        ? `Deterministic Supervisor: модель не нужна. Временно откладываю доказанно красный cohort ${planner.deferPackages?.join(', ') || 'unknown'}; partial worktree сохранён, продолжаю siblings.`
        : /DEPENDENCY_COMPATIBILITY_EVIDENCE/.test(failure)
          ? 'Deterministic Supervisor: post-Executor dependency evidence не идёт в LLM Planner. Воспроизвожу/localize-ю его и пересчитываю exact Z3.'
          : /DEPENDENCY_MATERIALIZATION_(?:RECONCILE_REQUIRED|CONFLICT|POSTCONDITION)/.test(failure)
            ? 'Deterministic Supervisor: materialization state требует deterministic reconciliation/Baseline refresh; LLM dependency repair не запускается.'
            : 'Deterministic Supervisor: stale/owned worktree state однозначен; перестраиваю residual plan без model-сессии.',
    })
  } else {
    try {
      planner = await runIndependentPlannerWithProgress(job, project, failure, previousPromptPath)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (!message.startsWith('PLANNER_WORKTREE_FAILED:')) throw error
      planner = { status: 'refresh-plan', reason: `Independent Planner worktree could not be created (${message}). This does not block FLOW: fall back to deterministic residual regeneration; verifier remains authoritative.` }
      send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Planner worktree недоступен. Не показываю пользователю stop: выполняю deterministic residual refresh без Planner-модели; все scope/verification gates сохраняются.' })
    }
  }

  if (planner.status === 'blocked' && !planner.deferPackages?.length && autonomy.allowResidualPlanDeferral) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Planner вернул blocked без deferPackages. Запускаю независимый residual-audit: blocked допустим только после доказательства, что безопасного остаточного плана нет.' })
    const auditFailure = `${failure}

## Previous planner machine result requires an independent residual audit

Previous status: blocked
Previous reason: ${planner.reason}

Do not repeat the previous classification. Check whether any subset of the current immutable branch can still verify green. If a safe residual exists, status MUST be defer-blockers and deferPackages MUST contain only the blocking targets. Return blocked only if no reproducible residual plan exists even after deferring those targets.`
    const audited = await runIndependentPlannerWithProgress(job, project, auditFailure, previousPromptPath)
    if (audited.status !== 'blocked' || audited.deferPackages?.length) {
      planner = audited
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Residual-audit скорректировал planner result: status=${planner.status}, deferPackages=${planner.deferPackages?.join(', ') || 'none'}.` })
    }
  }
  let residualPlanAuthorized = false
  let bestSafeAdditions: Array<{ package: string; target: string }> = []
  let bestSafeCoverage = -1
  if (job.target !== 'green') {
    const capacity = readTargetClosure(job.workspace, project, 'yellow')
    const maxFullPlanAttempts = Math.min(2, autonomy.maxPlannerRevisions)
    for (let correction = 0; planner.status === 'expand-plan' && capacity?.planCanReachYellow === false; correction += 1) {
      const proposals = validateSupervisorScopeAdditions(previousMarkdown, project.name, planner.proposedScopeAdditions ?? [])
      const coverage = scopeExpansionCoverage(capacity, proposals.accepted)
      if (coverage.covered > bestSafeCoverage || (coverage.covered === bestSafeCoverage && proposals.accepted.length > bestSafeAdditions.length)) {
        bestSafeCoverage = coverage.covered
        bestSafeAdditions = proposals.accepted
      }
      if (coverage.covered >= coverage.required) break
      if (correction >= maxFullPlanAttempts - 1) {
        if (!autonomy.allowResidualPlanDeferral || !bestSafeAdditions.length) {
          const fallback = autonomy.autoDeferApprovalBlockers ? automaticSupervisorDeferralCandidates(job.workspace, project, previousPlan, failure) : []
          if (fallback.length && autonomy.allowResidualPlanDeferral) {
            const deferred = applyPlannerDeferrals(job.workspace, project.name, fallback, `Full-plan search исчерпан без безопасного expansion: ${planner.reason}`)
            planner = { ...planner, status: 'defer-blockers', deferPackages: deferred, proposedScopeAdditions: [], reason: `${planner.reason} Full-plan search исчерпан; текущий blocker/cohort автоматически отложен.` }
            send('flow:job-output', { jobId: job.id, stream: 'system', line: `Planner не нашёл полного Yellow expansion за ${maxFullPlanAttempts} попытки. Вместо красного stop откладываю ${deferred.join(', ')} и продолжаю residual plan.` })
            break
          }
          markAutonomyPlateau(job, `Planner не предложил ни полного, ни безопасного residual plan после ${maxFullPlanAttempts} независимых попыток.`)
          planner = { ...planner, status: 'blocked', proposedScopeAdditions: [], reason: planner.reason }
          break
        }
        residualPlanAuthorized = true
        planner = {
          ...planner,
          status: 'expand-plan',
          proposedScopeAdditions: bestSafeAdditions.map((item) => item.package + '@' + item.target),
          reason: planner.reason + ' Full Yellow доказанно недостижим без запрещённого/несовместимого scope; выполняется максимальный безопасный residual plan.',
        }
        send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Полный Yellow-plan недостижим после ' + maxFullPlanAttempts + ' независимых попыток. Запускаю лучший безопасный residual: ' + bestSafeAdditions.map((item) => item.package + '@' + item.target).join(', ') + '. Explicit exclusions и отклонённые additions не применяются.' })
        break
      }
      const missing = coverage.missingPackages.slice(0, 12).join(', ')
      send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Supervisor proposal неполный: для Yellow нужно ещё ' + coverage.required + ' lag-fix, предложение доказанно закрывает ' + coverage.covered + '. Запрашиваю один полный replacement plan перед safe-residual fallback.' })
      const correctionFailure = failure + '\n\nGOAL_CLOSURE_PROPOSAL_INSUFFICIENT: previous proposedScopeAdditions cover only ' + coverage.covered + ' of ' + coverage.required + ' required additional lag-policy rows. Return a COMPLETE replacement proposedScopeAdditions list in one response. Candidate lag blockers include: ' + missing + '. Companions that do not themselves reach each package lag-policy minimum do not count toward the required total. Never include an explicit exclusion.'
      planner = await runIndependentPlannerWithProgress(job, project, correctionFailure, previousPromptPath)
    }
    if (planner.status !== 'expand-plan' && bestSafeAdditions.length && autonomy.allowResidualPlanDeferral) {
      residualPlanAuthorized = true
      planner = {
        ...planner,
        status: 'expand-plan',
        proposedScopeAdditions: bestSafeAdditions.map((item) => item.package + '@' + item.target),
        reason: planner.reason + ' Full Yellow недостижим; выполняется лучший ранее доказанный безопасный residual plan.',
      }
    }
  }
  if (planner.executorGuidance) send('flow:job-output', { jobId: job.id, stream: 'system', line: `Planner guidance: ${planner.executorGuidance}` })

  if (planner.status === 'expand-plan') {
    const proposals = validateSupervisorScopeAdditions(previousMarkdown, project.name, planner.proposedScopeAdditions ?? [])
    if (!autonomy.allowSupervisorScopeExpansion || !proposals.accepted.length) {
      const details = proposals.rejected.length ? proposals.rejected.join('; ') : 'supervisor не вернул допустимого existing direct package@exact-semver'
      if (autonomy.autoDeferApprovalBlockers && autonomy.allowResidualPlanDeferral) {
        const fallback = automaticSupervisorDeferralCandidates(job.workspace, project, previousPlan, failure)
        if (fallback.length) {
          const deferred = applyPlannerDeferrals(job.workspace, project.name, fallback, `Автономный fallback вместо approval: ${details}. ${planner.reason}`)
          planner = { ...planner, status: 'defer-blockers', deferPackages: deferred, proposedScopeAdditions: [], reason: `${planner.reason} Запрещённые additions не применены; blocker/cohort временно отложен автоматически.` }
          send('flow:job-output', { jobId: job.id, stream: 'system', line: `Approval не требует пользователя: explicit exclusions/опасные additions сохранены, а ${deferred.join(', ')} временно отложены из executable plan. Health не меняется; Supervisor продолжит residual plan.` })
        } else {
          markAutonomyPlateau(job, `Planner запросил недопустимое расширение (${details}), но в текущем immutable plan больше нечего безопасно defer.`)
          planner = { ...planner, status: 'blocked', proposedScopeAdditions: [], reason: planner.reason }
        }
      } else {
        throw new Error(`MIGRATION_PLAN_APPROVAL_REQUIRED: безопасное автоматическое расширение невозможно: ${details}. ${planner.reason}`)
      }
    }
    if (proposals.rejected.length && !residualPlanAuthorized && planner.status === 'expand-plan') {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Supervisor отбросил запрещённые additions без красного stop: ' + proposals.rejected.join('; ') + '. Они не применяются к manifest/exclusions.' })
    }
    if (proposals.rejected.length) {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Safe-residual отбросил запрещённые additions без изменения exclusions: ' + proposals.rejected.join('; ') + '.' })
    }
    if (planner.status === 'expand-plan' && proposals.accepted.length) {
      applySupervisorScopeAdditions(job.workspace, project.name, proposals.accepted, planner.reason, job.target === 'green' ? 'green' : 'yellow')
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Supervisor расширил план на ${proposals.accepted.length} существующих direct dependencies: ${proposals.accepted.map((item) => `${item.package}@${item.target}`).join(', ')}. Это не approval bypass: registry, peer, group и merged verification выполняются заново.` })
    }
  }

  const allowedPackages = new Set(previousPlan.branches.flatMap((branch) => branch.packages))
  const deferrals = partitionPlannerDeferrals(previousMarkdown, project.name, allowedPackages, planner.deferPackages ?? [])
  if (deferrals.rejected.length) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Supervisor проигнорировал невалидные deferPackages вместо остановки всего FLOW: ' + deferrals.rejected.join(', ') + '.' })
  }
  if (deferrals.ignored.length) {
    send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Supervisor проигнорировал безопасный no-op defer для уже неисполняемых packages: ' + deferrals.ignored.join(', ') + '.' })
  }
  const requestedDeferrals = deferrals.apply
  // Deferrals are internal execution control, not exclusions and not health
  // forgiveness. A risky/new architectural addition can therefore stay out of
  // the executable residual plan while the rest of the verified work keeps
  // moving; only genuinely destructive/safety-invalid Git state reaches the
  // user as a hard stop.
  if (requestedDeferrals.length) {
    const deferred = applyPlannerDeferrals(job.workspace, project.name, requestedDeferrals, planner.reason)
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Supervisor временно отложил ${deferred.length} недостижимых target без исключения их из health: ${deferred.join(', ')}. Пересчитываю остаточный план и продолжаю FLOW.` })
  }

  // Even an over-conservative planner cannot veto a deterministic generator
  // repair.  Regenerate first; only ask for approval/block if the resulting
  // machine plan is still unchanged/unsafe.
  const mergedRefExists = (await spawnCapture('git', ['-C', project.path, 'show-ref', '--verify', '--quiet', `refs/heads/${previousPlan.mergedBranch}`], project.path, 15_000)).code === 0
  const planningBranch = mergedRefExists ? previousPlan.mergedBranch : previousPlan.baseBranch
  const switched = await executeCommand(job, { label: `Planner: расчёт от фактического ${planningBranch}`, command: 'git', cwd: project.path, args: ['-C', project.path, 'switch', planningBranch], captureAgentSession: false })
  if (switched.code !== 0) throw new Error(`PLANNER_BASE_CHECKOUT_FAILED: ${switched.stderr.trim()}`)
  const generate = actionCommands({ action: 'generate', projectName: project.name }, job.workspace, project)[0]
  const previousTargets = residualStabilityTargets(previousMarkdown, project.name)
  const residualStabilityPath = join(app.getPath('temp'), `dependency-flow-residual-${job.id}-${randomUUID()}.json`)
  writeFileSync(residualStabilityPath, JSON.stringify({
    version: 1,
    projects: { [project.name]: { targets: previousTargets } },
  }, null, 2), 'utf8')
  const compatibilityEvidence = compatibilityEvidencePathFromFailure(failure)
  if (compatibilityEvidence && !existsSync(compatibilityEvidence)) {
    throw new Error(`PLANNER_REGENERATION_FAILED: dependency compatibility evidence file disappeared before deterministic re-solve: ${compatibilityEvidence}`)
  }
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: 'Фаза FLOW: детерминированный пересчёт остаточного плана. Agent execution приостановлен; сейчас работают generator / Solver / Verifier.',
  })
  let generated: { code: number; stderr: string; stdout: string }
  try {
    generated = await executeCommand(job, {
      ...generate,
      args: [
        ...generate.args,
        '--residual-stability-file', residualStabilityPath,
        ...(compatibilityEvidence ? ['--compatibility-evidence-file', compatibilityEvidence] : []),
      ],
      label: compatibilityEvidence
        ? 'Planner: deterministic compatibility evidence → localize → Z3 re-solve'
        : 'Planner: детерминированный residual-пересчёт без target churn',
      captureAgentSession: false,
    })
  } finally {
    try { unlinkSync(residualStabilityPath) } catch { /* best-effort temp cleanup */ }
  }
  const nestedBaselineDecision = extractBaselineDecisionEnvelope(generated.stdout, generated.stderr)
  if (generated.code !== 0 && nestedBaselineDecision) {
    send('flow:job-output', {
      jobId: job.id,
      stream: 'system',
      line: 'Fast Baseline внутри residual replan дошёл до decision boundary. Не классифицирую это как infrastructure/planner failure и не перезапускаю Agent: FLOW приостанавливается для выбора dependency group.',
    })
    throw new Error(`MIGRATION_BASELINE_DECISION_REQUIRED: ${nestedBaselineDecision}`)
  }
  if (generated.code !== 0) throw new Error(`PLANNER_REGENERATION_FAILED: ${generated.stderr.trim() || generated.stdout.trim()}`)
  send('flow:job-output', {
    jobId: job.id,
    stream: 'system',
    line: compatibilityEvidence
      ? `Post-Executor evidence передан deterministic Verifier (${compatibilityEvidence}). Только воспроизведённый/localized structural nogood может изменить Z3 assignment; LLM Planner версии не выбирает.`
      : `Residual stability передала planner ${Object.keys(previousTargets).length} прежних target(s): уже merged версии hard-fixed, pending targets сохранены как приоритет до появления нового hard constraint.`,
  })
  const targetMode = previousManifest.targetMode === 'default' || previousManifest.targetMode === 'yellow' || previousManifest.targetMode === 'green' ? previousManifest.targetMode : job.target === 'green' ? 'green' : 'yellow'
  const dashboardUrl = `${DASHBOARD_SCHEME}://dashboard/${encodeURIComponent(job.workspace.id)}?desktop-export=1&project=${encodeURIComponent(project.name)}&v=${Date.now()}`
  let nextMarkdown = await buildProjectPrompt(dashboardUrl, project.name, targetMode)
  // A replan after one or more successful merges must never recreate work from
  // the historical base. The roadmap above was generated from the cumulative
  // merged checkout, so bind every fresh residual branch to that exact state.
  // scopeBranch preserves the Dashboard group identity used by group-scoped
  // prompt export; only the Git execution branch becomes a continuation.
  if (mergedRefExists) {
    const generatedPlan = migrationPlanFromPrompt(nextMarkdown, project.name)
    if (generatedPlan?.branches.length) {
      const refsResult = await spawnCapture('git', ['-C', project.path, 'for-each-ref', '--format=%(refname:short)', 'refs/heads', 'refs/remotes/origin'], project.path, 15_000)
      if (refsResult.code !== 0) throw new Error(`PLANNER_GIT_STATE_FAILED: не удалось прочитать refs перед residual continuation plan. ${refsResult.stderr.trim()}`)
      const refs = refsResult.stdout.split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
      const residualPlan = continuationMigrationPlan(generatedPlan, new Set(generatedPlan.branches.map((branch) => branch.branch)), refs)
      const rewritten = replaceMigrationPlanInPrompt(nextMarkdown, project.name, residualPlan)
      if (!rewritten) throw new Error('PLANNER_REGENERATION_FAILED: не удалось привязать residual plan к cumulative merged branch.')
      nextMarkdown = rewritten
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Residual roadmap рассчитан по ${previousPlan.mergedBranch}; ${residualPlan.branches.length} новых work branches будут созданы как continuation от уже зелёного cumulative дерева.` })
    }
  }
  const plansDir = join(job.workspace.path, '.dependency-roadmap', 'desktop', 'plans')
  mkdirSync(plansDir, { recursive: true })
  const candidatePath = join(plansDir, `${new Date().toISOString().replace(/[:.]/g, '-')}-${project.name.replace(/[^a-zA-Z0-9._-]+/g, '-')}-planner.md`)
  writeFileSync(candidatePath, nextMarkdown, 'utf8')
  const assessment = assessPromptRevision(previousMarkdown, nextMarkdown, project.name)
  if (!assessment.safe) {
    if (autonomy.autoDeferApprovalBlockers && autonomy.allowResidualPlanDeferral && ['approval-required', 'blocked', 'defer-blockers'].includes(planner.status)) {
      markAutonomyPlateau(job, `${planner.reason}. Небезопасная candidate revision не принята (${assessment.reason}); предыдущий проверенный prompt остаётся источником истины.`)
      return
    }
    const code = assessment.additions.length ? 'MIGRATION_PLAN_APPROVAL_REQUIRED' : 'MIGRATION_REPLAN_STALLED'
    throw new Error(`${code}: ${assessment.reason}. Candidate: ${candidatePath}${assessment.additions.length ? `. Изменения: ${assessment.additions.join(', ')}` : ''}`)
  }
  persistLatestPrompt(job.workspace, project.name, candidatePath)
  const revisedCapacity = readTargetClosure(job.workspace, project, job.target === 'green' ? 'green' : 'yellow')
  if (job.target !== 'green' && revisedCapacity?.planCanReachYellow === false) {
    if (residualPlanAuthorized && revisedCapacity.remainingPackages.length) {
      job.residualExecutionPromptPath = candidatePath
      send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Goal-capacity остаётся ниже Yellow, но полный plan доказанно недостижим без запрещённого scope. Разрешаю один запуск безопасного residual (' + revisedCapacity.remainingPackages.length + ' actions); после него обязателен новый audit.' })
      return
    }
    send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Planner revision пока недостаточна: фактический executable plan даёт максимум ' + (revisedCapacity.maxLagOkPctAfterPlan?.toFixed(1) ?? '?') + '%, не хватает ещё ' + (revisedCapacity.neededBeyondCurrentPlan ?? '?') + ' lag-fix. Executor запрещён; Supervisor продолжает собирать полный план.' })
    return
  }
  send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Planner revision принята автоматически: ' + assessment.reason + '. Goal-capacity проверена по фактическому executable roadmap; FLOW продолжает migration.' })
}

async function migrationOutcomeSignature(job: JobRecord, project: ProjectSpec): Promise<string> {
  const promptPath = promptPathForProject(job.workspace, project.name)
  const prompt = promptPath && existsSync(promptPath) ? readFileSync(promptPath, 'utf8') : ''
  const plan = prompt ? migrationPlanFromPrompt(prompt, project.name) : undefined
  const ref = plan?.mergedBranch || 'HEAD'
  const commitResult = await spawnCapture('git', ['-C', project.path, 'rev-parse', ref], project.path, 15_000)
  const commit = commitResult.code === 0 ? commitResult.stdout.trim() : ref
  const closure = readTargetClosure(job.workspace, project, job.target === 'green' ? 'green' : 'yellow')
  return `commit=${commit}:lag=${closure?.lagOk ?? '?'}/${closure?.total ?? '?'}:critical=${closure?.critical ?? '?'}:high=${closure?.high ?? '?'}`
}

async function runMigrationAgentLoop(job: JobRecord): Promise<void> {
  const project = findProject(job.workspace, job.projectName as string)
  const autonomy = autonomyPolicy(readSettings(job.workspace), project.name)
  const repeats = new Map<string, number>()
  const outcomeRepeats = new Map<string, number>()
  job.autonomyPlateauReason = undefined

  for (let revision = 0; revision <= autonomy.maxPlannerRevisions; revision += 1) {
    const capacity = readTargetClosure(job.workspace, project, job.target === 'green' ? 'green' : 'yellow')
    const outcome = await migrationOutcomeSignature(job, project)
    const outcomeSeen = (outcomeRepeats.get(outcome) ?? 0) + 1
    outcomeRepeats.set(outcome, outcomeSeen)
    // Different planner prose/candidate packages are not progress. When the
    // deterministic roadmap already has zero executable actions, one planner
    // pass is enough to try expanding the scope: if Git/health are unchanged
    // on the next loop, a second identical model pass cannot execute anything.
    // With real executable residual work we still allow one alternative path.
    const promptPathForBudget = promptPathForProject(job.workspace, project.name)
    const promptManifestForBudget = promptPathForBudget && existsSync(promptPathForBudget)
      ? migrationScopeManifestFromPrompt(readFileSync(promptPathForBudget, 'utf8'))
      : undefined
    const promptActionRows = typeof promptManifestForBudget?.actionRows === 'number' ? promptManifestForBudget.actionRows : undefined
    const executorScopeEmpty = capacity?.remainingPackages.length === 0 || promptActionRows === 0
    const noProgressRepeatLimit = executorScopeEmpty ? 1 : 2
    if (outcomeSeen > noProgressRepeatLimit) {
      const reason = `Нет фактического Git/health-прогресса после ${outcomeSeen - 1} residual/replan циклов (${outcome}); executor actions=${capacity?.remainingPackages.length ?? promptActionRows ?? '?'}. Следующие candidate combinations не исследуются автоматически.`
      if (autonomy.softStopOnAutonomyPlateau) {
        markAutonomyPlateau(job, reason)
        return
      }
      throw new Error(`MIGRATION_REPLAN_STALLED: ${reason}`)
    }

    // Residual-first autonomy: do useful, already-approved work before asking
    // Supervisor to mathematically close the entire target.  The previous
    // implementation could stop at 76-78% without executing a perfectly safe
    // residual iteration merely because the *future* Yellow plan was not yet
    // complete.
    if (capacity?.remainingPackages.length) {
      if (capacity.planCanReachYellow === false) {
        send('flow:job-output', {
          jobId: job.id,
          stream: 'system',
          line: `Текущий executable residual содержит ${capacity.remainingPackages.length} безопасных actions. Выполняю их до goal-seeking replan; недостающие ${capacity.neededBeyondCurrentPlan ?? '?'} lag-fix Supervisor будет закрывать уже по фактическому новому состоянию.`,
        })
      }
      try {
        job.residualExecutionPromptPath = undefined
        await runMigrationAgentIteration(job)
        return
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        if (!message.startsWith('MIGRATION_REPLAN_REQUIRED:')) throw error
        const promptPath = promptPathForProject(job.workspace, project.name)
        if (!promptPath || !existsSync(promptPath)) throw error
        const scope = promptScopeHash(readFileSync(promptPath, 'utf8')) || promptPath
        const signature = `${scope}:${normalizedPlannerFailure(message)}`
        const seen = (repeats.get(signature) ?? 0) + 1
        repeats.set(signature, seen)
        if (seen > autonomy.maxSamePlanRepeats) {
          if (autonomy.softStopOnAutonomyPlateau && autonomy.allowResidualPlanDeferral) {
            const plan = migrationPlanFromPrompt(readFileSync(promptPath, 'utf8'), project.name)
            const fallback = plan ? automaticSupervisorDeferralCandidates(job.workspace, project, plan, message) : []
            if (fallback.length) {
              applyPlannerDeferrals(job.workspace, project.name, fallback, `Повторяющийся blocker остановлен autonomy budget: ${normalizedPlannerFailure(message)}`)
              send('flow:job-output', { jobId: job.id, stream: 'system', line: `Одинаковый blocker повторился ${seen} раз. Вместо красного stop временно откладываю ${fallback.join(', ')} и продолжаю с остаточным планом.` })
              await refreshMigrationPlan(job, project, promptPath, message)
              continue
            }
            markAutonomyPlateau(job, `Одинаковый scope/blocker повторился ${seen} раз; дальнейшее повторение этого пути запрещено budget-защитой.`)
            return
          }
          throw new Error(`MIGRATION_REPLAN_STALLED: один и тот же scope/blocker повторился ${seen} раз без прогресса; дальнейшие попытки остановлены, чтобы не сжигать время/токены. ${message}`)
        }
        if (revision >= autonomy.maxPlannerRevisions) {
          if (autonomy.softStopOnAutonomyPlateau) {
            markAutonomyPlateau(job, `Исчерпан budget ${autonomy.maxPlannerRevisions} materially-different planner revisions. Последний blocker сохранён для handoff: ${normalizedPlannerFailure(message)}`)
            return
          }
          throw new Error(`MIGRATION_REPLAN_EXHAUSTED: исчерпан budget ${autonomy.maxPlannerRevisions} разных planner revisions без зелёного исполнимого плана. Последний blocker: ${message}`)
        }
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `Supervisor replan ${revision + 1}/${autonomy.maxPlannerRevisions}: текущая группа не закрылась, но зелёные siblings/targets сохранены. Ищу residual strategy без пользовательского approval.` })
        await refreshMigrationPlan(job, project, promptPath, message)
        if (job.autonomyPlateauReason) return
        continue
      }
    }

    if (!capacity || capacity.reached) return

    // Only after all currently executable work is exhausted do we ask the
    // Supervisor to close the remaining health gap.  If it cannot do so
    // safely, that is an autonomy plateau/best-effort handoff -- not a crash.
    const promptPath = promptPathForProject(job.workspace, project.name)
    if (!promptPath || !existsSync(promptPath)) throw new Error(targetClosureMessage(capacity))
    if (revision >= autonomy.maxPlannerRevisions) {
      if (autonomy.softStopOnAutonomyPlateau) {
        markAutonomyPlateau(job, `Текущий executable plan исчерпан; Supervisor не нашёл безопасного расширения за ${autonomy.maxPlannerRevisions} revisions. ${targetClosureMessage(capacity)}`)
        return
      }
      throw new Error(`MIGRATION_REPLAN_EXHAUSTED: ${targetClosureMessage(capacity)}`)
    }
    const targetGap = job.target === 'green'
      ? `TARGET_GREEN_PLAN_INSUFFICIENT: executable plan исчерпан, Green пока не достигнут. ${targetClosureMessage(capacity)}`
      : `TARGET_PLAN_INSUFFICIENT: executable plan исчерпан; до Yellow не хватает ${capacity.neededBeyondCurrentPlan ?? '?'} совместимых lag-fix. ${targetClosureMessage(capacity)}`
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Supervisor goal-seeking ${revision + 1}/${autonomy.maxPlannerRevisions}: безопасная работа уже выполнена; теперь ищу следующий residual scope.` })
    await refreshMigrationPlan(job, project, promptPath, `MIGRATION_REPLAN_REQUIRED: ${targetGap}`)
    if (job.autonomyPlateauReason) return
  }
}


function markReleaseRecovered(workspace: WorkspaceRecord, projectName: string, target?: string): void {
  const state = readTeamState(workspace)
  const run = state?.projects[projectName]
  if (!state || !run) return
  const updatedAt = new Date().toISOString()
  const progress = updateFlowProgress(run, 'release', 'passed', target)
  state.updatedAt = updatedAt
  state.projects[projectName] = {
    ...run,
    ...progress,
    updatedAt,
    recovery: undefined,
  }
  const path = teamStatePath(workspace)
  atomicWriteJsonSync(path, state, (_key, value) => value === undefined ? undefined : value)
}

async function handoffPreparedReleaseToMigrationRepair(
  job: JobRecord,
  project: ProjectSpec,
  releaseBranch: string,
  mergedBranch: string,
  sourceCommit: string,
): Promise<void> {
  const [branch, head, mergeHead, unstaged, untracked, stagedTree] = await Promise.all([
    spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000),
    spawnCapture('git', ['-C', project.path, 'rev-parse', 'HEAD'], project.path, 15_000),
    spawnCapture('git', ['-C', project.path, 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], project.path, 15_000),
    spawnCapture('git', ['-C', project.path, 'diff', '--name-only'], project.path, 15_000),
    spawnCapture('git', ['-C', project.path, 'ls-files', '--others', '--exclude-standard'], project.path, 15_000),
    spawnCapture('git', ['-C', project.path, 'write-tree'], project.path, 15_000),
  ])
  if (branch.code !== 0 || branch.stdout.trim() !== releaseBranch) {
    throw new Error(`RELEASE_TO_MIGRATION_HANDOFF_UNSAFE: ожидалась ${releaseBranch}, фактически ${branch.stdout.trim() || 'неизвестно'}.`)
  }
  if (head.code !== 0 || head.stdout.trim() !== sourceCommit) {
    throw new Error(`RELEASE_TO_MIGRATION_HANDOFF_UNSAFE: release HEAD не совпадает с pinned source commit ${sourceCommit.slice(0, 12)}.`)
  }
  if (mergeHead.code === 0) throw new Error('RELEASE_TO_MIGRATION_HANDOFF_UNSAFE: в release worktree обнаружен незавершённый merge.')
  if (unstaged.code !== 0 || untracked.code !== 0 || unstaged.stdout.trim() || untracked.stdout.trim()) {
    throw new Error('RELEASE_TO_MIGRATION_HANDOFF_UNSAFE: кроме подготовленного staged release есть unstaged/untracked изменения; автоматический возврат к migration repair ничего не удаляет.')
  }
  if (stagedTree.code !== 0 || !stagedTree.stdout.trim()) throw new Error('RELEASE_TO_MIGRATION_HANDOFF_UNSAFE: не удалось прочитать staged tree.')

  const treeDiff = await spawnCapture('git', ['-C', project.path, 'diff', '--name-only', stagedTree.stdout.trim(), mergedBranch], project.path, 15_000)
  if (treeDiff.code !== 0) throw new Error(`RELEASE_TO_MIGRATION_HANDOFF_UNSAFE: не удалось сравнить staged release с ${mergedBranch}.`)
  const allowed = [
    '.dependency-roadmap-audit',
    'docs/dependency-upgrades',
    'docs/dependency-upgrades.md',
    'docs/dependency-update-summary',
    'docs/dependency-update-summary.md',
    'docs/dependency-update-review-notes',
    'docs/dependency-update-review-notes.md',
  ]
  const outside = treeDiff.stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).filter((path) => !allowed.some((prefix) => path === prefix || path.startsWith(`${prefix}/`)))
  if (outside.length) {
    throw new Error(`RELEASE_TO_MIGRATION_HANDOFF_UNSAFE: staged release отличается от ${mergedBranch} вне воспроизводимых tool-managed docs/audit: ${outside.slice(0, 20).join(', ')}.`)
  }

  send('flow:job-output', { jobId: job.id, stream: 'system', line: `Release recovery доказал, что дефект находится в ${mergedBranch}. Подготовленный release воспроизводим и не содержит пользовательских unstaged/untracked файлов — возвращаю FLOW в migration integration repair.` })
  const reset = await executeCommand(job, { label: 'Сброс воспроизводимого prepared release перед migration repair', command: 'git', args: ['-C', project.path, 'reset', '--hard', 'HEAD'], cwd: project.path, captureAgentSession: false })
  if (reset.code !== 0) throw new Error(`RELEASE_TO_MIGRATION_HANDOFF_FAILED: ${reset.stderr.trim() || 'git reset failed'}`)
  const checkout = await executeCommand(job, { label: `Возврат на ${mergedBranch} для migration repair`, command: 'git', args: ['-C', project.path, 'switch', mergedBranch], cwd: project.path, captureAgentSession: false })
  if (checkout.code !== 0) throw new Error(`RELEASE_TO_MIGRATION_HANDOFF_FAILED: ${checkout.stderr.trim() || 'git switch failed'}`)
  const removeRelease = await executeCommand(job, { label: `Удаление пустой подготовительной ${releaseBranch}; release будет создан заново после repair`, command: 'git', args: ['-C', project.path, 'branch', '-D', releaseBranch], cwd: project.path, captureAgentSession: false })
  if (removeRelease.code !== 0) throw new Error(`RELEASE_TO_MIGRATION_HANDOFF_FAILED: ${removeRelease.stderr.trim() || 'git branch -D failed'}`)
}

async function runReleaseRecoveryAgent(job: JobRecord, issue: FlowRecoveryIssue): Promise<void> {
  if (!job.projectName || !job.agentNote?.trim()) throw new Error('RECOVERY_PROMPT_REQUIRED: напишите агенту, что нужно проверить/исправить.')
  const project = findProject(job.workspace, job.projectName)
  const promptPath = promptPathForProject(job.workspace, project.name)
  if (!promptPath || !existsSync(promptPath)) throw new Error('RECOVERY_PROMPT_MISSING: не найден сохранённый dependency prompt.')
  const promptMarkdown = readFileSync(promptPath, 'utf8')
  const plan = migrationPlanFromPrompt(promptMarkdown, project.name)
  if (!plan) throw new Error('RECOVERY_PLAN_MISSING: не удалось прочитать Branch plan из сохранённого prompt.')
  const state = readTeamState(job.workspace)?.projects[project.name]
  const releaseBranch = state?.releaseBranch || project.git?.releaseBranch || issue.branch
  if (!releaseBranch) throw new Error('RELEASE_RECOVERY_BRANCH_MISSING: release-ветка не определена.')

  const [branchBefore, headBefore, statusBefore] = await Promise.all([
    spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000),
    spawnCapture('git', ['-C', project.path, 'rev-parse', 'HEAD'], project.path, 15_000),
    spawnCapture('git', ['-C', project.path, 'status', '--porcelain=v1', '--untracked-files=all'], project.path, 15_000),
  ])
  if (branchBefore.code !== 0 || branchBefore.stdout.trim() !== releaseBranch) {
    throw new Error(`RELEASE_RECOVERY_UNSAFE_BRANCH: ожидалась ${releaseBranch}, фактически ${branchBefore.stdout.trim() || 'неизвестно'}.`)
  }
  if (headBefore.code !== 0) throw new Error(`RELEASE_RECOVERY_GIT_STATE_FAILED: ${headBefore.stderr.trim() || 'не удалось прочитать HEAD'}`)
  if (statusBefore.code !== 0 || !relevantGitStatus(statusBefore.stdout)) {
    throw new Error('RELEASE_RECOVERY_NOTHING_TO_REPAIR: release-ветка уже чистая; повторите этап Release-ветка без agent recovery.')
  }
  const sourceCommit = state?.releaseSourceCommit || job.releaseSourceCommit || headBefore.stdout.trim()
  if (headBefore.stdout.trim() !== sourceCommit) {
    throw new Error(`RELEASE_RECOVERY_SCOPE_VIOLATION: HEAD release-ветки ${headBefore.stdout.trim().slice(0, 12)} не совпадает с pinned source commit ${sourceCommit.slice(0, 12)}. Автовосстановление не будет угадывать историю ветки.`)
  }
  const settings = readSettings(job.workspace)
  const autonomy = autonomyPolicy(settings, project.name)
  const releasePolicy = releasePolicyForProject(settings, project.name)
  const toolDir = bundledToolDir()
  const releaseArgs = [join(toolDir, 'dependency_release_branch.py'), '--project-dir', project.path,
    '--source-branch', project.git?.sourceBranch || 'master', '--source-commit', sourceCommit,
    '--merged-branch', plan.mergedBranch, '--release-branch', releaseBranch,
    '--commit-message', releasePolicy.commitMessage]
  const oneOffGate = state?.releaseGateCommand || job.releaseGateCommand
  for (const gateCommand of releaseGateCommands(settings, project.name, oneOffGate)) releaseArgs.push('--gate-command', gateCommand)

  const promptDir = join(app.getPath('userData'), 'release-recovery-prompts')
  mkdirSync(promptDir, { recursive: true })
  const recoveryStem = `${project.name}-${releaseBranch}`.replace(/[^a-zA-Z0-9._-]+/g, '-')
  const recoveryPromptPath = join(promptDir, `${recoveryStem}.md`)
  const recoveryResultPath = join(promptDir, `${recoveryStem}-result.json`)
  const openCodeServerUrl = await ensureOpenCodeServer(job, project.path)
  job.agentProvider = job.workspace.agent
  let feedback = issue.message
  const originalNote = job.agentNote.trim()
  job.agentNote = undefined

  for (let attempt = 1; attempt <= autonomy.maxReleaseRecoveryAttempts; attempt += 1) {
    try { if (existsSync(recoveryResultPath)) unlinkSync(recoveryResultPath) } catch { /* overwritten below */ }
    const recoveryPrompt = buildReleaseRecoveryPrompt({
      projectName: project.name,
      projectPath: project.path,
      releaseBranch,
      mergedBranch: plan.mergedBranch,
      savedPromptPath: promptPath,
      resultPath: recoveryResultPath,
      failure: feedback,
      userNote: originalNote,
    })
    writeFileSync(recoveryPromptPath, recoveryPrompt, 'utf8')
    job.agentSessionId = undefined
    job.stdoutBuffer = ''
    const spec = {
      ...agentStartSpec(job.workspace.agent, project, recoveryPromptPath, recoveryPrompt, job.workspace.agentModel, undefined, openCodeServerUrl),
      label: `Recovery release ${releaseBranch} (попытка ${attempt}/${autonomy.maxReleaseRecoveryAttempts})`,
      captureAgentSession: false,
    }
    const result = await executeCommand(job, spec)
    const machineResult = readReleaseRecoveryResult(recoveryResultPath)
    if (machineResult?.status === 'blocked') {
      throw new Error(`RELEASE_RECOVERY_BLOCKED: ${machineResult.reason}`)
    }
    const agentReport = `${result.stdout}
${result.stderr}`
    // Backwards-compatible fallback for older agents/checkpoints. New runs use
    // the JSON result above so Markdown escaping/truncated CLI tails cannot
    // turn a definitive migration-repair result into three pointless retries.
    const normalizedAgentReport = agentReport.replace(/\\_/g, '_')
    const migrationRepair = /RELEASE_RECOVERY_NEEDS_MIGRATION_REPAIR:\s*([^\r\n]+)/i.exec(normalizedAgentReport)
    const migrationRepairReason = machineResult?.status === 'migration-repair-required'
      ? machineResult.reason
      : migrationRepair?.[1]?.trim()
    const blockedRepair = /RELEASE_RECOVERY_BLOCKED:\s*([^\r\n]+)/i.exec(normalizedAgentReport)
    if (blockedRepair) throw new Error(`RELEASE_RECOVERY_BLOCKED: ${blockedRepair[1].trim()}`)
    const [branchAfter, headAfter, mergeAfter] = await Promise.all([
      spawnCapture('git', ['-C', project.path, 'branch', '--show-current'], project.path, 15_000),
      spawnCapture('git', ['-C', project.path, 'rev-parse', 'HEAD'], project.path, 15_000),
      spawnCapture('git', ['-C', project.path, 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], project.path, 15_000),
    ])
    if (branchAfter.code !== 0 || branchAfter.stdout.trim() !== releaseBranch) {
      throw new Error(`RELEASE_RECOVERY_SCOPE_VIOLATION: агент покинул ${releaseBranch}. Фактически: ${branchAfter.stdout.trim() || 'неизвестно'}.`)
    }
    if (headAfter.code !== 0 || headAfter.stdout.trim() !== sourceCommit) {
      throw new Error('RELEASE_RECOVERY_SCOPE_VIOLATION: агент создал/изменил commit, хотя финальный commit принадлежит оркестратору.')
    }
    if (mergeAfter.code === 0) throw new Error('RELEASE_RECOVERY_SCOPE_VIOLATION: агент начал merge во время release recovery.')
    if (migrationRepairReason) {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Recovery-agent вернул migration-repair-required: ${migrationRepairReason}. Release больше не повторяю; возвращаюсь к merged verification/repair.` })
      await handoffPreparedReleaseToMigrationRepair(job, project, releaseBranch, plan.mergedBranch, sourceCommit)
      await runMigrationAgentLoop(job)
      const repairedRelease = await executeCommand(job, {
        label: 'Новый финальный release после migration integration repair',
        command: 'python', cwd: job.workspace.path, args: releaseArgs, captureAgentSession: false,
      })
      if (repairedRelease.code === 0) {
        markReleaseRecovered(job.workspace, project.name, job.target)
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `Migration repair и повторный release завершены: проверки/hooks зелёные, финальный commit создан на ${releaseBranch}.` })
        return
      }
      const repairedClassification = classifyFlowRecovery(repairedRelease.stderr, 'release')
      if (repairedClassification.kind !== 'agent') throw new Error(repairedRelease.stderr.trim() || `Release после migration repair завершился с кодом ${repairedRelease.code}`)
      feedback = repairedRelease.stderr.trim() || `Release после migration repair завершился с кодом ${repairedRelease.code}`
      continue
    }
    if (!machineResult && result.code === 0) {
      feedback = `Recovery-agent завершился без обязательного machine result ${recoveryResultPath}. Запиши JSON status=repaired|migration-repair-required|blocked; человеческий текст не является управляющим сигналом.`
      continue
    }
    if (result.code !== 0) {
      feedback = `Recovery-agent завершился с кодом ${result.code}${result.stderr.trim() ? `: ${result.stderr.trim().slice(-1600)}` : ''}`
      continue
    }

    const releaseResult = await executeCommand(job, {
      label: 'Повтор финального release после agent recovery',
      command: 'python', cwd: job.workspace.path, args: releaseArgs, captureAgentSession: false,
    })
    if (releaseResult.code === 0) {
      markReleaseRecovered(job.workspace, project.name, job.target)
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Release recovery завершён: проверки и repository hooks прошли, финальный commit создан оркестратором на ${releaseBranch}.` })
      return
    }
    const classified = classifyFlowRecovery(releaseResult.stderr, 'release')
    if (classified.kind !== 'agent') {
      throw new Error(releaseResult.stderr.trim() || `Release retry завершился с кодом ${releaseResult.code}`)
    }
    feedback = releaseResult.stderr.trim() || `Release retry завершился с кодом ${releaseResult.code}`
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Release всё ещё требует recovery после попытки ${attempt}: ${feedback.slice(-1600)}` })
  }
  throw new Error(`RELEASE_RECOVERY_EXHAUSTED: release-ветку ${releaseBranch} не удалось безопасно довести за ${autonomy.maxReleaseRecoveryAttempts} свежих попыток. Git-состояние сохранено.`)
}

async function runRecoveryAgentLoop(job: JobRecord): Promise<void> {
  if (!job.projectName || !job.agentNote?.trim()) throw new Error('RECOVERY_PROMPT_REQUIRED: напишите агенту, что нужно проверить/исправить.')
  const project = findProject(job.workspace, job.projectName)
  const issue = job.recoveryIssue ?? await inferredRecoveryIssue(job.workspace, project)
  if (!issue) throw new Error('RECOVERY_NOT_AVAILABLE: нет сохранённой recoverable-ошибки или подготовленной dirty release-ветки.')
  if (issue.kind === 'hard') throw new Error(`RECOVERY_HARD_STOP: ${issue.code}. Эта ошибка защищает scope/Git-инвариант и не может быть обойдена пользовательским prompt.`)
  if (issue.kind === 'infrastructure') throw new Error(`RECOVERY_INFRASTRUCTURE_BLOCKED: ${issue.code}. Сначала восстановите инфраструктуру/Git-чтение; агентский prompt не может безопасно обойти эту ошибку.`)

  const releaseLike = issue.action === 'release' || issue.code.startsWith('RELEASE_') || issue.code === 'MIGRATION_DONE_WORKTREE_NOT_CLEAN'
  if (releaseLike) {
    await runReleaseRecoveryAgent(job, issue)
    return
  }
  // Migration/merge recovery reuses the normal bounded orchestration, but
  // without clean-start stashing/deleting. The user's note is consumed by the
  // exact group/merge attempt that needs it.
  await runMigrationAgentLoop(job)
}


async function runAutonomousMigrationStage(job: JobRecord): Promise<void> {
  const seen = new Map<string, number>()
  const infrastructureSeen = new Map<string, number>()
  const project = findProject(job.workspace, job.projectName as string)
  let recoveryCycle = 0
  const maxInfrastructureRetries = 3

  while (recoveryCycle <= MAX_AUTONOMOUS_STAGE_RECOVERY_CYCLES) {
    try {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Подготовка agent job для ' + project.name + ': проверяю актуальность prompt и при необходимости экспортирую его из Dashboard.' })
      const promptPath = await ensureCurrentAgentPrompt(job.workspace, project, job.target === 'green' ? 'green' : 'yellow')
      send('flow:job-output', { jobId: job.id, stream: 'system', line: 'Prompt для ' + project.name + ' подготовлен: ' + promptPath })
      if (job.cancelled) throw new Error('Выполнение остановлено.')
      await runMigrationAgentLoop(job)
      return
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (message.includes(BASELINE_DECISION_MARKER)) {
        send('flow:job-output', {
          jobId: job.id,
          stream: 'system',
          line: 'Migration orchestration получила Baseline continuation decision. Infrastructure retry / Agent retry запрещены; возвращаю decision в UI.',
        })
        throw error instanceof Error ? error : new Error(message)
      }
      const classified = classifyFlowRecovery(message, 'agent')

      if (classified.kind === 'infrastructure') {
        const signature = `${classified.code}:${message.replace(/\s+/g, ' ').slice(0, 800)}`
        const count = (infrastructureSeen.get(signature) ?? 0) + 1
        infrastructureSeen.set(signature, count)
        if (count > maxInfrastructureRetries) throw error

        // Throw away only transport/runtime state. Git branches, immutable
        // targets, approved prompt and cumulative merged commits are retained.
        // The next loop re-reads Git facts and resumes remaining branches; it
        // never enters refreshMigrationPlan, so no Baseline/Z3 recomputation.
        stopOpenCodeServer(job)
        job.agentSessionId = undefined
        job.stdoutBuffer = ''
        send('flow:job-output', {
          jobId: job.id,
          stream: 'system',
          line: `Infrastructure recovery ${count}/${maxInfrastructureRetries}: ${classified.code}. Перезапускаю OpenCode transport и продолжаю тот же immutable Branch plan без Planner и без повторного Z3/Baseline.`,
        })
        await new Promise((resolvePromise) => setTimeout(resolvePromise, Math.min(3000, 500 * count)))
        continue
      }

      if (classified.kind !== 'agent' || recoveryCycle >= MAX_AUTONOMOUS_STAGE_RECOVERY_CYCLES) throw error
      const signature = `${classified.code}:${message.replace(/\s+/g, ' ').slice(0, 1000)}`
      const count = (seen.get(signature) ?? 0) + 1
      seen.set(signature, count)
      if (count > 1) throw new Error(`MIGRATION_AUTONOMOUS_RECOVERY_STALLED: одинаковый recovery ${classified.code} повторился без изменения причины. ${message}`)
      recoveryCycle += 1
      job.agentNote = `Autonomous stage recovery (${classified.code}): самостоятельно продолжи с фактического Git/state, устрани recoverable-причину и сохрани все scope/verification инварианты. Исходная ошибка: ${message.slice(0, 1800)}`
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Этап migration автоматически перехватил ${classified.code}: запускаю новый recovery-контекст (${recoveryCycle}/${MAX_AUTONOMOUS_STAGE_RECOVERY_CYCLES}) без возврата управления пользователю.` })
    }
  }
}


function commandAttemptsForAction(action: FlowAction): number {
  // These stages are declarative/idempotent. Retrying the exact failed command
  // handles transient Git/registry/network/process failures equally for
  // Continue and Autopilot. Agent/release own richer recovery loops instead.
  return ['preflight', 'baseline', 'generate', 'generate-all', 'audit', 'commit-state', 'push-workspace'].includes(action) ? 3 : 1
}

function commandSpecForRetry(job: JobRecord, spec: CommandSpec): CommandSpec {
  if (job.action !== 'baseline') return spec
  // An explicit Start-over is a one-shot user decision. Replaying
  // DEPLOOM_BASELINE_RESUME=restart on transient attempt 2/3 would delete the
  // checkpoint produced by attempt 1 and make every retry start from zero.
  // `auto` resumes an exact compatible checkpoint when one exists, and safely
  // starts fresh when the first attempt died before any checkpoint was written.
  return {
    ...spec,
    env: {
      ...spec.env,
      DEPLOOM_BASELINE_RESUME: 'auto',
      DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE: '1',
    },
  }
}

function deterministicWatchdogFailure(result: { code: number; stderr: string; stdout: string }): boolean {
  if (result.code === 124) return true
  const text = `${result.stderr}\n${result.stdout}`
  return /BASELINE_LOCALIZATION_TIMEOUT|HARD_TIMEOUT|HARD_STALL|verification timed out|project check launch failed:.*timed out/i.test(text)
}

function deterministicPythonProgrammingFailure(result: { code: number; stderr: string; stdout: string }): boolean {
  return isDeterministicToolFailure(result)
}

function baselineHumanDecisionRequired(result: { code: number; stderr: string; stdout: string }): boolean {
  return `${result.stderr}\n${result.stdout}`.includes('DEPLOOM_BASELINE_DECISION_V1 ')
}

function nonRetryableDeterministicFailure(result: { code: number; stderr: string; stdout: string }): boolean {
  if (baselineHumanDecisionRequired(result) || deterministicWatchdogFailure(result) || deterministicPythonProgrammingFailure(result)) return true
  const text = `${result.stderr}\n${result.stdout}`
  // These failures describe a completed deterministic proof attempt. Re-running
  // registry discovery + solving + localization cannot make them transiently
  // disappear; doing so only repeats hours of identical work.
  return /BASELINE_RECOVERY_CONTINUE_UNAVAILABLE|BASELINE_RECOVERY_CONCURRENT_RUN|BASELINE_VERIFY_UNKNOWN_ERROR|BASELINE_VERIFY_INCONCLUSIVE_PROJECT_ERROR|BASELINE_PLAN_BROKEN|BASELINE_CONSTRAINT_LOOP_STUCK|BASELINE_CONSTRAINT_BUDGET_EXHAUSTED|BASELINE_VERIFICATION_PLATEAU|BASELINE_VERIFICATION_HARD_BUDGET_EXHAUSTED|BASELINE_VERIFICATION_HARD_SAFETY_LIMIT|EXACT_SOLVER_UNSAT_PROVEN|EXACT_SOLVER_UNKNOWN|EXACT_SOLVER_BUDGET_EXHAUSTED|GLOBAL_EXACT_EXCLUSION_UNSAT_PROVEN|GLOBAL_EXACT_EXCLUSION_BUDGET_EXHAUSTED|GLOBAL_EXACT_EXCLUSION_SOLVER_UNKNOWN|BASELINE_SOLVER_REPEATED_FAILED_ASSIGNMENT|BASELINE_CONSTRAINT_MINIMIZATION_INCONCLUSIVE|BASELINE_NOOP_RESOLVER_INVALID|EXACT_SOLVER_PROOF_REQUIRED|FIXED_INPUT_CONSTRAINT_CONFLICT|HETEROGENEOUS_DIRECT_DEPENDENCY_DECLARATION|FIXED_DEPENDENCY_DECLARATION_MISMATCH|ASSIGNMENT_HETEROGENEOUS_SOURCE_CONFLICT|ASSIGNMENT_TARGETS_FIXED_INPUT|ASSIGNMENT_REMOVES_FIXED_INPUT|PROVEN_ASSIGNMENT_REOPENED|PROVEN_ASSIGNMENT_MUTATED|PROVEN_DEPENDENCY_SOURCE_DIRTY|PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_INVALID|PROVEN_DEPENDENCY_PROOF_IDENTITY_UNAVAILABLE|PROVEN_DEPENDENCY_ENVELOPE_INVALID|FINAL_BASELINE_COMPATIBILITY_INVALID|OBSERVED_RESOLVED_ASSIGNMENT_[A-Z0-9_]+/i.test(text)
}

async function executeCommand(job: JobRecord, spec: CommandSpec): Promise<{ code: number; stderr: string; stdout: string }> {
  const rootJob = job.parallelParent ?? job
  if (job.cancelled || rootJob.cancelled) throw new Error('Выполнение отменено пользователем.')
  if (['opencode', 'codex', 'claude'].includes(spec.command)) {
    const modelIndex = spec.args.indexOf('--model')
    const model = modelIndex >= 0 ? spec.args[modelIndex + 1] : undefined
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Agent execution binding: provider=${spec.command}; model=${model || '<provider-default>'}` })
  }
  send('flow:job-output', { jobId: job.id, stream: 'system', line: `▶ ${spec.label}\n$ ${spec.command} ${spec.args.join(' ')}` })
  let stderr = ''
  let stdout = ''
  const code = await new Promise<number>((resolvePromise, reject) => {
    const pythonPath = join(bundledToolDir(), 'vendor')
    const baseEnv: NodeJS.ProcessEnv = commandEnvironment({ ...process.env, ...spec.env, FORCE_COLOR: '0' })
    let ephemeralOpenCodeDirectory: string | undefined
    let commandEnv: NodeJS.ProcessEnv
    if (spec.command === 'python') {
      commandEnv = { ...baseEnv, PYTHONUNBUFFERED: '1', PYTHONPATH: [pythonPath, baseEnv.PYTHONPATH].filter(Boolean).join(delimiter) }
    } else if (spec.command === 'opencode') {
      if (spec.env?.OPENCODE_DB) {
        commandEnv = openCodeDatabaseEnv(baseEnv, spec.env.OPENCODE_DB)
      } else if (spec.args.includes('--attach')) {
        // The attached CLI is only a client of our sidecar. Giving every
        // concurrent client its own tiny DB prevents CLI startup from becoming
        // a second SQLite writer against the server's session database.
        const clientRuntime = openCodeClientDatabase(job)
        ephemeralOpenCodeDirectory = clientRuntime.directory
        commandEnv = openCodeDatabaseEnv(baseEnv, clientRuntime.databasePath)
      } else {
        commandEnv = openCodeDatabaseEnv(baseEnv, ensureOpenCodeRuntime(job))
      }
    } else {
      commandEnv = baseEnv
    }
    const invocation = resolveSpawnInvocation(spec.command, spec.args, { env: commandEnv })
    const child = spawn(invocation.command, invocation.args, { cwd: spec.cwd, shell: false, detached: processTreeDetached(), windowsHide: true, windowsVerbatimArguments: invocation.windowsVerbatimArguments, env: commandEnv })
    job.child = child
    rootJob.parallelChildren ??= new Set<ChildProcessWithoutNullStreams>()
    rootJob.parallelChildren.add(child)
    let settled = false
    let timedOut = false
    let forceTimer: ReturnType<typeof setTimeout> | undefined
    let lastOutputAt = Date.now()
    let lastStallNoticeAt = 0
    let stallNoticeCount = 0
    let hardStallTriggered = false
    const markOutput = () => {
      if (stallNoticeCount > 0 && Date.now() - lastOutputAt >= (spec.stallWarningMs ?? Number.POSITIVE_INFINITY)) {
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `${spec.label}: вывод возобновился после периода тишины; процесс продолжает работу.` })
      }
      lastOutputAt = Date.now()
    }
    const stallTimer = spec.stallWarningMs ? setInterval(() => {
      const now = Date.now()
      const silentMs = now - lastOutputAt
      if (spec.stallAbortMs && silentMs >= spec.stallAbortMs && !hardStallTriggered) {
        hardStallTriggered = true
        timedOut = true
        send('flow:job-output', {
          jobId: job.id,
          stream: 'stderr',
          line: `[error] ⛔ ${spec.label}: HARD_STALL — нет heartbeat/output ${Math.max(1, Math.floor(silentMs / 60_000))} мин. Это превышает watchdog ${Math.ceil(spec.stallAbortMs / 60_000)} мин; завершаю дерево процессов. State/progress checkpoint сохранён для диагностики.`,
        })
        killProcessTree(child)
        forceTimer = setTimeout(() => finish(124), 10_000)
        return
      }
      if (silentMs < spec.stallWarningMs! || now - lastStallNoticeAt < spec.stallWarningMs!) return
      stallNoticeCount += 1
      lastStallNoticeAt = now
      send('flow:job-output', {
        jobId: job.id,
        stream: 'stderr',
        line: `[warn] ⚠ ${spec.label}: нет новых сообщений ${Math.max(1, Math.floor(silentMs / 60_000))} мин. Процесс ещё запущен; это метка возможного зависания. Внутренний verifier обязан либо прислать heartbeat/progress, либо завершить зависший subprocess по hard timeout.`,
      })
    }, Math.min(spec.stallWarningMs, 60_000)) : undefined
    child.stdout.on('data', (chunk: Buffer) => {
      markOutput()
      const text = decodeProcessOutputChunk(chunk)
      stdout = `${stdout}${text}`.slice(-6000)
      if (spec.captureAgentSession !== false) captureAgentSession(job, text)
      send('flow:job-output', { jobId: job.id, stream: 'stdout', line: text })
    })
    child.stderr.on('data', (chunk: Buffer) => {
      markOutput()
      const text = decodeProcessOutputChunk(chunk)
      if (spec.captureAgentSession !== false) captureAgentSession(job, text)
      stderr = `${stderr}${text}`.slice(-6000)
      send('flow:job-output', { jobId: job.id, stream: 'stderr', line: text })
    })
    const cleanupCommandRuntime = () => {
      if (!ephemeralOpenCodeDirectory) return
      try { rmSync(ephemeralOpenCodeDirectory, { recursive: true, force: true }) } catch { /* isolated client DB will be abandoned and the parent runtime cleanup removes it later */ }
      ephemeralOpenCodeDirectory = undefined
    }
    const finish = (result: number) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      if (forceTimer) clearTimeout(forceTimer)
      if (stallTimer) clearInterval(stallTimer)
      rootJob.parallelChildren?.delete(child)
      cleanupCommandRuntime()
      resolvePromise(result)
    }
    const timer = spec.timeoutMs ? setTimeout(() => {
      timedOut = true
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `${spec.label}: лимит ${Math.ceil(spec.timeoutMs! / 60_000)} мин достигнут; завершаю зависший процесс и продолжу recovery в свежем контексте.` })
      killProcessTree(child)
      forceTimer = setTimeout(() => finish(124), 10_000)
    }, spec.timeoutMs) : undefined
    child.on('error', (error) => {
      if (timedOut) finish(124)
      else {
        if (timer) clearTimeout(timer)
        if (forceTimer) clearTimeout(forceTimer)
        if (stallTimer) clearInterval(stallTimer)
        rootJob.parallelChildren?.delete(child)
        cleanupCommandRuntime()
        const hint = packageManagerResolutionHint(spec.command, invocation.resolvedExecutable)
        if (hint && (error as NodeJS.ErrnoException).code === 'ENOENT') {
          reject(new Error(`INFRA_PACKAGE_MANAGER_NOT_FOUND: ${spec.command}: ${error.message}. ${hint}`))
        } else reject(error)
      }
    })
    child.on('close', (result) => finish(timedOut ? 124 : (result ?? 1)))
    child.stdin.on('error', (error) => {
      const code = (error as NodeJS.ErrnoException).code
      if (code === 'EPIPE' || code === 'ERR_STREAM_DESTROYED') return
      send('flow:job-output', {
        jobId: job.id,
        stream: 'stderr',
        line: `[warn] ${spec.label}: stdin stream error ignored after child launch: ${error.message}`,
      })
    })
    if (spec.stdin) child.stdin.end(spec.stdin)
    else child.stdin.end()
  })
  if (job.cancelled || rootJob.cancelled) throw new Error(job.agentSessionId ? 'Агент остановлен. Сессию можно продолжить с этого места.' : 'Выполнение остановлено.')
  if (spec.command === 'opencode' && code !== 0 && openCodeDatabaseLocked(`${stderr}\n${stdout}`)) {
    throw new Error(`OPENCODE_SQLITE_BUSY: OpenCode CLI не получил доступ к своей runtime DB. Это инфраструктурный сбой; immutable dependency plan остаётся действительным.${stderr.trim() ? `\n\n${stderr.trim()}` : ''}`)
  }
  return { code, stderr, stdout }
}

async function executeJob(job: JobRecord, commands: CommandSpec[]): Promise<void> {
  let exitCode = 0
  let errorMessage = ''
  let teamStateFinalized = false
  try {
    updateTeamState(job, 'running')
    if (job.bestEffortReason) {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Best-effort release: целевой health-level пока не достигнут, но исполнимый migration plan исчерпан. ${job.bestEffortReason}. Финальные project gates и repository hooks НЕ ослабляются.` })
    }
    for (const spec of commands) {
      if (spec.finalizeTeamStateBeforeRun) {
        updateTeamState(job, 'passed')
        teamStateFinalized = true
      }
      if (spec.skipWhenNoStagedChanges) {
        const staged = await spawnCapture('git', ['-C', spec.cwd, 'diff', '--cached', '--quiet'], spec.cwd)
        if (staged.code === 0) {
          send('flow:job-output', { jobId: job.id, stream: 'system', line: `${spec.label}: новых изменений нет, шаг уже актуален.` })
          continue
        }
        if (staged.code > 1) throw new Error(`${spec.label}: не удалось проверить подготовленные изменения. ${staged.stderr.trim()}`)
      }
      const maxCommandAttempts = commandAttemptsForAction(job.action)
      let attemptsPerformed = 1
      let result = await executeCommand(job, spec)
      for (let attempt = 2; result.code !== 0 && !nonRetryableDeterministicFailure(result) && attempt <= maxCommandAttempts; attempt += 1) {
        const retrySpec = commandSpecForRetry(job, spec)
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `${spec.label}: transient retry ${attempt}/${maxCommandAttempts}; предыдущий exit=${result.code}.${job.action === 'baseline' ? ' Повтор использует safe resume=auto; explicit restart повторно не применяется.' : ''}` })
        attemptsPerformed = attempt
        result = await executeCommand(job, retrySpec)
      }
      if (result.code !== 0 && deterministicWatchdogFailure(result)) {
        send('flow:job-output', {
          jobId: job.id,
          stream: 'stderr',
          line: `[error] ${spec.label}: watchdog/timeout считается детерминированным stop-сигналом; полный Baseline автоматически повторно не запускаю. Последний baseline-verification-progress.json сохранён для диагностики.`,
        })
      }
      if (result.code !== 0 && deterministicPythonProgrammingFailure(result)) {
        send('flow:job-output', {
          jobId: job.id,
          stream: 'stderr',
          line: `[error] ${spec.label}: обнаружен внутренний Python programming/import failure; повтор того же установленного кода не выполняю. Исправьте/обновите DepLoom и используйте Baseline Continue для восстановления с durable safe point.`,
        })
      }
      exitCode = result.code
      if (exitCode !== 0) throw new Error(`${spec.label}: команда завершилась с кодом ${exitCode} после ${attemptsPerformed} попыток.${result.stderr.trim() ? `\n\n${result.stderr.trim()}` : ''}`)
    }
    // Preserve the generator output for the project that produced it before
    // another `--only-project` run is allowed to replace the configured files.
    // This is the persistence boundary that makes project switching truly
    // isolated rather than merely filtering renderer state.
    if (job.action === 'baseline' && job.projectName) {
      // Baseline intentionally writes into a project-private output directory
      // so concurrent project Baselines cannot race on shared jsonOut/htmlOut.
      // Snapshot exactly those files; reading the shared configured roadmap
      // here used to turn a successful Baseline into a false failure whenever
      // that shared file happened to contain another project.
      const baselineOutput = baselineProjectOutputDir(job.workspace, job.projectName)
      if (!snapshotProjectArtifacts(job.workspace, job.projectName, {
        roadmap: join(baselineOutput, 'dependency-roadmap.json'),
        dashboard: join(baselineOutput, 'dependency-roadmap.html'),
      })) {
        throw new Error(`PROJECT_ARTIFACT_SNAPSHOT_FAILED: private Baseline roadmap для ${job.projectName} не содержит этот проект.`)
      }
    } else if (job.action === 'generate' && job.projectName) {
      if (!snapshotProjectArtifacts(job.workspace, job.projectName)) {
        throw new Error(`PROJECT_ARTIFACT_SNAPSHOT_FAILED: roadmap для ${job.projectName} не содержит этот проект после generate.`)
      }
    } else if (job.action === 'generate-all') {
      for (const project of readProjects(job.workspace)) snapshotProjectArtifacts(job.workspace, project.name)
    }
    // Baseline is the end of planning epoch creation, not the beginning of
    // execution. Only after the new Baseline has been fully generated and
    // snapshotted do we discard the previous execution epoch. Manual FLOW
    // therefore lands on Step 3; explicit Autopilot may immediately continue
    // to agent because its own stage scheduler is still active.
    if (job.action === 'baseline' && job.projectName) {
      await cleanupSupersededMigrationAfterBaseline(job, findProject(job.workspace, job.projectName))
    }
    if (job.action === 'agent') await runAutonomousMigrationStage(job)
    if (job.action === 'recover') await runRecoveryAgentLoop(job)
    if (job.action === 'release' && job.projectName) {
      await cleanupToolManagedProjectWorktreesAfterRelease(job, findProject(job.workspace, job.projectName))
    }
    exitCode = 0
    if (!teamStateFinalized) updateTeamState(job, 'passed')
  } catch (error) {
    if (job.pauseRequested && job.action === 'baseline') {
      exitCode = 0
      errorMessage = ''
      updateTeamState(job, 'paused')
      send('flow:job-output', { jobId: job.id, stream: 'system', workspaceId: job.workspace.id, projectName: job.projectName, line: 'Baseline paused at the last safe checkpoint. Use Continue to resume, or Start over to reset the checkpoint.' })
      return
    }
    exitCode = exitCode || 1
    errorMessage = error instanceof Error ? error.message : String(error)
    let recovered = false
    if (!job.cancelled && job.action === 'release') {
      const classified = classifyFlowRecovery(errorMessage, 'release')
      if (classified.kind === 'agent') {
        const issue: FlowRecoveryIssue = { code: classified.code, kind: classified.kind, action: 'release', message: errorMessage, branch: job.releaseBranch, updatedAt: new Date().toISOString() }
        job.agentNote = `Autonomous release recovery: исправь recoverable release failure без ослабления gates и верни управление оркестратору. ${errorMessage.slice(0, 1800)}`
        send('flow:job-output', { jobId: job.id, stream: 'system', line: `Этап release автоматически перехватил ${classified.code}: запускаю встроенный release recovery без возврата пользователю.` })
        try {
          await runReleaseRecoveryAgent(job, issue)
          exitCode = 0
          errorMessage = ''
          recovered = true
          updateTeamState(job, 'passed')
        } catch (recoveryError) {
          exitCode = 1
          errorMessage = recoveryError instanceof Error ? recoveryError.message : String(recoveryError)
        }
      }
    }
    if (!recovered) {
      const baselineDecisionRequired = errorMessage.includes(BASELINE_DECISION_MARKER)
      if (baselineDecisionRequired) {
        updateTeamState(job, 'paused')
        job.recoveryIssue = undefined
        send('flow:job-output', {
          jobId: job.id,
          stream: 'system',
          workspaceId: job.workspace.id,
          projectName: job.projectName,
          line: job.action === 'agent'
            ? 'Миграция приостановлена на безопасной decision boundary: выберите предложенную dependency group / измените scope / запустите глубокий поиск.'
            : 'Baseline приостановлен на безопасной decision boundary.',
        })
      } else {
        updateTeamState(job, 'failed')
        job.recoveryIssue = await persistRecoveryIssue(job, errorMessage)
      }
    }
  } finally {
    stopOpenCodeServer(job)
    jobs.delete(job.id)
    send('flow:job-finished', { jobId: job.id, action: job.action, workspaceId: job.workspace.id, projectName: job.projectName, exitCode, error: errorMessage })
    // A successful agent job is not necessarily the end of migration while
    // Autopilot is active: the goal-seeker may immediately launch another
    // generate/audit/agent residual cycle. Only manual single-stage runs get a
    // stage-complete OS notification; Autopilot emits one final notification
    // when the whole FLOW really has no next action.
    if (exitCode === 0 && !job.autopilot) showFlowNotification({ kind: 'stage-complete', projectName: job.projectName || job.workspace.name, action: job.action })
  }
}

function isInside(candidate: string, root: string): boolean {
  const normalizedCandidate = normalizePathForComparison(normalize(resolve(candidate)))
  const normalizedRoot = normalizePathForComparison(normalize(resolve(root)))
  const boundary = process.platform === 'win32' ? '\\' : '/'
  return normalizedCandidate === normalizedRoot || normalizedCandidate.startsWith(`${normalizedRoot}${boundary}`)
}

function allowedPath(targetPath: string): boolean {
  const state = loadState()
  if (isInside(targetPath, join(app.getPath('userData'), 'project-artifacts'))) return true
  return state.workspaces.some((workspace) => isInside(targetPath, workspace.path) || readProjects(workspace).some((project) => isInside(targetPath, project.path)))
}

function setupIpc(): void {
  ipcMain.handle('flow:bootstrap', async () => {
    const state = loadState()
    const workspace = selectedWorkspace(state)
    return { state, appVersion: app.getVersion(), environment: await environmentInfo(), details: workspace ? await workspaceDetails(workspace) : undefined, defaults: { templateRemote: DEFAULT_TEMPLATE_REMOTE, toolRemote: DEFAULT_TOOL_REMOTE }, updateStatus: currentUpdateStatus, notificationsEnabled: notificationsEnabled(state), themePreference: currentThemePreference() }
  })

  ipcMain.handle('flow:set-notifications-enabled', (_event, enabled: boolean) => {
    const state = loadState()
    state.notificationsEnabled = Boolean(enabled)
    saveState(state)
    return { enabled: notificationsEnabled(state) }
  })

  ipcMain.handle('flow:notify-autopilot-complete', (_event, input: { projectName?: string; published?: boolean }) => {
    const projectName = typeof input?.projectName === 'string' && input.projectName.trim() ? input.projectName.trim() : 'Проект'
    showFlowNotification({ kind: 'autopilot-complete', projectName, published: input?.published === true })
  })
  ipcMain.handle('flow:check-for-updates', async () => {
    if (!app.isPackaged) return { currentVersion: app.getVersion(), development: true }
    try {
      const result = await checkForLatestUpdate()
      return { currentVersion: app.getVersion(), availableVersion: result?.updateInfo.version }
    } catch (error) {
      const status = publishUpdaterError(error)
      return { currentVersion: app.getVersion(), error: status }
    }
  })

  ipcMain.handle('flow:install-update', installLatestUpdate)
  ipcMain.handle('flow:pick-directory', async () => {
    const result = await dialog.showOpenDialog(mainWindow!, { properties: ['openDirectory', 'createDirectory'] })
    return result.canceled ? undefined : result.filePaths[0]
  })

  ipcMain.handle('flow:pick-file', async (_event, filters?: Array<{ name: string; extensions: string[] }>) => {
    const result = await dialog.showOpenDialog(mainWindow!, { properties: ['openFile'], filters })
    return result.canceled ? undefined : result.filePaths[0]
  })

  ipcMain.handle('flow:list-agent-models', async (_event, agentProvider: AgentProvider, cwd?: string) => {
    try {
      return await listAgentModels(agentProvider, cwd || process.cwd())
    } catch {
      return []
    }
  })

  ipcMain.handle('flow:register-workspace', async (_event, raw: { path: string; name?: string }) => {
    const workspacePath = resolve(raw.path)
    if (!existsSync(join(workspacePath, '.git'))) throw new Error('Выбранная папка не является Git-репозиторием workspace.')
    const state = loadState()
    const existing = state.workspaces.find((item) => normalizePathForComparison(normalize(item.path)) === normalizePathForComparison(normalize(workspacePath)))
    const workspace = existing ?? {
      id: randomUUID(), name: raw.name?.trim() || basename(workspacePath), path: workspacePath,
      templateRemote: DEFAULT_TEMPLATE_REMOTE, toolRemote: DEFAULT_TOOL_REMOTE,
      settingsPath: '.dependency-roadmap/settings.project.json', agent: 'codex' as const,
    }
    if (!existing) state.workspaces.push(workspace)
    state.selectedWorkspaceId = workspace.id
    saveState(state)
    return { state, details: await workspaceDetails(workspace) }
  })

  ipcMain.handle('flow:add-project', async (_event, raw: { workspaceId?: string; name: string; path: string; sourceBranch?: string; baseBranch?: string; mergedBranch?: string }) => {
    const state = loadState()
    const workspace = findWorkspace(state, raw.workspaceId)
    const selectedPath = resolve(raw.path)
    const projectPath = resolveProjectPackageDirectory(selectedPath, true)
    const gitProbe = await spawnCapture('git', ['-C', projectPath, 'rev-parse', '--is-inside-work-tree'], projectPath, 15_000)
    if (gitProbe.timedOut || gitProbe.code !== 0 || gitProbe.stdout.trim().toLowerCase() !== 'true') {
      throw new Error('Папка проекта не находится внутри Git-репозитория.')
    }
    const settingsPath = resolveSettingsPath(workspace)
    const settings = existsSync(settingsPath) ? JSON.parse(readFileSync(settingsPath, 'utf8')) as Record<string, unknown> : { schemaVersion: 1 }
    const projects = Array.isArray(settings.projects) ? settings.projects as ProjectSpec[] : []
    if (projects.some((project) => project.name === raw.name.trim())) throw new Error(`Проект ${raw.name.trim()} уже есть в настройках.`)
    projects.push({
      name: raw.name.trim(), path: projectPath,
      git: {
        sourceBranch: raw.sourceBranch?.trim() || 'master',
        baseBranch: raw.baseBranch?.trim() || 'libs',
        branchPrefix: raw.baseBranch?.trim() || 'libs',
        mergedBranch: raw.mergedBranch?.trim() || `${raw.baseBranch?.trim() || 'libs'}-merged`,
        push: false,
      },
    })
    settings.projects = projects
    mkdirSync(dirname(settingsPath), { recursive: true })
    const temporary = `${settingsPath}.dependency-flow-tmp`
    writeFileSync(temporary, `${JSON.stringify(settings, null, 2)}\n`, 'utf8')
    renameSync(temporary, settingsPath)
    workspace.selectedProject = raw.name.trim()
    saveState(state)
    return { state, details: await workspaceDetails(workspace) }
  })

  ipcMain.handle('flow:remove-project', async (_event, raw: { workspaceId?: string; projectName: string }) => {
    const state = loadState()
    const workspace = findWorkspace(state, raw.workspaceId)
    const projectName = String(raw.projectName || '').trim()
    if (!projectName) throw new Error('Имя проекта обязательно.')
    if ([...jobs.values()].some((job) => job.workspace.id === workspace.id)) {
      throw new Error('Нельзя менять состав workspace во время активной команды. Дождитесь завершения или отмените её.')
    }

    const settingsPath = resolveSettingsPath(workspace)
    if (!existsSync(settingsPath)) throw new Error('settings.project.json не найден.')
    const settings = JSON.parse(readFileSync(settingsPath, 'utf8')) as Record<string, unknown>
    const projects = Array.isArray(settings.projects) ? settings.projects as ProjectSpec[] : []
    const remaining = projects.filter((project) => project.name !== projectName)
    if (remaining.length === projects.length) throw new Error(`Проект ${projectName} не найден в settings.project.json.`)

    settings.projects = remaining
    const temporary = `${settingsPath}.dependency-flow-tmp`
    writeFileSync(temporary, `${JSON.stringify(settings, null, 2)}\n`, 'utf8')
    renameSync(temporary, settingsPath)

    if (workspace.selectedProject === projectName) workspace.selectedProject = remaining[0]?.name
    const removedPromptPath = workspace.latestPromptPaths?.[projectName]
    if (removedPromptPath) {
      const next = { ...workspace.latestPromptPaths }
      delete next[projectName]
      workspace.latestPromptPaths = next
      if (workspace.latestPromptPath === removedPromptPath) workspace.latestPromptPath = undefined
    }
    saveState(state)
    return { state, details: await workspaceDetails(workspace) }
  })

  // Keep the historical IPC name and optional templateRemote input so an old
  // renderer / an already configured installation remains compatible. The new
  // UI does not send templateRemote: in that mode DepLoom bootstraps a
  // complete local workspace itself and no external template repository is
  // required.
  ipcMain.handle('flow:clone-workspace', async (_event, raw: { parentPath: string; folderName: string; teamRemote?: string; templateRemote?: string }) => {
    const parent = resolve(raw.parentPath)
    const folderName = raw.folderName.trim()
    if (!folderName || isAbsolute(folderName)) {
      throw new Error(`WORKSPACE_FOLDER_OUTSIDE_PARENT: invalid folder name ${JSON.stringify(folderName)}`)
    }
    const target = resolve(parent, folderName)
    const relativeTarget = relative(parent, target)
    if (!relativeTarget || relativeTarget === '..' || relativeTarget.startsWith(`..${sep}`) || isAbsolute(relativeTarget)) {
      throw new Error(`WORKSPACE_FOLDER_OUTSIDE_PARENT: ${target} is outside ${parent}`)
    }
    if (existsSync(target)) throw new Error(`Папка уже существует: ${target}`)
    const templateRemote = String(raw.templateRemote || '').trim()
    try {
      if (templateRemote) {
        // Legacy bootstrap path. Existing callers that explicitly provide a
        // template keep the old clone/upstream/origin semantics unchanged.
        const clone = await spawnCapture('git', ['clone', templateRemote, target], raw.parentPath, 120_000)
        if (clone.code !== 0) throw new Error(clone.stderr || 'Не удалось клонировать template.')
        if (raw.teamRemote?.trim()) {
          const rename = await spawnCapture('git', ['-C', target, 'remote', 'rename', 'origin', 'upstream'], target)
          if (rename.code !== 0) throw new Error(rename.stderr)
          const add = await spawnCapture('git', ['-C', target, 'remote', 'add', 'origin', raw.teamRemote.trim()], target)
          if (add.code !== 0) throw new Error(add.stderr)
        }
      } else {
        await initializeWorkspaceRepository(target, raw.teamRemote?.trim() || undefined, spawnCapture)
      }
    } catch (error) {
      // Creation is transactional from the UI's point of view: a failed first
      // attempt must not leave an unusable folder that blocks the next retry.
      rmSync(target, { recursive: true, force: true })
      throw error
    }

    const state = loadState()
    const workspace: WorkspaceRecord = {
      id: randomUUID(), name: basename(target), path: target,
      templateRemote, toolRemote: DEFAULT_TOOL_REMOTE,
      teamRemote: raw.teamRemote?.trim() || undefined,
      settingsPath: '.dependency-roadmap/settings.project.json', agent: 'codex',
    }
    state.workspaces.push(workspace)
    state.selectedWorkspaceId = workspace.id
    saveState(state)
    return { state, details: await workspaceDetails(workspace) }
  })

  ipcMain.handle('flow:select-workspace', async (_event, workspaceId: string) => {
    const state = loadState()
    const workspace = findWorkspace(state, workspaceId)
    state.selectedWorkspaceId = workspace.id
    saveState(state)
    return { state, details: await workspaceDetails(workspace) }
  })

  ipcMain.handle('flow:update-workspace', async (_event, raw: Partial<WorkspaceRecord> & { id: string }) => {
    const state = loadState()
    const index = state.workspaces.findIndex((item) => item.id === raw.id)
    if (index < 0) throw new Error('Рабочий набор не найден.')
    const current = state.workspaces[index]
    state.workspaces[index] = {
      ...current,
      name: typeof raw.name === 'string' ? raw.name : current.name,
      agent: raw.agent === 'opencode' ? 'opencode' : raw.agent === 'claude' ? 'claude' : raw.agent === 'codex' ? 'codex' : current.agent,
      agentModel: typeof raw.agentModel === 'string' ? (raw.agentModel.trim() || undefined) : current.agentModel,
      selectedProject: typeof raw.selectedProject === 'string' ? raw.selectedProject : current.selectedProject,
      latestPromptPath: typeof raw.latestPromptPath === 'string' ? raw.latestPromptPath : current.latestPromptPath,
      latestPromptPaths: raw.latestPromptPaths && typeof raw.latestPromptPaths === 'object' ? { ...current.latestPromptPaths, ...raw.latestPromptPaths } : current.latestPromptPaths,
    }
    saveState(state)
    return { state, details: await workspaceDetails(state.workspaces[index]) }
  })

  ipcMain.handle('flow:update-project-branches', async (_event, raw: { workspaceId?: string; projectName: string; branchBase?: string; push?: boolean }) => {
    const state = loadState()
    const workspace = findWorkspace(state, raw.workspaceId)
    const branchBase = raw.branchBase?.trim() || 'libs'
    const branchCheck = await spawnCapture('git', ['check-ref-format', '--branch', branchBase], workspace.path)
    if (branchCheck.code !== 0) throw new Error(branchCheck.stderr.trim() || `Некорректное имя ветки: ${branchBase}`)
    const settingsPath = resolveSettingsPath(workspace)
    if (!existsSync(settingsPath)) throw new Error('settings.project.json не найден.')
    const settings = JSON.parse(readFileSync(settingsPath, 'utf8')) as Record<string, unknown>
    const projects = Array.isArray(settings.projects) ? settings.projects as ProjectSpec[] : []
    const projectIndex = projects.findIndex((project) => project.name === raw.projectName)
    if (projectIndex < 0) throw new Error(`Проект ${raw.projectName} не найден в settings.project.json.`)
    projects[projectIndex] = applyBranchBase(projects[projectIndex], branchBase, raw.push)
    settings.projects = projects
    const temporary = `${settingsPath}.dependency-flow-tmp`
    writeFileSync(temporary, `${JSON.stringify(settings, null, 2)}\n`, 'utf8')
    renameSync(temporary, settingsPath)
    return { state, details: await workspaceDetails(workspace) }
  })
  ipcMain.handle('flow:refresh-workspace', async () => {
    const state = loadState()
    const workspace = findWorkspace(state)
    return { state, details: await workspaceDetails(workspace) }
  })

  ipcMain.handle('flow:baseline-intent-plan', async (_event, input: { workspaceId?: string; projectName: string }) => {
    const state = loadState()
    const workspace = findWorkspace(state, input.workspaceId)
    const project = findProject(workspace, input.projectName)
    return baselineIntentPlan(workspace, project)
  })

  ipcMain.handle('flow:run-action', async (_event, input: ActionInput) => {
    const state = loadState()
    const workspace = findWorkspace(state, input.workspaceId)
    const project = findProject(workspace, input.projectName)
    // The stage list lets a user click a *different* (already completed) stage
    // while the active one is still running -- only the active stage's own
    // button was ever disabled. Two commands racing on the same working tree
    // (e.g. baseline's git status/generator run vs. the agent's own git
    // switch/commit/merge) can corrupt the very state the gate above depends
    // Same-project jobs remain exclusive. Across different projects, only
    // project-isolated actions (preflight/baseline) may overlap. Mutating FLOW
    // stages still retain the workspace-wide lock.
    const runningJob = [...jobs.values()].find((existing) =>
      projectRunConflicts(existing, workspace, project, input.action)
    )
    if (runningJob) {
      const scope = runningJob.projectName === project.name ? `проекта ${project.name}` : 'workspace'
      throw new Error(`Для ${scope} уже выполняется «${runningJob.action}». Дождитесь завершения или отмените текущую команду.`)
    }
    const savedRun = readTeamState(workspace)?.projects[project.name]
    const savedReleaseBranch = savedRun?.releaseBranch
    const effectiveTarget: ClosureTarget = savedRun?.target === 'green' || input.target === 'green' ? 'green' : 'yellow'
    input.target = effectiveTarget
    if (input.action === 'release') input.releaseBranch = releaseBranchForAction('release', input.releaseBranch, savedReleaseBranch, project.git?.releaseBranch)
    if (input.action === 'push-workspace') input.releaseBranch = releaseBranchForAction('publish', input.releaseBranch, savedReleaseBranch, project.git?.releaseBranch)
    // Supervisor deferrals belong to one planning epoch only. A fresh baseline
    // reconsiders every package from scratch instead of inheriting an old
    // "temporarily unreachable" decision forever. A migration-only restart
    // intentionally keeps the current plan; rebuilding it is the baseline's job.
    if (input.action === 'baseline' && input.baselineResume !== 'continue') clearPlannerDeferrals(workspace, project.name)

    let bestEffortReleaseReason: string | undefined
    let bestEffortCurrentLevel: string | undefined
    // Audit/state publication are useful even when the desired health level is
    // not fully reachable.  Only release needs a closure decision.  When the
    // executable plan is exhausted and Critical=0, allow a *best-effort*
    // release: all normal build/type/lint/final-gate/hooks still run, so this
    // never turns health degradation into a verifier bypass.
    if (input.action === 'release') {
      const roadmapPath = projectRoadmapPath(workspace, project.name)
      if (!roadmapPath || !existsSync(roadmapPath)) throw new Error('Нельзя продолжить FLOW: свежий roadmap JSON этого проекта не найден. Сначала выполните «Верификация».')
      const closure = targetClosureFromRoadmap(JSON.parse(readFileSync(roadmapPath, 'utf8')), project.name, effectiveTarget)
      const autonomy = autonomyPolicy(readSettings(workspace), project.name)
      if (!closure.reached && (!closure.bestEffortReleaseEligible || !autonomy.allowBestEffortRelease)) throw new Error(targetClosureMessage(closure))
      if (!closure.reached && closure.bestEffortReleaseEligible && autonomy.allowBestEffortRelease) {
        bestEffortReleaseReason = closure.bestEffortReason || targetClosureMessage(closure)
        bestEffortCurrentLevel = closure.current
      }
    }
    if (input.action === 'release') {
      const sourceBranch = project.git?.sourceBranch || 'master'
      let source = await spawnCapture('git', ['-C', project.path, 'rev-parse', `origin/${sourceBranch}`], project.path)
      if (source.code !== 0) {
        const fetched = await spawnCapture('git', ['-C', project.path, 'fetch', 'origin', sourceBranch], project.path, 120_000)
        if (fetched.code === 0) source = await spawnCapture('git', ['-C', project.path, 'rev-parse', `origin/${sourceBranch}`], project.path)
        if (source.code !== 0) throw new Error(`RELEASE_SOURCE_REF_UNAVAILABLE: не удалось получить origin/${sourceBranch}. ${fetched.stderr.trim() || source.stderr.trim()}`)
      }
      input.sourceCommit = source.stdout.trim()
    }
    const commands = [...await baselineStartCommands(input, project), ...await cleanAgentStartCommands(input, workspace, project), ...actionCommands(input, workspace, project)]
    const job: JobRecord = {
      id: randomUUID(), action: input.action, workspace, projectName: input.action === 'generate-all' ? undefined : project.name, target: input.target, cancelled: false,
      ...(['agent', 'recover'].includes(input.action) ? { agentProvider: workspace.agent, agentNote: input.agentNote?.trim() || undefined } : {}),
      ...(['release', 'push-workspace'].includes(input.action) ? { releaseBranch: input.releaseBranch } : {}),
      ...(input.action === 'release' ? { releaseSourceCommit: input.sourceCommit, releaseGateCommand: input.gateCommand?.trim() || undefined } : {}),
      ...(bestEffortReleaseReason ? { bestEffortReason: bestEffortReleaseReason, bestEffortCurrentLevel } : {}),
      ...(input.autopilot === true ? { autopilot: true } : {}),
    }
    jobs.set(job.id, job)
    void executeJob(job, commands)
    return { jobId: job.id, preview: commands.map((item) => `${item.command} ${item.args.join(' ')}`) }
  })

  ipcMain.handle('flow:get-hardware-snapshot', () => hardwareSnapshot())
  ipcMain.handle('flow:get-theme-preference', () => currentThemePreference())
  ipcMain.handle('flow:set-theme-preference', (_event, value: unknown) => { const state = loadState(); const preference = applyThemePreference(value); saveState({ ...state, themePreference: preference }); return { preference } })

  ipcMain.handle('flow:pause-job', (_event, jobId: string) => {
    const job = jobs.get(jobId)
    if (!job) return false
    if (job.action !== 'baseline') return false
    job.pauseRequested = true
    job.cancelled = true
    send('flow:job-output', { jobId: job.id, stream: 'system', workspaceId: job.workspace.id, projectName: job.projectName, line: 'Пауза Baseline: останавливаю текущий subprocess. Последний safe checkpoint сохранён; незавершённая операция будет повторена при «Продолжить».' })
    if (job.child) killProcessTree(job.child)
    return true
  })

  ipcMain.handle('flow:cancel-job', (_event, jobId: string) => {
    const job = jobs.get(jobId)
    if (!job) return false
    job.cancelled = true
    if (['agent', 'recover'].includes(job.action) && job.agentSessionId) job.agentSessionResumable = true
    if (job.child) killProcessTree(job.child)
    for (const child of job.parallelChildren ?? []) killProcessTree(child)
    job.parallelChildren?.clear()
    for (const worker of job.parallelJobs?.values() ?? []) stopOpenCodeServer(worker)
    job.parallelJobs?.clear()
    stopOpenCodeServer(job)
    return true
  })

  // OpenCode runs against a local `opencode serve` sidecar, so once the
  // session id is known the desktop can send a real asynchronous user message
  // into the currently running session. Codex/Claude stay on their one-shot
  // CLI transports for now; for them (and during the short OpenCode startup
  // window before a session id is captured) the note remains a next-attempt
  // queue rather than being lost.
  ipcMain.handle('flow:send-agent-note', async (_event, raw: { jobId: string; note: string; branch?: string }) => {
    const root = jobs.get(raw.jobId)
    if (!root || !['agent', 'recover'].includes(root.action)) return false
    const job = raw.branch
      ? root.parallelJobs?.get(raw.branch) ?? (root.agentBranch === raw.branch ? root : undefined)
      : root
    if (!job) return false
    const note = raw.note.trim()
    if (!note) return false
    if (await sendOpenCodeLiveMessage(job, note)) {
      send('flow:job-output', { jobId: job.id, stream: 'system', line: `Вы → агент: ${note}` })
      return true
    }
    // Append rather than replace: a user who sends two messages before the
    // next attempt starts (easy while a long attempt is still running) would
    // otherwise silently lose the first one.
    job.agentNote = job.agentNote ? `${job.agentNote}\n${note}` : note
    const reason = job.agentProvider === 'opencode'
      ? 'OpenCode-сессия ещё запускается'
      : `${job.agentProvider ?? 'этот агент'} работает через одноразовый CLI-вызов`
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Вы → агент: ${note}` })
    send('flow:job-output', { jobId: job.id, stream: 'system', line: `Сообщение сохранено до следующей попытки: ${reason}.` })
    return true
  })

  ipcMain.handle('flow:recover-with-agent', async (_event, raw: { workspaceId?: string; projectName: string; note: string }) => {
    const note = raw.note.trim()
    if (!note) throw new Error('Напишите агенту, что нужно проверить или исправить.')
    const state = loadState()
    const workspace = findWorkspace(state, raw.workspaceId)
    const project = findProject(workspace, raw.projectName)
    const runningJob = [...jobs.values()].find((existing) =>
      projectRunConflicts(existing, workspace, project, 'recover')
    )
    if (runningJob) {
      const scope = runningJob.projectName === project.name ? `проекта ${project.name}` : 'workspace'
      throw new Error(`Для ${scope} уже выполняется «${runningJob.action}».`)
    }
    const issue = await inferredRecoveryIssue(workspace, project)
    if (!issue) throw new Error('Нет сохранённой recoverable-ошибки или подготовленной dirty release-ветки.')
    if (issue.kind === 'hard') throw new Error(`${issue.code}: это safety hard-stop; пользовательский prompt не может его обойти.`)
    if (issue.kind === 'infrastructure') throw new Error(`${issue.code}: сначала восстановите инфраструктуру/Git-чтение.`)
    const savedRun = readTeamState(workspace)?.projects[project.name]
    const job: JobRecord = {
      id: randomUUID(), action: 'recover', workspace, projectName: project.name,
      target: savedRun?.target, cancelled: false, agentProvider: workspace.agent,
      agentNote: note, recoveryIssue: issue,
      ...(savedRun?.releaseBranch ? { releaseBranch: savedRun.releaseBranch } : {}),
      ...(savedRun?.releaseSourceCommit ? { releaseSourceCommit: savedRun.releaseSourceCommit } : {}),
      ...(savedRun?.releaseGateCommand ? { releaseGateCommand: savedRun.releaseGateCommand } : {}),
    }
    jobs.set(job.id, job)
    void executeJob(job, [])
    return { jobId: job.id }
  })

  ipcMain.handle('flow:open-path', async (_event, targetPath: string) => {
    if (!allowedPath(targetPath)) throw new Error('Путь находится вне зарегистрированного workspace/project.')
    return shell.openPath(targetPath)
  })
}

async function setupDashboardProtocol(): Promise<void> {
  protocol.handle(DASHBOARD_SCHEME, async (request) => {
    const url = new URL(request.url)
    const workspaceId = decodeURIComponent(url.pathname.replace(/^\//, ''))
    const projectName = url.searchParams.get('project') || undefined
    const state = loadState()
    const workspace = state.workspaces.find((item) => item.id === workspaceId)
    if (!workspace) return new Response('Workspace not found', { status: 404 })
    const project = projectName ? readProjects(workspace).find((item) => item.name === projectName) : undefined
    const dashboardPath = project ? projectDashboardPath(workspace, project.name) : artifactPath(workspace, 'htmlOut', '.dependency-roadmap/artifacts/local-dependency-roadmap.html')
    if (!dashboardPath || !existsSync(dashboardPath)) return new Response('Dashboard not generated for this project yet', { status: 404 })
    const response = await net.fetch(pathToFileURL(dashboardPath).toString())
    const headers = new Headers(response.headers)
    headers.set('Cache-Control', 'no-store, no-cache, must-revalidate')
    headers.set('Pragma', 'no-cache')
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers })
  })
}

function setupDownloads(): void {
  session.defaultSession.on('will-download', (_event, item) => {
    const state = loadState()
    const workspace = selectedWorkspace(state)
    if (!workspace) return
    // Capture ownership at download start. Selection may change while Chromium
    // is writing the file; the completion callback must never use/save a stale
    // DesktopState snapshot and pull the UI back to the old project.
    const downloadWorkspaceId = workspace.id
    const downloadProjectName = workspace.selectedProject
    const suggested = item.getFilename().replace(/[^a-zA-Z0-9._-]+/g, '-')
    let savePath: string
    // Captured before the download overwrites the file, so the `done` handler
    // below can tell which projects the user actually edited.
    let previousDashboardState = ''
    if (/dashboard-state/i.test(suggested)) {
      savePath = artifactPath(workspace, 'dashboardState', '.dependency-roadmap/state/dashboard-state.json')
      try { previousDashboardState = existsSync(savePath) ? readFileSync(savePath, 'utf8') : '' } catch { previousDashboardState = '' }
    } else {
      const directory = join(workspace.path, '.dependency-roadmap', 'desktop', 'downloads')
      mkdirSync(directory, { recursive: true })
      savePath = join(directory, `${new Date().toISOString().replace(/[:.]/g, '-')}-${suggested}`)
    }
    mkdirSync(dirname(savePath), { recursive: true })
    item.setSavePath(savePath)
    item.once('done', (_doneEvent, status) => {
      if (status === 'completed') {
        if ((/prompt|task|agent/i.test(suggested) || suggested.endsWith('.txt')) && downloadProjectName) {
          const latestState = loadState()
          const stored = latestState.workspaces.find((candidate) => candidate.id === downloadWorkspaceId)
          if (stored) {
            rememberScopedPromptPath(stored, downloadProjectName, savePath)
            saveState(latestState)
          }
        }
        const recalculate = /dashboard-state-recalculate/i.test(suggested)
        // Recalculate exactly the projects whose overrides changed. Falling
        // back to the selected project only when the diff yields nothing
        // keeps behaviour sane for a state file we could not parse.
        let changedProjects: string[] = []
        if (recalculate) {
          try { changedProjects = changedOverrideProjects(previousDashboardState, readFileSync(savePath, 'utf8')) } catch { changedProjects = [] }
          const known = new Set(readProjects(workspace).map((project) => project.name))
          changedProjects = changedProjects.filter((project) => known.has(project))
          if (!changedProjects.length && downloadProjectName) changedProjects = [downloadProjectName]
        }
        send('flow:download-saved', {
          path: savePath,
          filename: suggested,
          workspaceId: downloadWorkspaceId,
          projectName: downloadProjectName,
          recalculate,
          ...(changedProjects.length ? { recalculateProjects: changedProjects } : {}),
        })
      }
    })
  })
}

function setupAutoUpdater(): void {
  if (!app.isPackaged) return
  // electron-builder embeds the public GitHub feed in app-update.yml.
  // No client API key or setFeedURL override is required.
  autoUpdater.autoDownload = true
  // Only the explicit install action may replace the running app. Besides
  // avoiding stale intermediate installs, this prevents the NSIS updater from
  // killing a manually reopened app a few seconds after a normal quit.
  autoUpdater.autoInstallOnAppQuit = false
  autoUpdater.on('checking-for-update', () => publishUpdateStatus({ state: 'checking', version: downloadedUpdateVersion ?? currentUpdateStatus.version }))
  autoUpdater.on('update-available', (info) => publishUpdateStatus({ state: 'available', version: info.version }))
  autoUpdater.on('update-not-available', (info) => {
    downloadedUpdateVersion = undefined
    publishUpdateStatus({ state: 'current', version: info.version })
  })
  autoUpdater.on('download-progress', (progress) => publishUpdateStatus({ state: 'downloading', version: currentUpdateStatus.version, percent: Math.round(progress.percent) }))
  autoUpdater.on('update-downloaded', (info) => {
    downloadedUpdateVersion = info.version
    publishUpdateStatus({ state: 'ready', version: info.version })
  })
  autoUpdater.on('error', (error) => {
    publishUpdaterError(error)
  })
  const check = () => {
    void checkForLatestUpdate().catch((error: unknown) => {
      publishUpdaterError(error)
    })
  }
  stopUpdateChecks?.()
  stopUpdateChecks = scheduleUpdateChecks(check)
}
async function createWindow(): Promise<void> {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 940,
    minWidth: 1080,
    minHeight: 700,
    backgroundColor: '#ffffff',
    title: 'DepLoom',
    icon: join(__dirname, '..', 'build', process.platform === 'win32' ? 'icon.ico' : 'icon.png'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  const devServerOrigin = (() => {
    if (!process.env.VITE_DEV_SERVER_URL) return ''
    try { return new URL(process.env.VITE_DEV_SERVER_URL).origin } catch { return '' }
  })()
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (devServerOrigin) {
      try {
        if (new URL(url).origin === devServerOrigin) return
      } catch { /* malformed navigation is denied below */ }
    }
    event.preventDefault()
    if (/^https?:/i.test(url)) void shell.openExternal(url)
  })
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) void shell.openExternal(url)
    return { action: 'deny' }
  })
  if (process.env.VITE_DEV_SERVER_URL) await mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL)
  else await mainWindow.loadFile(join(__dirname, '..', 'dist', 'index.html'))
}

app.whenReady().then(async () => {
  applyThemePreference(loadState().themePreference)
  app.setAppUserModelId('io.github.alexanderlevenskikh.deploom')
  setupIpc()
  await setupDashboardProtocol()
  setupDownloads()
  await createWindow()
  setupAutoUpdater()
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) void createWindow() })
})

app.on('before-quit', () => {
  stopUpdateChecks?.()
  stopUpdateChecks = undefined
  // Nothing else tears these down when the window is closed mid-run: an agent
  // CLI keeps writing to a repository the user is about to reopen elsewhere,
  // and the `opencode serve` sidecar survives as an orphan still holding its
  // local port, so the next launch reserves another one and leaks again.
  for (const job of jobs.values()) {
    job.cancelled = true
    if (['agent', 'recover'].includes(job.action) && job.agentSessionId) job.agentSessionResumable = true
    if (job.child) killProcessTree(job.child)
    for (const child of job.parallelChildren ?? []) killProcessTree(child)
    job.parallelChildren?.clear()
    for (const worker of job.parallelJobs?.values() ?? []) stopOpenCodeServer(worker)
    job.parallelJobs?.clear()
    stopOpenCodeServer(job)
  }
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
