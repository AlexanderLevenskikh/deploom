// Keep the exact roadmap row objects for the packages in one execution batch,
// but drop unrelated project rows. The agent still gets the same untruncated
// release intelligence and target artifact evidence; it simply no longer has
// to repeatedly search an ~MB project-wide JSON file for a handful of rows.
export function batchRoadmapDocument(
  roadmap: unknown,
  projectName: string,
  packages: readonly string[],
): unknown {
  if (!roadmap || typeof roadmap !== 'object' || Array.isArray(roadmap)) return roadmap
  const root = roadmap as Record<string, unknown>
  const projects = root.projects && typeof root.projects === 'object' && !Array.isArray(root.projects)
    ? root.projects as Record<string, unknown>
    : undefined
  const projectRows = projects?.[projectName]
  if (!Array.isArray(projectRows)) return roadmap
  const wanted = new Set(packages)
  const filteredRows = projectRows.filter((row) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return false
    const packageName = String((row as Record<string, unknown>).name ?? (row as Record<string, unknown>).package ?? '')
    return wanted.has(packageName)
  })
  const found = new Set(filteredRows.map((row) => String((row as Record<string, unknown>).name ?? (row as Record<string, unknown>).package ?? '')))
  // Missing release intelligence is a quality failure for the optimization,
  // not a reason to hand the agent a partial dossier. Fall back to the full
  // canonical roadmap and let materializeBatchRoadmap keep all facts.
  if ([...wanted].some((packageName) => !found.has(packageName))) return roadmap
  const projectHealth = root.project_health && typeof root.project_health === 'object' && !Array.isArray(root.project_health)
    ? root.project_health as Record<string, unknown>
    : undefined
  return {
    ...root,
    projects: { [projectName]: filteredRows },
    ...(projectHealth?.[projectName] !== undefined ? { project_health: { [projectName]: projectHealth[projectName] } } : {}),
  }
}

export function replaceRoadmapPath(prompt: string, originalPath: string, batchPath: string): string {
  if (!originalPath || originalPath === batchPath) return prompt
  return prompt.split(originalPath).join(batchPath)
}
