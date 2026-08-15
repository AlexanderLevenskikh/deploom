export type ProjectPromptWorkspace = {
  latestPromptPath?: string
  latestPromptPaths?: Record<string, string>
}

export function scopedPromptPath(workspace: ProjectPromptWorkspace, projectName: string): string | undefined {
  const value = workspace.latestPromptPaths?.[projectName]
  return typeof value === 'string' && value.trim() ? value : undefined
}

export function rememberScopedPromptPath(workspace: ProjectPromptWorkspace, projectName: string, promptPath: string): void {
  workspace.latestPromptPaths = { ...workspace.latestPromptPaths, [projectName]: promptPath }
  // Keep the legacy scalar for downgrade/backward compatibility only. New
  // project-specific reads must use latestPromptPaths/scopedPromptPath.
  workspace.latestPromptPath = promptPath
}

export function forgetScopedPromptPath(workspace: ProjectPromptWorkspace, projectName: string): string | undefined {
  const current = scopedPromptPath(workspace, projectName)
  if (!current) return undefined
  const next = { ...(workspace.latestPromptPaths ?? {}) }
  delete next[projectName]
  workspace.latestPromptPaths = Object.keys(next).length ? next : undefined
  // The scalar is compatibility-only. Never leave it pointing at a prompt we
  // have deliberately invalidated for a fresh Baseline epoch.
  if (workspace.latestPromptPath === current) workspace.latestPromptPath = undefined
  return current
}

export function roadmapContainsProject(value: unknown, projectName: string): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const root = value as Record<string, unknown>
  const projects = root.projects && typeof root.projects === 'object' && !Array.isArray(root.projects)
    ? root.projects as Record<string, unknown>
    : undefined
  if (projects && Object.prototype.hasOwnProperty.call(projects, projectName)) return true
  const health = root.project_health && typeof root.project_health === 'object' && !Array.isArray(root.project_health)
    ? root.project_health as Record<string, unknown>
    : undefined
  return Boolean(health && Object.prototype.hasOwnProperty.call(health, projectName))
}

export type ProjectScopedEvent = { workspaceId?: string; projectName?: string }

export function eventBelongsToProject(event: ProjectScopedEvent, workspaceId?: string, projectName?: string): boolean {
  if (!workspaceId || !projectName) return false
  return event.workspaceId === workspaceId && event.projectName === projectName
}
