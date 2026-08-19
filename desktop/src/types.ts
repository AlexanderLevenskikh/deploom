export type AgentProvider = 'codex' | 'opencode' | 'claude'
export type FlowAction = 'preflight' | 'sync-tool' | 'baseline' | 'generate' | 'generate-all' | 'audit' | 'agent' | 'recover' | 'release' | 'commit-state' | 'push-workspace'
export type TargetLevel = 'yellow' | 'green'
export type ThemePreference = 'system' | 'light' | 'dark'
export type BaselinePackagePolicy = 'auto' | 'keep-current' | 'required'
export type BaselineIntent = { schemaVersion: 1; policies: Record<string, BaselinePackagePolicy>; extraIterations?: number; decisionGrantIterations?: number }
export type BaselineIntentCandidate = { name: string; kind: 'runtime' | 'dev' | 'peer'; requestedSpec: string; currentVersion?: string }
export type BaselineIntentPlan = { candidates: BaselineIntentCandidate[]; intent: BaselineIntent }
export type BaselineDecision = { schemaVersion: 1; reason: 'repeated-package-conflict' | 'budget-exhausted' | 'policy-unsat'; project: string; mode: string; iteration?: number; hardIterations?: number; learnedConstraints?: number; package?: string; currentVersion?: string; failedVersions?: string[]; predicate?: string }

export type HardwareSnapshot = { capturedAt: string; cpu: { logicalCores: number; loadPct?: number }; memory: { totalBytes: number; freeBytes: number; usedBytes: number; usedPct: number }; process: { memoryBytes?: number; cpuPct?: number }; disks?: Array<{ name: string; filesystem?: string; freeBytes?: number; totalBytes?: number; usedPct?: number }> }
export type BaselineRecoveryInfo = { available: boolean; mode?: 'yellow' | 'green'; status?: string; phase?: string; updatedAt?: string; generation?: number; iteration?: number; lastAssignment?: string; lastPredicate?: string; learnedConstraints?: number; exactExclusions?: number; reason?: string }

export type ProjectLevel = { status: 'red' | 'yellow' | 'green'; lagOkPct?: number; remainingYellow?: number; remainingGreen?: number; measuredAt?: string }

export type ProjectSpec = {
  name: string
  path: string
  git?: {
    sourceBranch?: string
    baseBranch?: string
    branchPrefix?: string
    mergedBranch?: string
    releaseBranch?: string
    push?: boolean
  }
}

export type WorkspaceRecord = {
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

export type DesktopState = {
  schemaVersion: 1
  selectedWorkspaceId?: string
  notificationsEnabled?: boolean
  themePreference?: ThemePreference
  workspaces: WorkspaceRecord[]
}

export type EnvironmentInfo = Record<string, { available: boolean; version: string }>

export type AgentSessionState = { provider: AgentProvider; id: string; interrupted: boolean; updatedAt: string; scopeFingerprint?: string }

export type TeamFlowState = {
  schemaVersion: 1
  updatedAt: string
  // `agentSession` is the whole-migration single-session shape every run
  // before the per-branch-group loop persisted; `agentSessions` (keyed by
  // branch) plus `activeAgentBranch` is used once a project's migration runs
  // under that loop. A project has one or the other, never both.
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
    activeAgentBranch?: string
    recovery?: { code: string; kind: 'agent' | 'hard' | 'infrastructure'; action: string; message: string; branch?: string; updatedAt: string }
    autonomyPlateau?: { target: string; reason: string; updatedAt: string }
    bestEffortRelease?: { target: string; current: string; reason: string; updatedAt: string; handoffPath?: string }
  }>
}

export type MigrationBranchStatus = 'waiting' | 'created' | 'partial' | 'changes' | 'ready' | 'integrated' | 'merged'
export type MigrationBranchRuntimePhase = 'planning' | 'queued' | 'starting' | 'running' | 'bootstrapping' | 'verifying' | 'repairing' | 'failed' | 'ready' | 'merging' | 'integration-verifying'
export type MigrationBranchRuntime = { phase: MigrationBranchRuntimePhase; detail?: string; updatedAt: string }

export type MigrationBranchProgress = {
  branch: string
  label: string
  packages: string[]
  status: MigrationBranchStatus
  runtime?: MigrationBranchRuntime
  worktreePath?: string
  worktreeDirtyChanges?: number
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
  factsRef: string
  factsCommit?: string
  trustworthy: boolean
}

export type LagBlocker = {
  package: string
  kind?: string
  group?: number
  current?: string
  required?: string
  lagPolicyMonths?: number
  plannedTarget?: string
  note?: string
}

export type TargetClosure = {
  reached: boolean
  target: TargetLevel
  current: 'red' | 'yellow' | 'green' | 'unknown'
  lagOkPct?: number
  lagOk?: number
  total?: number
  remainingPackages: string[]
  lagBlockers: LagBlocker[]
  neededForYellow?: number
  plannedLagFixes?: number
  maxLagOkPctAfterPlan?: number
  neededBeyondCurrentPlan?: number
  planCanReachYellow?: boolean
  criticalPackages?: string[]
  uncoveredCriticalPackages?: string[]
  critical?: number
  high?: number
  excluded?: number
  bestEffortReleaseEligible?: boolean
  bestEffortReason?: string
}

