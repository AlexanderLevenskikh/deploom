import type { FlowAction, JobOutput, MigrationBranchProgress, MigrationProgress, MigrationBranchRuntimePhase } from '../types'

export type RunMonitorPhase =
  | 'idle'
  | 'scan'
  | 'planning'
  | 'solving'
  | 'verifying'
  | 'localizing'
  | 'confirming'
  | 'reproducing'
  | 'retrying'
  | 'migrating'
  | 'repairing'
  | 'merging'
  | 'running'
  | 'finished'

export type RunMonitorActivity =
  | 'idle'
  | 'scan'
  | 'planning'
  | 'solving'
  | 'verifying-assignment'
  | 'confirming-exact'
  | 'certifying-conflict'
  | 'searching-next'
  | 'project-check'
  | 'localizing'
  | 'confirming-localized'
  | 'reproducing'
  | 'retrying'
  | 'migrating'
  | 'repairing'
  | 'merging'
  | 'running'
  | 'finished'

export type MonitorHealth = 'healthy' | 'quiet' | 'warning' | 'stale'

export type RunMonitorState = {
  phase: RunMonitorPhase
  activity: RunMonitorActivity
  active: boolean
  currentOperation?: string
  runElapsedSeconds?: number
  attemptElapsedSeconds?: number
  stepElapsedSeconds?: number
  lastSignalAt?: number
  lastSignalAgeSeconds?: number
  health?: MonitorHealth
  dependency?: { current: number; total: number; name: string }
  projectCheck?: { current: number; total: number; name: string; status?: 'pass' | 'red' }
  solver?: { componentsDone?: number; componentsTotal?: number; changed?: number; constraints?: number }
  baseline?: {
    mode?: string
    iteration?: number
    maxIterations?: number
    baseIterations?: number
    allowedIterations?: number
    hardIterations?: number
    learningExtensionLimit?: number
    certifiedExtensions?: number
    learnedConstraints?: number
    exactExclusions?: number
    exactSinceLearning?: number
    generalizationAttempts?: number
    diagnostics?: number
  }
  assignment?: string
  conflict?: {
    candidate?: string
    literals?: number
    contextRadius?: number
    repeatCount?: number
    literalBudget?: number
    boundedSlice?: boolean
    seedSource?: string
    authority?: string
  }
  localization?: {
    initialUnits: number
    currentUnits: number
    packages?: number
    checksStarted: number
    maxChecks: number
    activeChecks?: number
    completedChecks?: number
    shrinkHistory: number[]
    wave?: string
    confirming?: boolean
  }
  reproduction?: { current: number; total: number; literals?: number }
  retry?: { current: number; total: number }
  migration?: {
    totalBranches: number
    completedBranches: number
    readyBranches: number
    activeBranches: number
    totalDependencies: number
    completedDependencies: number
    readyDependencies: number
    activeDependencies: number
    branch?: string
    label?: string
    runtimePhase?: MigrationBranchRuntimePhase
    runtimeDetail?: string
    metPackages?: number
    branchPackages?: number
    queuedBranches: number
    failedBranches: number
  }
}

