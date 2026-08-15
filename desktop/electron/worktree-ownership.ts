import { realpathSync } from 'node:fs'

export function portablePathKey(candidate: string, platform: NodeJS.Platform = process.platform): string {
  // `git worktree list --porcelain` commonly prints forward-slash Windows
  // paths (`C:/Users/...`) while Electron/path APIs hand us backslashes.
  // Windows paths are case-insensitive; POSIX paths are not. Lower-casing on
  // Linux/macOS could make a user worktree look like a DepLoom-owned TEMP
  // worktree on a case-sensitive filesystem, so preserve case there.
  const normalized = candidate.trim().replace(/^"|"$/g, '').replace(/\\/g, '/').replace(/\/{2,}/g, '/').replace(/\/$/, '')
  return platform === 'win32' ? normalized.toLowerCase() : normalized
}

function existingPathKey(candidate: string, platform: NodeJS.Platform = process.platform): string {
  try { return portablePathKey(realpathSync.native(candidate), platform) } catch { return portablePathKey(candidate, platform) }
}

export function isToolManagedWorktreePath(candidate: string, tempPath: string, platform: NodeJS.Platform = process.platform): boolean {
  const normalizedTemp = existingPathKey(tempPath, platform)
  const normalizedCandidate = existingPathKey(candidate, platform)
  const parallelRoot = `${normalizedTemp}/dependency-flow-worktrees`
  if (normalizedCandidate === parallelRoot || normalizedCandidate.startsWith(parallelRoot + '/')) return true
  if (!normalizedCandidate.startsWith(normalizedTemp + '/')) return false
  const relative = normalizedCandidate.slice(normalizedTemp.length + 1)
  // Detached planner and verification worktrees live directly under TEMP,
  // unlike parallel workers which live under dependency-flow-worktrees/.
  // They are still fully owned/disposable DepLoom state and may remain
  // registered after a crash or forced application shutdown.
  return /^dependency-flow-planner-[^/]+$/.test(relative)
    || /^dependency-flow-[^/]+-(?:baseline|merged)$/.test(relative)
}

export function toolManagedWorktreeFromLegacyDeferral(reason: string, tempPath: string, platform: NodeJS.Platform = process.platform): string | undefined {
  if (!/PARALLEL_USER_WORKTREE_BLOCKED/i.test(reason)) return undefined
  const pathMatch = /worktree\s+([^\s,;]+)/i.exec(reason)
  const candidatePath = pathMatch?.[1]
  return candidatePath && isToolManagedWorktreePath(candidatePath, tempPath, platform) ? candidatePath : undefined
}

export type RestartWorktreeCleanup = {
  toolManagedPaths: string[]
  blockedUserWorktrees: Array<{ branch: string; path: string }>
}

export function toolManagedWorktreePaths(
  records: Array<{ path: string; branch?: string }>,
  projectPath: string,
  tempPath: string,
  platform: NodeJS.Platform = process.platform,
): string[] {
  const primaryKey = existingPathKey(projectPath, platform)
  const seen = new Set<string>()
  const paths: string[] = []
  for (const record of records) {
    if (existingPathKey(record.path, platform) === primaryKey || !isToolManagedWorktreePath(record.path, tempPath, platform)) continue
    const key = existingPathKey(record.path, platform)
    if (seen.has(key)) continue
    seen.add(key)
    paths.push(record.path)
  }
  return paths
}

// A fresh restart may delete DepLoom's own temporary worktrees, but
// must never claim an arbitrary user-created worktree. The primary checkout is
// intentionally ignored here: cleanAgentStartCommands switches it to the
// source branch before cleanup commands run.
export function restartWorktreeCleanupTargets(
  records: Array<{ path: string; branch?: string }>,
  branches: string[],
  projectPath: string,
  tempPath: string,
  platform: NodeJS.Platform = process.platform,
): RestartWorktreeCleanup {
  const branchSet = new Set(branches.filter(Boolean))
  const primaryKey = existingPathKey(projectPath, platform)
  const toolManagedPaths = toolManagedWorktreePaths(records, projectPath, tempPath, platform)
  const toolManagedKeys = new Set(toolManagedPaths.map((path) => existingPathKey(path, platform)))
  const blockedUserWorktrees: Array<{ branch: string; path: string }> = []

  for (const record of records) {
    if (existingPathKey(record.path, platform) === primaryKey || toolManagedKeys.has(existingPathKey(record.path, platform))) continue

    // User-created worktrees are never removed. They only block restart when
    // one of the old plan branches that we are about to delete is checked out
    // there; unrelated user worktrees can safely coexist with the new run.
    if (record.branch && branchSet.has(record.branch)) {
      blockedUserWorktrees.push({ branch: record.branch, path: record.path })
    }
  }

  return { toolManagedPaths, blockedUserWorktrees }
}
