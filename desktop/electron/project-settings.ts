export type BranchProject = {
  name: string
  git?: Record<string, unknown>
}

export type ProjectLevel = {
  status: 'red' | 'yellow' | 'green'
  lagOkPct?: number
  remainingYellow?: number
  remainingGreen?: number
  measuredAt?: string
}

export function applyBranchBase<T extends BranchProject>(project: T, branchBase: string, push?: boolean): T {
  return {
    ...project,
    git: {
      ...project.git,
      baseBranch: branchBase,
      branchPrefix: branchBase,
      mergedBranch: `${branchBase}-merged`,
      ...(typeof push === 'boolean' ? { push } : {}),
    },
  }
}

export function projectLevelsFromRoadmap(value: unknown, measuredAt?: string): Record<string, ProjectLevel> {
  if (!value || typeof value !== 'object') return {}
  const health = (value as { project_health?: unknown }).project_health
  if (!health || typeof health !== 'object') return {}
  const levels: Record<string, ProjectLevel> = {}
  const projects = (value as { projects?: unknown }).projects
  const roadmapProjects = projects && typeof projects === 'object' ? projects as Record<string, unknown> : {}
  const noAction = new Set(['', '-', '—', 'нет действия', 'ничего не делать'])
  const remaining = (project: string, target: 'yellow' | 'green'): number | undefined => {
    const rows = roadmapProjects[project]
    if (!Array.isArray(rows)) return undefined
    return rows.filter((item) => {
      if (!item || typeof item !== 'object' || (item as { scope_excluded?: unknown }).scope_excluded === true) return false
      return !noAction.has(String((item as Record<string, unknown>)[`target_${target}`] ?? '').trim().toLowerCase())
    }).length
  }
  for (const [project, raw] of Object.entries(health)) {
    if (!raw || typeof raw !== 'object') continue
    const status = (raw as { status?: unknown }).status
    if (status !== 'red' && status !== 'yellow' && status !== 'green') continue
    const lag = (raw as { lag_ok_pct?: unknown }).lag_ok_pct
    const remainingYellow = remaining(project, 'yellow')
    const remainingGreen = remaining(project, 'green')
    levels[project] = {
      status,
      ...(typeof lag === 'number' && Number.isFinite(lag) ? { lagOkPct: lag } : {}),
      ...(typeof remainingYellow === 'number' ? { remainingYellow } : {}),
      ...(typeof remainingGreen === 'number' ? { remainingGreen } : {}),
      ...(measuredAt ? { measuredAt } : {}),
    }
  }
  return levels
}
function projectLevelFromHealth(raw: unknown, measuredAt?: string): ProjectLevel | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const status = (raw as { status?: unknown }).status
  if (status !== 'red' && status !== 'yellow' && status !== 'green') return undefined
  const value = raw as { lag_ok_pct?: unknown; lagOkPct?: unknown }
  const lag = typeof value.lag_ok_pct === 'number' ? value.lag_ok_pct : value.lagOkPct
  return { status, ...(typeof lag === 'number' && Number.isFinite(lag) ? { lagOkPct: lag } : {}), ...(measuredAt ? { measuredAt } : {}) }
}

export function projectLevelsFromHistorySnapshots(value: unknown): Record<string, ProjectLevel> {
  if (!Array.isArray(value)) return {}
  const levels: Record<string, ProjectLevel> = {}
  for (const snapshot of value) {
    if (!snapshot || typeof snapshot !== 'object') continue
    const measuredAt = typeof (snapshot as { capturedAt?: unknown }).capturedAt === 'string' ? String((snapshot as { capturedAt: string }).capturedAt) : undefined
    const projects = (snapshot as { projects?: unknown }).projects
    if (!projects || typeof projects !== 'object') continue
    for (const [project, raw] of Object.entries(projects)) {
      if (levels[project] || !raw || typeof raw !== 'object') continue
      const level = projectLevelFromHealth((raw as { health?: unknown }).health ?? raw, measuredAt)
      if (level) levels[project] = level
    }
  }
  return levels
}

export function preferNewestProjectLevels(history: Record<string, ProjectLevel>, roadmap: Record<string, ProjectLevel>): Record<string, ProjectLevel> {
  const result = { ...history }
  for (const [project, current] of Object.entries(roadmap)) {
    const previous = result[project]
    const previousTime = previous?.measuredAt ? Date.parse(previous.measuredAt) : Number.NaN
    const currentTime = current.measuredAt ? Date.parse(current.measuredAt) : Number.NaN
    if (!previous || Number.isNaN(previousTime) || Number.isNaN(currentTime) || currentTime >= previousTime) result[project] = current
  }
  return result
}
