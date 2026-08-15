export type AutonomyPolicy = {
  maxAgentAttemptsPerBatch: number
  maxGroupRepairAttempts: number
  maxMergeRecoveryAttempts: number
  maxIntegrationRepairAttempts: number
  maxReleaseRecoveryAttempts: number
  maxPlannerRevisions: number
  maxSamePlanRepeats: number
  allowBestEffortRelease: boolean
  allowResidualPlanDeferral: boolean
  allowSupervisorScopeExpansion: boolean
  maxParallelGroups: number
  autoDeferApprovalBlockers: boolean
  softStopOnAutonomyPlateau: boolean
}

type UnknownRecord = Record<string, unknown>

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : {}
}

function projectRecord(settings: UnknownRecord, projectName: string): UnknownRecord {
  const projects = Array.isArray(settings.projects) ? settings.projects : []
  return projects.map(asRecord).find((item) => String(item.name ?? '') === projectName) ?? {}
}

function integer(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(min, Math.min(max, Math.trunc(parsed)))
}

function boolean(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback
}

export function autonomyPolicy(settingsValue: unknown, projectName: string): AutonomyPolicy {
  const settings = asRecord(settingsValue)
  const global = asRecord(settings.autonomy)
  const project = projectRecord(settings, projectName)
  const projectAutonomy = asRecord(project.autonomy)
  const merged = { ...global, ...projectAutonomy }

  return {
    // Batch retries stay deliberately bounded: a fresh session plus the same
    // Git/evidence state is useful several times, but double-digit retries are
    // almost always token/time burn rather than new information.
    maxAgentAttemptsPerBatch: integer(merged.maxAgentAttemptsPerBatch, 6, 2, 10),
    maxGroupRepairAttempts: integer(merged.maxGroupRepairAttempts, 4, 1, 8),
    maxMergeRecoveryAttempts: integer(merged.maxMergeRecoveryAttempts, 4, 1, 8),
    maxIntegrationRepairAttempts: integer(merged.maxIntegrationRepairAttempts, 4, 1, 8),
    maxReleaseRecoveryAttempts: integer(merged.maxReleaseRecoveryAttempts, 4, 1, 8),
    // Replans are cheaper than throwing work away. Allow more *different*
    // revisions than before, but repeated identical scope/reason signatures are
    // still stopped immediately by maxSamePlanRepeats.
    maxPlannerRevisions: integer(merged.maxPlannerRevisions, 6, 1, 12),
    maxSamePlanRepeats: integer(merged.maxSamePlanRepeats, 1, 1, 3),
    allowBestEffortRelease: boolean(merged.allowBestEffortRelease, true),
    // If a planner asks for human approval solely because the current work
    // branch would need a new architectural companion/scope expansion, keep
    // the FLOW moving by deferring that bounded branch instead of stopping
    // the whole run. Infrastructure/hard planner failures never use this.
    allowResidualPlanDeferral: boolean(merged.allowResidualPlanDeferral, true),
    // Supervisor may activate exact targets only for dependencies already
    // declared by the project. Registry evidence and all verification gates
    // remain mandatory; exclusions and new direct dependencies still stop.
    allowSupervisorScopeExpansion: boolean(merged.allowSupervisorScopeExpansion, true),
    // Independent work branches are safe to execute in separate Git worktrees;
    // integration remains serialized. Two workers is a conservative default for
    // shared corporate model capacity and can be raised per project. The upper
    // bound is a safety guard, not a hidden product limit: configured values
    // such as 8 must be honoured when the model provider can sustain them.
    maxParallelGroups: integer(merged.maxParallelGroups, 2, 1, 12),
    // Approval from an LLM is advisory. When the requested expansion is not
    // allowed (explicit exclusion/new architecture), defer the bounded blocker
    // and keep the rest of the FLOW moving instead of surfacing a red banner.
    autoDeferApprovalBlockers: boolean(merged.autoDeferApprovalBlockers, true),
    // Exhausted but safely preserved autonomous work becomes a handoff/best-
    // effort outcome, not a terminal UI error. Safety/Git corruption still fail.
    softStopOnAutonomyPlateau: boolean(merged.softStopOnAutonomyPlateau, true),
  }
}

export function normalizedPlannerFailure(message: string): string {
  return message
    .replace(/[0-9a-f]{7,40}/gi, '<sha>')
    .replace(/\b\d+(?:\.\d+){1,3}\b/g, '<version>')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 1200)
}
