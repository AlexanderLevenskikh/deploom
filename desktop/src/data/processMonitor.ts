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

export type MonitorHealth = 'healthy' | 'quiet' | 'warning' | 'stale'

export type RunMonitorState = {
  phase: RunMonitorPhase
  active: boolean
  currentOperation?: string
  elapsedSeconds?: number
  stepElapsedSeconds?: number
  lastSignalAt?: number
  lastSignalAgeSeconds?: number
  health?: MonitorHealth
  dependency?: { current: number; total: number; name: string }
  projectCheck?: { current: number; total: number; name: string; status?: 'pass' | 'red' }
  solver?: { componentsDone?: number; componentsTotal?: number; changed?: number; constraints?: number }
  assignment?: string
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

export function deriveRunMonitor(
  logs: readonly JobOutput[],
  active: boolean,
  jobId?: string,
  migrationProgress?: MigrationProgress,
  action?: FlowAction,
  now = Date.now(),
): RunMonitorState {
  const scoped = jobId ? logs.filter((entry) => entry.jobId === jobId) : logs
  let state: RunMonitorState = { phase: active ? 'running' : 'idle', active }
  let stepStartedAt: number | undefined
  let lastSignalAt = latestReceivedAt(scoped)

  for (const entry of scoped) {
    if (entry.stream !== 'system') continue
    const line = entry.line.trim()
    const receivedAt = entry.receivedAt
    let match: RegExpExecArray | null

    match = /\[dependency (\d+)\/(\d+)\]\s+([^:]+):/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'scan',
        dependency: { current: Number(match[1]), total: Number(match[2]), name: match[3].trim() },
      }
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    if (/target planning started/i.test(line)) {
      state = { ...state, phase: 'planning', dependency: undefined, currentOperation: undefined }
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /peer solver \w+;\s+packages=(\d+),\s+components=(\d+),\s+candidates=(\d+)/i.exec(line)
    if (match) {
      state = { ...state, phase: 'solving', dependency: undefined, currentOperation: `Z3 ? ${match[2]} components ? ${match[3]} candidates` }
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /exact z3 \w+ SUMMARY;\s+components=(\d+)\/(\d+),\s+changed=(\d+),\s+constraints=(\d+)/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'solving',
        solver: {
          componentsDone: Number(match[1]),
          componentsTotal: Number(match[2]),
          changed: Number(match[3]),
          constraints: Number(match[4]),
        },
      }
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline verify \w+ iteration \d+:\s+materializing \d+ changed direct package\(s\),\s+assignment=([a-f0-9]+)/i.exec(line)
    if (match) {
      state = { ...state, phase: 'verifying', dependency: undefined, assignment: match[1], currentOperation: undefined }
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /project check (\d+)\/(\d+) started:\s+(.+)$/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'verifying',
        projectCheck: { current: Number(match[1]), total: Number(match[2]), name: match[3].trim() },
      }
      stepStartedAt = receivedAt ?? stepStartedAt
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /project check (\d+)\/(\d+) (PASS|RED)(?: exit=\d+)?:\s+(.+)$/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'verifying',
        projectCheck: {
          current: Number(match[1]),
          total: Number(match[2]),
          name: match[4].trim(),
          status: match[3].toUpperCase() === 'PASS' ? 'pass' : 'red',
        },
      }
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline localization \w+ started;\s+units=(\d+),\s+maxChecks=(\d+)/i.exec(line)
    if (match) {
      const units = Number(match[1])
      state = {
        ...state,
        phase: 'localizing',
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
      stepStartedAt = receivedAt
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline localization \w+ wave-start;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+)/i.exec(line)
    if (match && state.localization) {
      const currentUnits = Number(match[3])
      state = {
        ...state,
        phase: 'localizing',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
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
      stepStartedAt = receivedAt ?? stepStartedAt
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline localization \w+ heartbeat;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+)/i.exec(line)
    if (match && state.localization) {
      const currentUnits = Number(match[3])
      state = {
        ...state,
        phase: 'localizing',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
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
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline localization \w+ confirmation-start;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+)/i.exec(line)
    if (match && state.localization) {
      state = {
        ...state,
        phase: 'confirming',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
        localization: {
          ...state.localization,
          currentUnits: Number(match[3]),
          checksStarted: Number(match[1]),
          maxChecks: Number(match[2]),
          confirming: true,
          wave: fieldText(line, 'wave') ?? state.localization.wave,
        },
      }
      stepStartedAt = receivedAt ?? stepStartedAt
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline localization \w+ confirmation-heartbeat;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+)/i.exec(line)
    if (match && state.localization) {
      state = {
        ...state,
        phase: 'confirming',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
        localization: {
          ...state.localization,
          currentUnits: Number(match[3]),
          checksStarted: Number(match[1]),
          maxChecks: Number(match[2]),
          confirming: true,
          wave: fieldText(line, 'wave') ?? state.localization.wave,
        },
      }
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline localization \w+ shrink;.*currentUnits=(\d+).*units=(\d+),\s+packages=(\d+)/i.exec(line)
    if (match && state.localization) {
      const units = Number(match[2])
      state = {
        ...state,
        phase: 'localizing',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
        localization: {
          ...state.localization,
          currentUnits: units,
          packages: Number(match[3]),
          shrinkHistory: pushShrink(state.localization.shrinkHistory, units),
          confirming: false,
        },
      }
      stepStartedAt = receivedAt ?? stepStartedAt
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline localization \w+ finish;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+),\s+units=(\d+),\s+packages=(\d+)/i.exec(line)
    if (match && state.localization) {
      const units = Number(match[4])
      state = {
        ...state,
        phase: 'localizing',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
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
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /Baseline reproduction \w+ (\d+)\/(\d+) started;\s+literals=(\d+)/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'reproducing',
        reproduction: { current: Number(match[1]), total: Number(match[2]), literals: Number(match[3]) },
      }
      stepStartedAt = receivedAt ?? stepStartedAt
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /transient retry (\d+)\/(\d+)/i.exec(line)
    if (match) {
      state = { ...state, phase: 'retrying', retry: { current: Number(match[1]), total: Number(match[2]) } }
      stepStartedAt = receivedAt ?? stepStartedAt
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }

    match = /:\s*([^:]+):\s*running;\s*elapsed=(\d+(?:\.\d+)?)s;\s*hardTimeout=/i.exec(line)
    if (match) {
      state = {
        ...state,
        currentOperation: match[1].trim(),
      }
      if (receivedAt) lastSignalAt = receivedAt
      continue
    }
  }

  const migrationActive = active && (action === 'agent' || action === 'recover') && migrationProgress
  if (migrationActive) {
    const migration = migrationState(migrationProgress)
    const branch = activeMigrationBranch(migrationProgress)
    state = {
      ...state,
      phase: phaseForRuntime(migration.runtimePhase),
      migration,
      currentOperation: migration.runtimeDetail || migration.label || migration.branch || state.currentOperation,
    }
    const runtimeAt = branch?.runtime?.updatedAt ? Date.parse(branch.runtime.updatedAt) : NaN
    if (Number.isFinite(runtimeAt)) lastSignalAt = Math.max(lastSignalAt ?? 0, runtimeAt)
  }

  if (stepStartedAt !== undefined && active) {
    state.stepElapsedSeconds = Math.max(0, (now - stepStartedAt) / 1000)
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

  if (!active && state.phase !== 'idle') state = { ...state, active: false, phase: 'finished' }
  return state
}