function number(value: string | undefined): number | undefined {
  if (value === undefined) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function fieldNumber(line: string, field: string): number | undefined {
  const match = new RegExp(`(?:^|[,;]\\s*)${field}=(\\d+(?:\\.\\d+)?)`, 'i').exec(line)
  return number(match?.[1])
}

function fieldText(line: string, field: string): string | undefined {
  const match = new RegExp(`(?:^|[,;]\\s*)${field}=([^,;]+)`, 'i').exec(line)
  return match?.[1]?.trim()
}

function pushShrink(history: number[], units: number): number[] {
  if (history[history.length - 1] === units) return history
  return [...history, units]
}

function healthForAge(ageSeconds: number | undefined): MonitorHealth | undefined {
  if (ageSeconds === undefined) return undefined
  if (ageSeconds < 60) return 'healthy'
  if (ageSeconds < 5 * 60) return 'quiet'
  if (ageSeconds < 15 * 60) return 'warning'
  return 'stale'
}

function phaseForRuntime(phase: MigrationBranchRuntimePhase | undefined): RunMonitorPhase {
  if (phase === 'repairing') return 'repairing'
  if (phase === 'merging') return 'merging'
  if (phase === 'verifying' || phase === 'integration-verifying') return 'verifying'
  if (phase === 'planning') return 'planning'
  return 'migrating'
}

function activityForRuntime(phase: MigrationBranchRuntimePhase | undefined): RunMonitorActivity {
  if (phase === 'repairing') return 'repairing'
  if (phase === 'merging') return 'merging'
  if (phase === 'verifying' || phase === 'integration-verifying') return 'project-check'
  if (phase === 'planning') return 'planning'
  return 'migrating'
}

function activeMigrationBranch(progress: MigrationProgress): MigrationBranchProgress | undefined {
  const working = progress.branches.find((branch) =>
    branch.runtime && !['planning', 'queued', 'failed', 'ready'].includes(branch.runtime.phase),
  )
  if (working) return working
  const current = progress.branches.find((branch) => branch.branch === progress.currentBranch)
  if (current) return current
  return progress.branches.find((branch) => branch.runtime?.phase === 'queued')
    ?? progress.branches.find((branch) => branch.status !== 'merged')
}

function migrationState(progress: MigrationProgress): NonNullable<RunMonitorState['migration']> {
  const branch = activeMigrationBranch(progress)
  return {
    totalBranches: progress.totalBranches,
    completedBranches: progress.completedBranches,
    readyBranches: progress.readyBranches,
    activeBranches: progress.activeBranches,
    totalDependencies: progress.totalDependencies,
    completedDependencies: progress.completedDependencies,
    readyDependencies: progress.readyDependencies,
    activeDependencies: progress.activeDependencies,
    branch: branch?.branch,
    label: branch?.label,
    runtimePhase: branch?.runtime?.phase,
    runtimeDetail: branch?.runtime?.detail,
    metPackages: branch?.metPackages,
    branchPackages: branch?.packages.length,
    queuedBranches: progress.branches.filter((item) => item.runtime?.phase === 'queued').length,
    failedBranches: progress.branches.filter((item) => item.runtime?.phase === 'failed').length,
  }
}

function latestReceivedAt(logs: readonly JobOutput[]): number | undefined {
  for (let index = logs.length - 1; index >= 0; index -= 1) {
    const received = logs[index].receivedAt
    if (received !== undefined) return received
  }
  return undefined
}

function earliestReceivedAt(logs: readonly JobOutput[]): number | undefined {
  for (const entry of logs) {
    if (entry.receivedAt !== undefined) return entry.receivedAt
  }
  return undefined
}


type StructuredBaselineProgress = {
  schemaVersion: 2
  type: 'deploom-baseline-progress'
  project: string
  mode: string
  phase: string
  updatedAt: string
  [key: string]: unknown
}

const STRUCTURED_BASELINE_PREFIX = 'DEPLOOM_PROGRESS_V2 '

function structuredBaselineProgress(line: string): StructuredBaselineProgress | undefined {
  const trimmed = line.trim()
  if (!trimmed.startsWith(STRUCTURED_BASELINE_PREFIX)) return undefined
  try {
    const parsed = JSON.parse(trimmed.slice(STRUCTURED_BASELINE_PREFIX.length)) as Partial<StructuredBaselineProgress>
    if (
      parsed.schemaVersion !== 2
      || parsed.type !== 'deploom-baseline-progress'
      || typeof parsed.phase !== 'string'
      || typeof parsed.mode !== 'string'
      || typeof parsed.updatedAt !== 'string'
    ) return undefined
    return parsed as StructuredBaselineProgress
  } catch {
    return undefined
  }
}

function structuredNumber(event: StructuredBaselineProgress, key: string): number | undefined {
  const value = event[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function structuredText(event: StructuredBaselineProgress, key: string): string | undefined {
  const value = event[key]
  return typeof value === 'string' && value ? value : undefined
}

function structuredBoolean(event: StructuredBaselineProgress, key: string): boolean | undefined {
  const value = event[key]
  return typeof value === 'boolean' ? value : undefined
}

function structuredTransition(phase: string): { phase: RunMonitorPhase; activity: RunMonitorActivity } | undefined {
  if (phase === 'solve-and-verify-started' || phase === 'iteration-started') return { phase: 'solving', activity: 'solving' }
  if (phase === 'exact-assignment-blocked' || phase === 'constraint-learned' || phase === 'generalization-certified') return { phase: 'solving', activity: 'searching-next' }
  if (phase.startsWith('exact-assignment-confirmation')) return { phase: 'confirming', activity: 'confirming-exact' }
  if (phase.startsWith('generalization-')) return { phase: 'confirming', activity: 'certifying-conflict' }
  if (phase.startsWith('localization-confirmation')) return { phase: 'confirming', activity: 'confirming-localized' }
  if (phase.startsWith('localization-')) return { phase: 'localizing', activity: 'localizing' }
  if (phase.startsWith('reproduction-')) return { phase: 'reproducing', activity: 'reproducing' }
  if (phase.includes('verification') || phase === 'project-preflight') return { phase: 'verifying', activity: 'verifying-assignment' }
  if (phase === 'budget-exhausted') return { phase: 'solving', activity: 'searching-next' }
  return undefined
}

export function deriveRunMonitor(
  logs: readonly JobOutput[],
  active: boolean,
  jobId?: string,
  migrationProgress?: MigrationProgress,
  action?: FlowAction,
  now = Date.now(),
  runStartedAt?: number,
): RunMonitorState {
  // A FLOW command is one logical run even when the Electron orchestrator
  // starts another child process for a bounded retry. Prefer the explicit run
  // start over a process/job id so heartbeat and elapsed time keep following
  // the current attempt instead of freezing on attempt 1.
  const scoped = runStartedAt !== undefined
    ? logs.filter((entry) => entry.receivedAt === undefined || entry.receivedAt >= runStartedAt - 1_000)
    : jobId ? logs.filter((entry) => entry.jobId === jobId) : logs

  let state: RunMonitorState = {
    phase: active ? 'running' : 'idle',
    activity: active ? 'running' : 'idle',
    active,
  }
  const logicalRunStartedAt = runStartedAt ?? earliestReceivedAt(scoped)
  let attemptStartedAt = logicalRunStartedAt
  let phaseStartedAt = logicalRunStartedAt
  let lastSignalAt = latestReceivedAt(scoped)

  const move = (phase: RunMonitorPhase, activity: RunMonitorActivity, at?: number) => {
    if ((state.phase !== phase || state.activity !== activity) && at !== undefined) phaseStartedAt = at
    state = { ...state, phase, activity }
  }

  for (const entry of scoped) {
    if (entry.stream !== 'system') continue
    const line = entry.line.trim()
    const receivedAt = entry.receivedAt
    let match: RegExpExecArray | null


    const structured = structuredBaselineProgress(line)
    if (structured) {
      const parsedUpdatedAt = Date.parse(structured.updatedAt)
      const eventAt = receivedAt ?? (Number.isFinite(parsedUpdatedAt) ? parsedUpdatedAt : undefined)
      const transition = structuredTransition(structured.phase)
      if (transition) move(transition.phase, transition.activity, eventAt)

      const iteration = structuredNumber(structured, 'iteration')
      const assignment = structuredText(structured, 'assignment')
      const nextBaseline = {
        ...state.baseline,
        mode: structured.mode,
        iteration: iteration ?? state.baseline?.iteration,
        maxIterations: structuredNumber(structured, 'maxIterations') ?? state.baseline?.maxIterations,
        baseIterations: structuredNumber(structured, 'baseIterations') ?? state.baseline?.baseIterations,
        allowedIterations: structuredNumber(structured, 'allowedIterations') ?? state.baseline?.allowedIterations,
        hardIterations: structuredNumber(structured, 'hardIterations') ?? state.baseline?.hardIterations,
        learningExtensionLimit: structuredNumber(structured, 'learningExtensionLimit') ?? state.baseline?.learningExtensionLimit,
        certifiedExtensions: structuredNumber(structured, 'certifiedExtensions') ?? state.baseline?.certifiedExtensions,
        learnedConstraints: structuredNumber(structured, 'learnedConstraints') ?? state.baseline?.learnedConstraints,
        exactExclusions: structuredNumber(structured, 'exactExclusions') ?? state.baseline?.exactExclusions,
        exactSinceLearning: structuredNumber(structured, 'exactSinceLearning') ?? state.baseline?.exactSinceLearning,
        generalizationAttempts: structuredNumber(structured, 'generalizationAttempts') ?? state.baseline?.generalizationAttempts,
        diagnostics: structuredNumber(structured, 'diagnostics') ?? state.baseline?.diagnostics,
      }

      state = {
        ...state,
        baseline: nextBaseline,
        assignment: assignment ?? state.assignment,
      }

      if (structured.phase.startsWith('generalization-')) {
        state = {
          ...state,
          conflict: {
            ...state.conflict,
            candidate: structuredText(structured, 'candidate') ?? state.conflict?.candidate,
            literals: structuredNumber(structured, 'literals') ?? state.conflict?.literals,
            contextRadius: structuredNumber(structured, 'contextRadius') ?? state.conflict?.contextRadius,
            repeatCount: structuredNumber(structured, 'repeatCount') ?? state.conflict?.repeatCount,
            literalBudget: structuredNumber(structured, 'literalBudget') ?? state.conflict?.literalBudget,
            boundedSlice: structuredBoolean(structured, 'boundedSlice') ?? state.conflict?.boundedSlice,
            seedSource: structuredText(structured, 'seedSource') ?? state.conflict?.seedSource,
            authority: structuredText(structured, 'authority') ?? state.conflict?.authority,
          },
        }
      }

      if (eventAt !== undefined) lastSignalAt = Math.max(lastSignalAt ?? 0, eventAt)
      continue
    }

    match = /transient retry (\d+)\/(\d+)/i.exec(line)
    if (match) {
      attemptStartedAt = receivedAt ?? attemptStartedAt
      move('retrying', 'retrying', receivedAt)
      state = { ...state, retry: { current: Number(match[1]), total: Number(match[2]) }, dependency: undefined, projectCheck: undefined }
      continue
    }

    match = /\[dependency (\d+)\/(\d+)\]\s+([^:]+):/i.exec(line)
    if (match) {
      move('scan', 'scan', receivedAt)
      state = {
        ...state,
        dependency: { current: Number(match[1]), total: Number(match[2]), name: match[3].trim() },
        projectCheck: undefined,
      }
      continue
    }

    if (/target planning started/i.test(line)) {
      move('planning', 'planning', receivedAt)
      state = { ...state, dependency: undefined, currentOperation: undefined }
      continue
    }

    match = /Baseline solve-and-verify \w+ started;.*maxIterations=(\d+)/i.exec(line)
    if (match) {
      move('solving', 'solving', receivedAt)
      state = { ...state, baseline: { ...state.baseline, maxIterations: Number(match[1]) }, dependency: undefined }
      continue
    }

    match = /peer solver \w+;\s+packages=(\d+),\s+components=(\d+),\s+candidates=(\d+)/i.exec(line)
    if (match) {
      move('solving', 'solving', receivedAt)
      state = { ...state, dependency: undefined, currentOperation: undefined }
      continue
    }

    match = /exact z3 \w+ SUMMARY;\s+components=(\d+)\/(\d+),\s+changed=(\d+),\s+constraints=(\d+)/i.exec(line)
    if (match) {
      move('solving', 'solving', receivedAt)
      state = {
        ...state,
        solver: {
          componentsDone: Number(match[1]),
          componentsTotal: Number(match[2]),
          changed: Number(match[3]),
          constraints: Number(match[4]),
        },
      }
      continue
    }

    match = /Baseline verify \w+ iteration (\d+):.*assignment=([a-f0-9]+)/i.exec(line)
    if (match) {
      move('verifying', 'verifying-assignment', receivedAt)
      state = {
        ...state,
        dependency: undefined,
        projectCheck: undefined,
        assignment: match[2],
        baseline: { ...state.baseline, iteration: Number(match[1]) },
        currentOperation: undefined,
      }
      continue
    }

    match = /Baseline exact-assignment confirmation \w+ started;\s*assignment=([a-f0-9]+)/i.exec(line)
    if (match) {
      move('confirming', 'confirming-exact', receivedAt)
      state = { ...state, assignment: match[1], currentOperation: undefined }
      continue
    }

    if (/graph certification \w+ \d+\/\d+:/i.test(line) || /graph-guided generalization proposal \w+/i.test(line)) {
      move('confirming', 'certifying-conflict', receivedAt)
      continue
    }

    if (/blocked exact failing assignment .* as global exact exclusion/i.test(line) || /global exact coordinator \w+ accepted;/i.test(line)) {
      move('solving', 'searching-next', receivedAt)
      continue
    }

    match = /project check (\d+)\/(\d+) started:\s+(.+)$/i.exec(line)
    if (match) {
      move('verifying', 'project-check', receivedAt)
      state = {
        ...state,
        projectCheck: { current: Number(match[1]), total: Number(match[2]), name: match[3].trim() },
      }
      continue
    }

    match = /project check (\d+)\/(\d+) (PASS|RED)(?: exit=\d+)?:\s+(.+)$/i.exec(line)
    if (match) {
      move('verifying', 'project-check', receivedAt)
      state = {
        ...state,
        projectCheck: {
          current: Number(match[1]),
          total: Number(match[2]),
          name: match[4].trim(),
          status: match[3].toUpperCase() === 'PASS' ? 'pass' : 'red',
        },
      }
      continue
    }

    match = /Baseline localization \w+ started;\s+units=(\d+),\s+maxChecks=(\d+)/i.exec(line)
    if (match) {
      const units = Number(match[1])
      move('localizing', 'localizing', receivedAt)
      state = {
        ...state,
        projectCheck: undefined,
        currentOperation: undefined,
        localization: {
          initialUnits: units,
          currentUnits: units,
          checksStarted: 0,
          maxChecks: Number(match[2]),
          shrinkHistory: [units],
        },
      }
      continue
    }

    match = /Baseline localization \w+ wave-start;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+)/i.exec(line)
    if (match && state.localization) {
      const currentUnits = Number(match[3])
      move('localizing', 'localizing', receivedAt)
      state = {
        ...state,
        localization: {
          ...state.localization,
          currentUnits,
          checksStarted: Number(match[1]),
          maxChecks: Number(match[2]),
          activeChecks: fieldNumber(line, 'active') ?? state.localization.activeChecks,
          completedChecks: fieldNumber(line, 'completed') ?? state.localization.completedChecks,
          shrinkHistory: pushShrink(state.localization.shrinkHistory, currentUnits),
          wave: fieldText(line, 'wave') ?? state.localization.wave,
          confirming: false,
        },
      }
      continue
    }

    match = /Baseline localization \w+ heartbeat;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+)/i.exec(line)
    if (match && state.localization) {
      const currentUnits = Number(match[3])
      move('localizing', 'localizing', receivedAt)
      state = {
        ...state,
        localization: {
          ...state.localization,
          currentUnits,
          checksStarted: Number(match[1]),
          maxChecks: Number(match[2]),
          activeChecks: fieldNumber(line, 'active') ?? state.localization.activeChecks,
          completedChecks: fieldNumber(line, 'completed') ?? state.localization.completedChecks,
          shrinkHistory: pushShrink(state.localization.shrinkHistory, currentUnits),
          wave: fieldText(line, 'wave') ?? state.localization.wave,
        },
      }
      continue
    }

    match = /Baseline localization \w+ confirmation-(?:start|heartbeat);.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+)/i.exec(line)
    if (match && state.localization) {
      move('confirming', 'confirming-localized', receivedAt)
      state = {
        ...state,
        localization: {
          ...state.localization,
          currentUnits: Number(match[3]),
          checksStarted: Number(match[1]),
          maxChecks: Number(match[2]),
          confirming: true,
          wave: fieldText(line, 'wave') ?? state.localization.wave,
        },
      }
      continue
    }

    match = /Baseline localization \w+ shrink;.*currentUnits=(\d+).*units=(\d+),\s+packages=(\d+)/i.exec(line)
    if (match && state.localization) {
      const units = Number(match[2])
      move('localizing', 'localizing', receivedAt)
      state = {
        ...state,
        localization: {
          ...state.localization,
          currentUnits: units,
          packages: Number(match[3]),
          shrinkHistory: pushShrink(state.localization.shrinkHistory, units),
          confirming: false,
        },
      }
      continue
    }

    match = /Baseline localization \w+ finish;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+),\s+units=(\d+),\s+packages=(\d+)/i.exec(line)
    if (match && state.localization) {
      const units = Number(match[4])
      move('localizing', 'localizing', receivedAt)
      state = {
        ...state,
        localization: {
          ...state.localization,
          currentUnits: units,
          packages: Number(match[5]),
          checksStarted: Number(match[1]),
          maxChecks: Number(match[2]),
          activeChecks: 0,
          shrinkHistory: pushShrink(state.localization.shrinkHistory, units),
          confirming: false,
        },
      }
      continue
    }

    match = /Baseline reproduction \w+ (\d+)\/(\d+) started;\s+literals=(\d+)/i.exec(line)
    if (match) {
      move('reproducing', 'reproducing', receivedAt)
      state = {
        ...state,
        reproduction: { current: Number(match[1]), total: Number(match[2]), literals: Number(match[3]) },
      }
      continue
    }

    match = /:\s*([^:]+):\s*running;\s*elapsed=(\d+(?:\.\d+)?)s;\s*hardTimeout=/i.exec(line)
    if (match) {
      state = { ...state, currentOperation: match[1].trim() }
      continue
    }
  }

  const migrationActive = active && (action === 'agent' || action === 'recover') && migrationProgress
  if (migrationActive) {
    const migration = migrationState(migrationProgress)
    const branch = activeMigrationBranch(migrationProgress)
    const nextPhase = phaseForRuntime(migration.runtimePhase)
    const nextActivity = activityForRuntime(migration.runtimePhase)
    move(nextPhase, nextActivity, branch?.runtime?.updatedAt ? Date.parse(branch.runtime.updatedAt) : undefined)
    state = {
      ...state,
      migration,
      currentOperation: migration.runtimeDetail || migration.label || migration.branch || state.currentOperation,
    }
    const runtimeAt = branch?.runtime?.updatedAt ? Date.parse(branch.runtime.updatedAt) : NaN
    if (Number.isFinite(runtimeAt)) lastSignalAt = Math.max(lastSignalAt ?? 0, runtimeAt)
  }

  const clockEnd = active ? now : lastSignalAt ?? now
  if (logicalRunStartedAt !== undefined) {
    state.runElapsedSeconds = Math.max(0, (clockEnd - logicalRunStartedAt) / 1000)
  }
  if (attemptStartedAt !== undefined) {
    state.attemptElapsedSeconds = Math.max(0, (clockEnd - attemptStartedAt) / 1000)
  }
  if (phaseStartedAt !== undefined) {
    state.stepElapsedSeconds = Math.max(0, (clockEnd - phaseStartedAt) / 1000)
  }

  if (lastSignalAt !== undefined) {
    const lastSignalAgeSeconds = Math.max(0, (now - lastSignalAt) / 1000)
    state = {
      ...state,
      lastSignalAt,
      lastSignalAgeSeconds,
      health: healthForAge(lastSignalAgeSeconds),
    }
  }

  if (!active && state.phase !== 'idle') state = { ...state, active: false, phase: 'finished', activity: 'finished' }
  return state
}