export type WorkspaceDetails = {
  workspace: WorkspaceRecord
  projects: ProjectSpec[]
  settingsExists: boolean
  dashboardPath?: string
  dashboardUrl?: string
  dashboardExists: boolean
  projectPromptPath?: string
  promptStale: boolean
  git: { branch: string; dirty: boolean; summary: string[] }
  teamState?: TeamFlowState
  projectLevels: Record<string, ProjectLevel>
  targetClosure?: TargetClosure
  migrationProgress?: MigrationProgress
  baselineRecovery?: BaselineRecoveryInfo
}

export type BootstrapPayload = {
  appVersion: string
  state: DesktopState
  environment: EnvironmentInfo
  details?: WorkspaceDetails
  defaults: { templateRemote: string; toolRemote: string }
  updateStatus?: UpdateStatus
  notificationsEnabled: boolean
  themePreference?: ThemePreference
}

export type ActionInput = {
  action: FlowAction
  workspaceId?: string
  projectName?: string
  target?: TargetLevel
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
  // Internal marker: suppress per-stage desktop notifications while the
  // Autopilot owns the whole multi-stage FLOW. The final Autopilot notification
  // is emitted separately when no next action remains.
  autopilot?: boolean
}

export type JobOutputSource = { kind: 'group' | 'planner'; id: string; label: string }
export type JobOutput = { jobId: string; stream: 'system' | 'stdout' | 'stderr'; line: string; workspaceId?: string; projectName?: string; source?: JobOutputSource; receivedAt?: number }
export type JobFinished = { jobId: string; action: FlowAction; workspaceId?: string; projectName?: string; exitCode: number; error?: string }
export type DownloadSaved = { path: string; filename: string; workspaceId?: string; projectName?: string; recalculate?: boolean; recalculateProjects?: string[] }
export type UpdateStatus = { state: 'idle' | 'checking' | 'available' | 'downloading' | 'current' | 'ready' | 'error'; version?: string; percent?: number; message?: string; authRequired?: boolean }

export type DependencyFlowApi = {
  bootstrap: () => Promise<BootstrapPayload>
  pickDirectory: () => Promise<string | undefined>
  pickFile: (filters?: Array<{ name: string; extensions: string[] }>) => Promise<string | undefined>
  listAgentModels: (agentProvider: AgentProvider, cwd?: string) => Promise<string[]>
  registerWorkspace: (input: { path: string; name?: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  cloneWorkspace: (input: { parentPath: string; folderName: string; teamRemote?: string; templateRemote?: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  addProject: (input: { workspaceId?: string; name: string; path: string; sourceBranch?: string; baseBranch?: string; mergedBranch?: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  removeProject: (input: { workspaceId?: string; projectName: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  selectWorkspace: (workspaceId: string) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  updateWorkspace: (input: Partial<WorkspaceRecord> & { id: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  updateProjectBranches: (input: { workspaceId?: string; projectName: string; branchBase?: string; push?: boolean }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  refreshWorkspace: () => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  getBaselineIntentPlan: (input: { workspaceId?: string; projectName: string }) => Promise<BaselineIntentPlan>
  runAction: (input: ActionInput) => Promise<{ jobId: string; preview: string[] }>
  cancelJob: (jobId: string) => Promise<boolean>
  pauseJob: (jobId: string) => Promise<boolean>
  sendAgentNote: (input: { jobId: string; note: string; branch?: string }) => Promise<boolean>
  recoverWithAgent: (input: { workspaceId?: string; projectName: string; note: string }) => Promise<{ jobId: string }>
  openPath: (targetPath: string) => Promise<string>
  checkForUpdates: () => Promise<{ currentVersion: string; availableVersion?: string; development?: boolean; error?: UpdateStatus }>
  setNotificationsEnabled: (enabled: boolean) => Promise<{ enabled: boolean }>
  notifyAutopilotComplete: (input: { projectName: string; published: boolean }) => Promise<void>
  installUpdate: () => Promise<void>
  getHardwareSnapshot: () => Promise<HardwareSnapshot>
  getThemePreference: () => Promise<ThemePreference>
  setThemePreference: (preference: ThemePreference) => Promise<{ preference: ThemePreference }>
  onUpdateStatus: (handler: (event: UpdateStatus) => void) => () => void
  onJobOutput: (handler: (event: JobOutput) => void) => () => void
  onMigrationProgressChanged: (handler: (event: { jobId: string; workspaceId?: string; projectName?: string; branch: string; phase?: MigrationBranchRuntimePhase }) => void) => () => void
  onJobFinished: (handler: (event: JobFinished) => void) => () => void
  onDownloadSaved: (handler: (event: DownloadSaved) => void) => () => void
}

declare global {
  interface Window {
    dependencyFlow?: DependencyFlowApi
  }
}