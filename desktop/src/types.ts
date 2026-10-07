export type AgentProvider = 'codex' | 'opencode' | 'claude'
export type FlowAction = 'preflight' | 'sync-tool' | 'baseline' | 'generate' | 'generate-all' | 'audit' | 'agent' | 'recover' | 'release' | 'commit-state' | 'push-workspace'
export type TargetLevel = 'yellow' | 'green'
export type ThemePreference = 'system' | 'light' | 'dark'
export type BaselinePackagePolicy = 'auto' | 'keep-current' | 'required'
export type BaselineSearchMode = 'AUTO' | 'BOUNDED_IMPROVEMENT' | 'EXHAUSTIVE'
export type BaselineExecutionMode = 'FAST' | 'AUTOPILOT' | 'BACKGROUND'
export type BaselineProofMode = 'VERIFIED' | 'DRAFT'
export type BaselineControlMode = 'AUTONOMOUS' | 'CONFIRM_SIGNIFICANT'
export type AcceptancePolicy = { maxKnownCritical: number; maxKnownHigh: number; targetLevel?: 'yellow' | 'green'; minLagOkPct?: number; maxKnownModerate?: number; maxKnownLow?: number; lagPolicyMonths?: number }
export type AcceptanceVerdict = { status: 'ACCEPTED' | 'REMEDIATION_REQUIRED' | 'UNKNOWN'; accepted: boolean; evidenceComplete: boolean; dependencyEvidenceFresh: boolean; critical?: number; high?: number; moderate?: number; low?: number; criticalPackages: string[]; highPackages: string[]; moderatePackages?: string[]; lowPackages?: string[]; auditGeneratedAt?: string; auditEngine?: string; lagOkPct?: number; lagOk?: number; lagUnknown?: number; lagTotal?: number; reasons: string[]; policy: AcceptancePolicy }
export type BaselineDeferredCohort = { id: string; label: string; packages: string[]; predicate?: string; confidence?: number; authority: 'DIAGNOSTIC_HINT'; deferredAt?: string; decisionId?: string; boundaryPackages?: string[]; warningPackages?: string[] }
export type BaselineCohortAction = { kind: 'DEFER' | 'REACTIVATE'; cohortId: string; label: string; packages: string[]; predicate?: string; confidence?: number; decisionId?: string }
export type BaselineIntent = { schemaVersion: 1 | 2; policies: Record<string, BaselinePackagePolicy>; controlMode?: BaselineControlMode; budgetMinutes?: number; budgetMinutesExplicit?: boolean; acceptancePolicy?: AcceptancePolicy; extraIterations?: number; decisionGrantIterations?: number; searchMode?: BaselineSearchMode; executionMode?: BaselineExecutionMode; proofMode?: BaselineProofMode; deferredCohorts?: BaselineDeferredCohort[]; cohortAction?: BaselineCohortAction; autoOpenPrompt?: boolean; targetLevel?: 'yellow' | 'green'; minLagOkPct?: number; lagPolicyMonths?: number; productMode?: 'fast' | 'deep' }
export type BaselineIntentCandidate = { name: string; kind: 'runtime' | 'dev' | 'peer'; requestedSpec: string; currentVersion?: string }
export type BaselineIntentPlan = { candidates: BaselineIntentCandidate[]; intent: BaselineIntent; legacyIntentResolutionNeeded?: string }
export type BaselineCohortSuggestion = { id: string; label: string; predicate: string; subjects: string[]; packages: string[]; blockedPackages: string[]; warningPackages: string[]; boundaryPackages: string[]; confidence: number; reasons: string[]; decisionId: string; expandedFrom?: string; authority: 'DIAGNOSTIC_HINT' }
export type BaselineCohortResolution = { status: 'SUGGESTED' | 'NO_ACTIONABLE_COHORT'; reason: 'ACTIONABLE_DIAGNOSTIC_COHORT' | 'PREDICATE_UNAVAILABLE' | 'NO_SAFE_DEFERRABLE_PACKAGE_OR_NEIGHBORHOOD'; predicate?: string; cohortId?: string; decisionId?: string; authority: 'DIAGNOSTIC_HINT' }
export type BaselineDecision = { schemaVersion: 1 | 2; event?: 'BASELINE_CONTINUATION_REQUIRED'; reason: 'repeated-package-conflict' | 'budget-exhausted' | 'policy-unsat' | 'AUTOMATIC_BUDGET_EXHAUSTED' | 'STAGNATION' | 'BASE_ITERATION_LIMIT' | 'EXPENSIVE_DIAGNOSTIC'; project: string; mode: string; iteration?: number; hardIterations?: number; learnedConstraints?: number; package?: string; currentVersion?: string; failedVersions?: string[]; predicate?: string; repeatedPredicate?: string; elapsedSeconds?: number; rejectedAssignments?: number; observedCandidateDurationSeconds?: number; continuationCostClass?: 'minutes' | 'hours'; recommendedAction?: string; availableActions?: string[]; suggestedCohort?: BaselineCohortSuggestion; bestIncumbent?: { changed_dependency_count?: number; policy_score?: number; deferred_targets?: string[]; objective_rank?: number[] }; cohortResolution?: BaselineCohortResolution }

