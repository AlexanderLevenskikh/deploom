// The dashboard shows every project in the workspace at once, so a scope
// change can land on a project other than the one currently selected in the
// desktop app. Recalculating "the selected project" would then rebuild the
// wrong roadmap and silently leave the edited one stale. Comparing the saved
// dashboard state before and after the write says exactly which projects were
// touched, so only those get recalculated -- and nothing is missed.

type OverridesByProject = Record<string, Record<string, unknown>>

function overridesByProject(raw: string): OverridesByProject {
  try {
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object') return {}
    const overrides = (parsed as Record<string, unknown>).packageOverrides
    if (!overrides || typeof overrides !== 'object') return {}
    const result: OverridesByProject = {}
    for (const [project, value] of Object.entries(overrides as Record<string, unknown>)) {
      if (value && typeof value === 'object') result[project] = value as Record<string, unknown>
    }
    return result
  } catch {
    return {}
  }
}

// Key order is irrelevant to meaning here (the dashboard rewrites the whole
// object on every save), so compare canonically rather than by raw text --
// otherwise every save would look like every project changed.
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => entry !== undefined)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, entry]) => `${JSON.stringify(key)}:${canonical(entry)}`)
      .join(',')}}`
  }
  return JSON.stringify(value ?? null)
}

export function changedOverrideProjects(previousRaw: string, nextRaw: string): string[] {
  const previous = overridesByProject(previousRaw)
  const next = overridesByProject(nextRaw)
  const projects = new Set([...Object.keys(previous), ...Object.keys(next)])
  return [...projects]
    .filter((project) => canonical(previous[project] ?? {}) !== canonical(next[project] ?? {}))
    .sort((a, b) => a.localeCompare(b))
}
