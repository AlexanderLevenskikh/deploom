export type ReleasePolicy = {
  commitMessage: string
  finalGateCommands: string[]
}

type UnknownRecord = Record<string, unknown>

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : {}
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim())
    .filter(Boolean)
}

function releaseRecord(value: unknown): UnknownRecord {
  return asRecord(asRecord(value).release)
}

export function releasePolicyForProject(settings: unknown, projectName: string): ReleasePolicy {
  const root = asRecord(settings)
  const globalRelease = releaseRecord(root)
  const projects = Array.isArray(root.projects) ? root.projects : []
  const project = projects
    .map(asRecord)
    .find((item) => String(item.name ?? '') === projectName)

  const git = asRecord(project?.git)
  const hasProjectRelease = Boolean(project && Object.prototype.hasOwnProperty.call(project, 'release'))
  const projectRelease = hasProjectRelease
    ? asRecord(project?.release)
    : asRecord(git.release)

  const merged = { ...globalRelease, ...projectRelease }
  const commitMessage = typeof merged.commitMessage === 'string' && merged.commitMessage.trim()
    ? merged.commitMessage.trim()
    : 'chore(deps): update dependencies'
  return {
    commitMessage,
    finalGateCommands: stringList(merged.finalGateCommands),
  }
}

export function releaseGateCommands(settings: unknown, projectName: string, extraCommand?: string): string[] {
  const configured = releasePolicyForProject(settings, projectName).finalGateCommands
  const extra = extraCommand?.trim()
  return [...new Set([...configured, ...(extra ? [extra] : [])])]
}