export type DependencyGraphPackage = { name: string; kind: 'runtime' | 'dev' | 'peer'; requestedSpec: string; currentVersion?: string; installedVersion?: string; policy: BaselinePackagePolicy; manifestObserved: boolean }
export type DependencyGraphObservedEdge = { source: string; target: string; kind: 'dependency' | 'peer' | 'optional'; authority: 'OBSERVED_LOCAL_MANIFEST' }
export type DependencyGraphSnapshot = { schemaVersion: 1; project: string; capturedAt: string; packages: DependencyGraphPackage[]; edges: DependencyGraphObservedEdge[]; intent: Pick<BaselineIntent, 'policies' | 'deferredCohorts'>; manifestCoverage: { observed: number; total: number; missing: string[] }; authorityBoundary: { packageList: 'PROJECT_MANIFEST'; relations: 'OBSERVED_LOCAL_MANIFEST'; cohorts: 'DIAGNOSTIC_HINT_OR_USER_POLICY'; proofAuthority: false } }

export type HardwareSnapshot = { capturedAt: string; cpu: { logicalCores: number; loadPct?: number }; memory: { totalBytes: number; freeBytes: number; usedBytes: number; usedPct: number }; process: { memoryBytes?: number; cpuPct?: number }; disks?: Array<{ name: string; filesystem?: string; freeBytes?: number; totalBytes?: number; usedPct?: number }> }
export type BaselineRecoveryInfo = { available: boolean; mode?: 'yellow' | 'green'; status?: string; phase?: string; updatedAt?: string; generation?: number; iteration?: number; lastAssignment?: string; lastPredicate?: string; learnedConstraints?: number; exactExclusions?: number; reason?: string }
export type ProjectPromptPreview = { path: string; content: string; stale: boolean; staleReason?: string; mtimeMs: number; size: number; projectName?: string }

export type ProjectLevel = { status: 'red' | 'yellow' | 'green'; lagOkPct?: number; remainingYellow?: number; remainingGreen?: number; measuredAt?: string }

export type ProjectSpec = {
  name: string
  path: string
  // D2.4: display-only Node hints from repo pins (engines.node, .nvmrc,
  // .node-version). The UI may suggest them, never auto-fills the runtime.
  nodeHints?: string[]
  git?: {
    sourceBranch?: string
    baseBranch?: string
    branchPrefix?: string
    mergedBranch?: string
    releaseBranch?: string
    push?: boolean
  }
  nodeVersion?: string
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
  // Per-project pointer to the newest completed Draft run whose result.json is
  // still on disk; rendering re-reads the artifact by runId (restart-safe).
  draftResults?: Record<string, { runId: string; status: 'DRAFT_READY' | 'DRAFT_PARTIAL'; importedAt: string }>
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
  acceptanceVerdict?: AcceptanceVerdict
  // F1: the persisted goal, so stage actions/autopilot/generate use the saved
  // target instead of a hard-coded 'yellow'.
  baselineIntent?: { targetLevel?: 'yellow' | 'green'; minLagOkPct?: number; lagPolicyMonths?: number }
  migrationProgress?: MigrationProgress
  baselineRecovery?: BaselineRecoveryInfo
  // The ready-to-handoff Draft Baseline result for the selected project
  // (planning-only, NOT_VERIFIED). Present only when a Draft run finished and
  // its artifacts/runs/<runId>/draft/result.json is still resolvable.
  draftResult?: DraftResultSnapshot
}

