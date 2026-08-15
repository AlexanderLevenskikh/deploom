export type AgentExecutionBatch = {
  packages: string[]
  compatibilityCohorts: string[]
}

export type AgentBatchingOptions = {
  // Compatibility cohorts are atomic for dependency assignment, but once the
  // orchestrator has materialized the *whole* branch assignment they no
  // longer need to be atomic for LLM source/config work. Keep false as the
  // compatibility default for legacy/in-progress branches that were not
  // pre-materialized by the deterministic control plane.
  splitCompatibilityCohorts?: boolean
}

type ManifestRow = {
  packageName: string
  compatibilityCohort: string
  shouldUpdate: boolean
}

function manifestRows(manifest: Record<string, unknown> | undefined): ManifestRow[] {
  if (!manifest || !Array.isArray(manifest.rows)) return []
  const columns = Array.isArray(manifest.columns)
    ? manifest.columns.filter((value): value is string => typeof value === 'string')
    : undefined
  return manifest.rows.flatMap((value): ManifestRow[] => {
    const raw = Array.isArray(value) && columns
      ? Object.fromEntries(columns.map((column, index) => [column, value[index]]))
      : value
    if (!raw || typeof raw !== 'object') return []
    const row = raw as Record<string, unknown>
    const packageName = String(row.package ?? row.name ?? '').trim()
    if (!packageName) return []
    return [{
      packageName,
      compatibilityCohort: String(row.compatibilityCohort ?? row.compatibility_cohort ?? '').trim(),
      shouldUpdate: row.shouldUpdate === true,
    }]
  })
}

// Git grouping answers "what must eventually be merged together". It should
// not also force an LLM to hold dozens of unrelated packages in one ever-
// growing conversation. Legacy branches keep every named compatibility
// cohort atomic because the agent may still own package materialization. New
// control-plane-materialized branches can opt into splitting the same cohort
// into bounded semantic LLM batches without changing the atomic dependency
// assignment itself.
export function buildAgentExecutionBatches(
  manifest: Record<string, unknown> | undefined,
  maxPackages = 6,
  options: AgentBatchingOptions = {},
): AgentExecutionBatch[] {
  const rows = manifestRows(manifest).filter((row) => row.shouldUpdate)
  if (!rows.length) return []
  const safeMax = Math.max(1, Math.floor(maxPackages))

  const cohortMembers = new Map<string, string[]>()
  const emittedCohorts = new Set<string>()
  for (const row of rows) {
    if (!row.compatibilityCohort) continue
    const current = cohortMembers.get(row.compatibilityCohort) ?? []
    if (!current.includes(row.packageName)) current.push(row.packageName)
    cohortMembers.set(row.compatibilityCohort, current)
  }

  const units: Array<{ packages: string[]; cohort?: string }> = []
  const seenPackages = new Set<string>()
  for (const row of rows) {
    if (seenPackages.has(row.packageName)) continue
    if (row.compatibilityCohort && !options.splitCompatibilityCohorts) {
      if (emittedCohorts.has(row.compatibilityCohort)) continue
      const packages = cohortMembers.get(row.compatibilityCohort) ?? [row.packageName]
      packages.forEach((packageName) => seenPackages.add(packageName))
      emittedCohorts.add(row.compatibilityCohort)
      units.push({ packages, cohort: row.compatibilityCohort })
      continue
    }
    seenPackages.add(row.packageName)
    units.push({ packages: [row.packageName], ...(row.compatibilityCohort ? { cohort: row.compatibilityCohort } : {}) })
  }

  const batches: AgentExecutionBatch[] = []
  let currentPackages: string[] = []
  let currentCohorts: string[] = []
  const flush = () => {
    if (!currentPackages.length) return
    batches.push({ packages: currentPackages, compatibilityCohorts: currentCohorts })
    currentPackages = []
    currentCohorts = []
  }
  for (const unit of units) {
    if (currentPackages.length && currentPackages.length + unit.packages.length > safeMax) flush()
    currentPackages.push(...unit.packages)
    if (unit.cohort && !currentCohorts.includes(unit.cohort)) currentCohorts.push(unit.cohort)
    if (currentPackages.length >= safeMax) flush()
  }
  flush()
  return batches
}