// Compact, typed mirror of the planner's draft result.json contract. The full
// prompt/plan text is fetched on demand through getCurrentDraftResult().
export type DraftResultSnapshot = {
  runId: string
  status: 'DRAFT_READY' | 'DRAFT_PARTIAL'
  projectId: string
  generatedAt: string
  elapsedMs: number
  deadlineSeconds?: number
  remainingMs?: number
  policyHash: string
  summary: string
  partialReason?: string | null
  verificationStatus: string
  authority: string
  compatibility: string
  metadata: {
    total: number
    unknown: number
    unknownPackages: string[]
    processed?: number
    processedTotal?: number
    pending?: number
    interrupted?: number
    registryFailed?: number
    osvUnknown?: number
    metadataKnown?: number
    metadataTotal?: number
    securityKnown?: number
    securityTotal?: number
    securityUnknown?: number
    noTarget?: number
    candidateTruncated?: number
    candidateTruncatedTargetless?: number
    candidateTruncatedProposed?: number
    blocked?: number
    ok?: number
    postPlanLagOk?: number
    postPlanLagOkPct?: number
    postPlanScopeTotal?: number
    postPlanTargetLevel?: string
    // F1: the policy gate (user's minLagOkPct) and the +5 p.p. planning
    // reserve are SEPARATE required counts and SEPARATE shortfalls; the
    // published postPlanShortfall is the POLICY one.
    postPlanPolicyRequired?: number
    postPlanReserveRequired?: number
    postPlanPolicyShortfall?: number
    postPlanReserveShortfall?: number
    postPlanShortfall?: number
    // F3: projected security on the EXACT chosen/kept versions + coverage and
    // the feasibility verdict (feasible | unknown | blocked).
    postPlanCritical?: number
    postPlanHigh?: number
    postPlanModerate?: number
    postPlanLow?: number
    postPlanSecurityKnown?: number
    postPlanSecurityUnknown?: number
    postPlanSecurityTotal?: number
    postPlanGoal?: string
  }
  // F2: per-project post-plan numbers (sizes and policies differ); the
  // renderer prefers this view for the selected project.
  perProject?: Record<string, {
    postPlanLagOk?: number
    postPlanLagOkPct?: number
    postPlanScopeTotal?: number
    postPlanTargetLevel?: string
    postPlanPolicyRequired?: number
    postPlanReserveRequired?: number
    postPlanPolicyShortfall?: number
    postPlanReserveShortfall?: number
    postPlanCritical?: number
    postPlanHigh?: number
    postPlanModerate?: number
    postPlanLow?: number
    postPlanSecurityKnown?: number
    postPlanSecurityUnknown?: number
    postPlanSecurityTotal?: number
    postPlanGoal?: string
    noTarget?: number
    candidateTruncated?: number
    candidateTruncatedTargetless?: number
    candidateTruncatedProposed?: number
    blocked?: number
    ok?: number
    proposed?: number
    scopeTotal?: number
  }>
  proposals: Record<string, number>
  artifacts: { manifest: string; plan: string; prompt: string; summary: string }
  hashes: { plan: string; prompt: string }
  // F4: whether the LAST Draft for this project still matches the current
  // planner inputs and acceptance policy (computed over the recorded identity,
  // not mtimes). A fresh run of THIS session is not stale; an old saved result
  // whose inputs moved is.
  stale?: boolean
  staleReason?: string
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
export type JobFinished = { jobId: string; action: FlowAction; workspaceId?: string; projectName?: string; exitCode: number; error?: string; draftResult?: DraftResultSnapshot }
export type DownloadSaved = { path: string; filename: string; workspaceId?: string; projectName?: string; recalculate?: boolean; recalculateProjects?: string[] }
export type UpdateStatus = { state: 'idle' | 'checking' | 'available' | 'downloading' | 'current' | 'ready' | 'error'; version?: string; percent?: number; message?: string; authRequired?: boolean }

export type IterativeTaskUiAction = { package: string; from: string; to: string }
export type IterativeTaskUiDeferred = { package: string; current: string; lagPolicyTarget: string; reason: string }
export type IterativeTaskUiView = {
  artifactId: string
  runId: string
  projectName: string
  targetCheckpointId: string
  languages: string[]
  actions: IterativeTaskUiAction[]
  deferred: IterativeTaskUiDeferred[]
  exactVersions: Record<string, string>
  completeness: { policySatisfied: boolean; denominator: number; remaining: number }
  verification: { status: string }
  content: string
  contentHashes: Record<string, { contentHash: string; contentBytes: number }>
  createdAt: string
}
export type IterativeTaskSnapshot = {
  present: boolean
  missing: string[]
  stale: boolean
  staleReason?: string
  task?: IterativeTaskUiView
  runDir?: string
}
export type IterativeTaskActionOutcome = { ok: boolean; artifactId?: string; stale?: boolean; canceled?: boolean; path?: string; fingerprint?: string; missing?: string[]; error?: string }
export type IterativeRunnerRepairRequest = { requestId: string; summary: string }
export type IterativeRunnerDecision = {
  step: 'begin' | 'verify-bootstrap' | 'plan-next' | 'materialize' | 'precheck' | 'verify-exact' | 'apply-feedback' | 'finish' | 'audit' | 'agent' | null
  phase: string
  reason: string
  repairRequests?: IterativeRunnerRepairRequest[]
  satisfied?: boolean
  bootstrap?: boolean
  infraBlocked?: boolean
}
export type IterativeRuntimeView = {
  requested?: string
  effectiveVersion?: string
  source?: string
  packageManager?: string
  packageManagerVersion?: string
  platform?: string
  arch?: string
}
// L1: display snapshot of the durable attempt journal (runDir/attempt.json +
// attempt.log). The journal is written BEFORE the long begin/drive spawns, so
// the first click is observable immediately and a Desktop restart restores
// both the current state and the reason the attempt stopped.
export type IterativeAttemptView = {
  attemptId: string
  projectName: string
  // P2 (#2): events are scoped to the workspace — the panel must not ingest the
  // same-named project of a DIFFERENT workspace.
  workspaceId?: string
  // 'waiting' — a retryable agent-launch outage (rate limit / temporary /
  // unknown with budget left) parked the repair until the provider's reset
  // time or a bounded backoff. It is NOT an active run (no attempt spent, no
  // agent spawned) and NOT a failure — the repair resumes on its own.
  status: 'starting' | 'running' | 'done' | 'failed' | 'canceled' | 'waiting'
  stage: 'preflight' | 'begin' | 'drive' | 'agent' | 'none'
  phase?: string
  startedAt: number
  activeElapsedMs?: number
  activeSince?: number
  finishedAt?: number
  lastHeartbeatAt: number
  targetSource: 'none' | 'roadmap' | 'discovery'
  discovery?: { parallelism: number; timeoutSeconds: number; maxPackages: number }
  targetsCount?: number
  packageProgress?: { processed: number; total: number }
  discoveryCompleted?: boolean
  discoverySkipped?: number
  stepsDone: string[]
  reason?: string
  lastError?: string
  lastStep?: string
  runCreated: boolean
  cancelRequested?: boolean
  // Launch-wait display: epoch-ms time of the next agent launch attempt while
  // the attempt is 'waiting'. The AUTHORITATIVE retry time lives in the durable
  // lease — this only drives the countdown in the panel.
  waitUntil?: number
  // Launch-wait display: the failure kind that caused the wait (rate-limited /
  // temporary / unknown) for a precise reason label.
  waitFailureKind?: string
}
export type IterativeStatusOutcome = {
  progressSummary?: { checkpointId: string; remaining: number; denominator: number; accepted: number; deferred: number; targetCount: number; unresolvedGoals: number }
  autopilotActive?: boolean
  autopilotEnabled?: boolean
  validationProfile?: { commands: string[]; unitCommand?: string; compareExistingFailures?: boolean; deferredChecks?: string; suggestedUnitCommand?: string }
  validationScope?: { mode?: string; existingFailures?: number; total?: number; skipped?: number }
  legacyPlanPresent?: boolean
  ok: boolean
  present: boolean
  stale: boolean
  staleReason?: string
  phase?: string
  decision?: IterativeRunnerDecision
  scopeExpansionIssues?: Array<{ packages: string[]; proposals: string[]; reason: string; nextAction: string; packageCount?: number; maxPackages?: number }>
  workingCheckout?: { path: string; kind: 'trial' | 'checkpoint' | 'project'; checkpointId?: string }
  stopDetail?: { kind: 'repair-timeout'; packageCount: number; recoverable: boolean; auditStatus: string }
  repairPause?: { exhausted: boolean; windows: number }
  // Review P1: Electron-authoritative liveness — a REAL begin/drive child is
  // in-flight for this project, independent of any React component's local busy
  // flag. The panel relies on it across tab switches and remounts.
  inFlight?: boolean
  // D2.4: display-only view of the durable runtime contract (requested vs
  // effective + CI evidence source). The UI renders it, never decides by it.
  requestedNode?: string
  runtime?: IterativeRuntimeView
  attempt?: IterativeAttemptView
  attemptLog?: string
  // P1#4: durable standalone "Проверить проект" verdict (present only while no
  // run exists). A check is a REAL preflight + current-dependencies control and
  // never creates/starts the migration — the UI shows "ready → Начать обновление".
  // `control` carries the structured conclusion (passed/failed/inconclusive) so
  // a failed control surfaces the repair agent, not a dead-end re-check.
  checked?: { validationScope?: { mode?: string; existingFailures?: number; total?: number; skipped?: number }; ok: boolean; checkedAt?: string; control?: { status?: string; kind?: string; summary?: string; failingCommands?: { command: string; exitCode: number }[] } }
  error?: string
}
// #1: durable supervisor outcome. drive runs the Python steps in a loop and
// stops ONLY at an agent gate (repair needed — the caller dispatches the
// agent), at finish, or on error/budget; every intermediate step (plan-next
// -> materialize -> precheck -> verify-exact -> next cohort) happens without
// a human click. Restart-safe: each iteration recomputes the decision from the
// durable Python state.
export type IterativeDriveOutcome = {
  autopilot?: { stopped: string; error?: string }
  ok: boolean
  steps: string[]
  stopped: 'agent-gate' | 'finished' | 'error' | 'time-budget' | 'iteration-budget' | 'canceled' | 'infra-blocked'
  phase?: string
  step?: string
  reason?: string
  bootstrap?: boolean
  repairRequests?: IterativeRunnerRepairRequest[]
  attempt?: IterativeAttemptView
  error?: string
}
export type IterativeAgentOutcome = {
  autopilot?: { stopped: string; error?: string }
  ok: boolean
  changedFiles?: string[]
  // #4: planner-owned files the agent mutated (modified/added/removed). Non-empty
  // means the repair was REJECTED with FORBIDDEN_MUTATION before feedback/verify.
  forbiddenMutations?: string[]
  phase?: string
  next?: IterativeRunnerDecision
  agentOutputTail?: string
  // Launch-wait resilience: ok=false + waiting=true parks the repair until
  // retryAt (provider rate limit / temporary outage); cancelable lets the panel
  // offer "отменить ожидание" via flow:iterative:cancel.
  waiting?: boolean
  retryAt?: number
  retryAfterSeconds?: number
  failureKind?: string
  cancelable?: boolean
  reason?: string
  error?: string
}

export type IterativeDiscoveryReport = {
  discovered: string[]
  unavailable: string[]
  budgetSkipped: string[]
  noCompatibleAlternative: string[]
}

export type IterativeBeginOutcome = {
  autopilot?: { stopped: string; error?: string }
  ok: boolean
  step?: string
  phase?: string
  next?: IterativeRunnerDecision
  targetsCount?: number
  targetLevel?: string
  // L2: begin did NOT create a run because there are no saved roadmap targets
  // (the user must pick Draft/scope setup or an explicit bounded discovery).
  noTargets?: boolean
  // P-review: the registry discovery outcome, so the UI can distinguish
  // "updates are not needed" from "registry data could not be fetched" and
  // "the discovery budget was exhausted" — the last two are NOT a completed
  // migration.
  discovery?: {
    discovered: string[]
    unavailable: string[]
    budgetSkipped: string[]
    noCompatibleAlternative: string[]
  }
  // P1#4: this begin was a "--check-only" project check. A check records a
  // durable non-run verdict and NEVER starts the migration. `control` carries
  // the structured conclusion (status passed/failed/inconclusive + failing
  // project commands) when the check concluded — a failed control means the
  // repair-agent path, an inconclusive result means a retry.
  checked?: { validationScope?: { mode?: string; existingFailures?: number; total?: number; skipped?: number }; ok: boolean; checkedAt?: string; control?: { status?: string; kind?: string; summary?: string; failingCommands?: { command: string; exitCode: number }[] } }
  // P2 (#1): this begin was the "--repair-only" repair of the current state
  // (independent of roadmap targets); it opened the repair run.
  repair?: boolean
  attempt?: IterativeAttemptView
  error?: string
}
export type DependencyFlowApi = {
  bootstrap: () => Promise<BootstrapPayload>
  pickDirectory: () => Promise<string | undefined>
  pickFile: (filters?: Array<{ name: string; extensions: string[] }>) => Promise<string | undefined>
  copyText: (text: string) => Promise<{ ok: boolean }>
  listAgentModels: (agentProvider: AgentProvider, cwd?: string) => Promise<string[]>
  registerWorkspace: (input: { path: string; name?: string; agent?: AgentProvider }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  cloneWorkspace: (input: { parentPath: string; folderName: string; teamRemote?: string; templateRemote?: string; agent?: AgentProvider }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  addProject: (input: { workspaceId?: string; name: string; path: string; sourceBranch?: string; baseBranch?: string; mergedBranch?: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  removeProject: (input: { workspaceId?: string; projectName: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  selectWorkspace: (workspaceId: string) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  updateWorkspace: (input: Partial<WorkspaceRecord> & { id: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  updateProjectBranches: (input: { workspaceId?: string; projectName: string; branchBase?: string; push?: boolean }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  listNodeVersions: () => Promise<{ runtimes: string[] }>
  updateProjectNode: (input: { workspaceId?: string; projectName: string; nodeVersion?: string }) => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  refreshWorkspace: () => Promise<{ state: DesktopState; details: WorkspaceDetails }>
  getBaselineIntentPlan: (input: { workspaceId?: string; projectName: string }) => Promise<BaselineIntentPlan>
  getCurrentProjectPromptPreview: () => Promise<ProjectPromptPreview | undefined>
  getCurrentDraftResult: (input: { workspaceId?: string; projectName: string; runId?: string }) => Promise<{ result: DraftResultSnapshot; prompt?: ProjectPromptPreview; plan?: string } | undefined>
  getDependencyGraphSnapshot: (input: { workspaceId?: string; projectName: string }) => Promise<DependencyGraphSnapshot>
  getIterativeTask: (input: { workspaceId?: string; projectName: string }) => Promise<IterativeTaskSnapshot>
  exportIterativeTask: (input: { workspaceId?: string; projectName: string }) => Promise<IterativeTaskActionOutcome>
  exportLegacyIterativeTask: (input: { workspaceId?: string; projectName: string }) => Promise<IterativeTaskActionOutcome>
  copyIterativeTask: (input: { workspaceId?: string; projectName: string; language?: string }) => Promise<IterativeTaskActionOutcome>
  saveIterativeTask: (input: { workspaceId?: string; projectName: string; language?: string }) => Promise<IterativeTaskActionOutcome>
  setIterativeAutopilot: (input: { workspaceId?: string; projectName: string; enabled: boolean }) => Promise<{ ok: boolean; active: boolean }>
  iterativeStatus: (input: { workspaceId?: string; projectName: string }) => Promise<IterativeStatusOutcome>
  iterativeDrive: (input: { workspaceId?: string; projectName: string; autopilot?: boolean; retryInfra?: boolean; discardCandidate?: boolean; resumeTerminal?: boolean }) => Promise<IterativeDriveOutcome>
  iterativeBegin: (input: { workspaceId?: string; projectName: string; autopilot?: boolean; discovery?: { mode: 'auto' | 'none'; timeoutSeconds?: number; parallelism?: number; maxPackages?: number }; validationProfile?: { commands: string[]; unitCommand?: string; compareExistingFailures?: boolean; deferredChecks?: string }; checkOnly?: boolean; repair?: boolean; restart?: boolean }) => Promise<IterativeBeginOutcome>
  iterativeAgent: (input: { workspaceId?: string; projectName: string; autopilot?: boolean }) => Promise<IterativeAgentOutcome>
  runAction: (input: ActionInput) => Promise<{ jobId: string; runId?: string; preview: string[] }>
  cancelJob: (jobId: string) => Promise<boolean>
  pauseJob: (jobId: string) => Promise<boolean>
  sendAgentNote: (input: { jobId: string; note: string; branch?: string }) => Promise<boolean>
  recoverWithAgent: (input: { workspaceId?: string; projectName: string; note: string }) => Promise<{ jobId: string }>
  openPath: (targetPath: string) => Promise<string>
  checkForUpdates: () => Promise<{ currentVersion: string; availableVersion?: string; development?: boolean; error?: UpdateStatus }>
  setNotificationsEnabled: (enabled: boolean) => Promise<{ enabled: boolean }>
  notifyAutopilotComplete: (input: { projectName: string; published: boolean }) => Promise<void>
  installUpdate: () => Promise<void>
  storageMaintenance: (action: 'inspect' | 'clean') => Promise<{ root: string; filesystem: string; freeBytes: number; eligible: number; removed: number; failed: number; protected: number }>
  getHardwareSnapshot: () => Promise<HardwareSnapshot>
  getThemePreference: () => Promise<ThemePreference>
  setThemePreference: (preference: ThemePreference) => Promise<{ preference: ThemePreference }>
  onUpdateStatus: (handler: (event: UpdateStatus) => void) => () => void
  onJobOutput: (handler: (event: JobOutput) => void) => () => void
  onMigrationProgressChanged: (handler: (event: { jobId: string; workspaceId?: string; projectName?: string; branch: string; phase?: MigrationBranchRuntimePhase }) => void) => () => void
  onJobFinished: (handler: (event: JobFinished) => void) => () => void
  onDownloadSaved: (handler: (event: DownloadSaved) => void) => () => void
  iterativeAttempt: (input: { workspaceId?: string; projectName: string; includeRunLog?: boolean }) => Promise<{ ok: boolean; present: boolean; attempt?: IterativeAttemptView; attemptLog?: string; runLog?: string; error?: string }>
  iterativeCancel: (input: { workspaceId?: string; projectName: string }) => Promise<{ ok: boolean }>
  onIterativeAttempt: (handler: (payload: IterativeAttemptView) => void) => () => void
}

declare global {
  interface Window {
    dependencyFlow?: DependencyFlowApi
  }
}