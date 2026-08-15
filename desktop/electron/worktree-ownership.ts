import { realpathSync } from 'node:fs'

export function portablePathKey(candidate: string): string {
  // `git worktree list --porcelain` commonly prints forward-slash Windows
  // paths (`C:/Users/...`) while Electron/path APIs hand us backslashes.
  return candidate.trim().replace(/^"|"$/g, '').replace(/\\/g, '/').replace(/\/{2,}/g, '/').replace(/\/$/, '').toLowerCase()
}

function existingPathKey(candidate: string): string {
  try { return portablePathKey(realpathSync.native(candidate)) } catch { return portablePathKey(candidate) }
}

export function isToolManagedWorktreePath(candidate: string, tempPath: string): boolean {
  const normalizedTemp = existingPathKey(tempPath)
  const normalizedCandidate = existingPathKey(candidate)
  const parallelRoot = `${normalizedTemp}/dependency-flow-worktrees`
  if (normalizedCandidate === parallelRoot || normalizedCandidate.startsWith(parallelRoot + '/')) return true
  if (!normalizedCandidate.startsWith(normalizedTemp + '/')) return false
  const relative = normalizedCandidate.slice(normalizedTemp.length + 1)
  // Detached planner and verification worktrees live directly under TEMP,
  // unlike parallel workers which live under dependency-flow-worktrees/.
  // They are still fully owned/disposable Dependency Flow state and may remain
  // registered after a crash or forced application shutdown.
  return /^dependency-flow-planner-[^/]+$/.test(relative)
    || /^dependency-flow-[^/]+-(?:baseline|merged)$/.test(relative)
}

export function toolManagedWorktreeFromLegacyDeferral(reason: string, tempPath: string): string | undefined {
  if (!/PARALLEL_USER_WORKTREE_BLOCKED/i.test(reason)) return undefined
  const pathMatch = /worktree\s+([^\s,;]+)/i.exec(reason)
  const candidatePath = pathMatch?.[1]
  return candidatePath && isToolManagedWorktreePath(candidatePath, tempPath) ? candidatePath : undefined
}


export type RestartWorktreeCleanup = {
  toolManagedPaths: string[]
  blockedUserWorktrees: Array<{ branch: string; path: string }>
}

export function toolManagedWorktreePaths(
  records: Array<{ path: string; branch?: string }>,
  projectPath: string,
  tempPath: string,
): string[] {
  const primaryKey = existingPathKey(projectPath)
  const seen = new Set<string>()
  const paths: string[] = []
  for (const record of records) {
    if (existingPathKey(record.path) === primaryKey || !isToolManagedWorktreePath(record.path, tempPath)) continue
    const key = existingPathKey(record.path)
    if (seen.has(key)) continue
    seen.add(key)
    paths.push(record.path)
  }
  return paths
}

// A fresh restart may delete Dependency Flow's own temporary worktrees, but
// must never claim an arbitrary user-created worktree. The primary checkout is
// intentionally ignored here: cleanAgentStartCommands switches it to the
// source branch before cleanup commands run.
export function restartWorktreeCleanupTargets(
  records: Array<{ path: string; branch?: string }>,
  branches: string[],
  projectPath: string,
  tempPath: string,
): RestartWorktreeCleanup {
  const branchSet = new Set(branches.filter(Boolean))
  const primaryKey = existingPathKey(projectPath)
  const toolManagedPaths = toolManagedWorktreePaths(records, projectPath, tempPath)
  const toolManagedKeys = new Set(toolManagedPaths.map(existingPathKey))
  const blockedUserWorktrees: Array<{ branch: string; path: string }> = []

  for (const record of records) {
    if (existingPathKey(record.path) === primaryKey || toolManagedKeys.has(existingPathKey(record.path))) continue

    // User-created worktrees are never removed. They only block restart when
    // one of the old plan branches that we are about to delete is checked out
    // there; unrelated user worktrees can safely coexist with the new run.
    if (record.branch && branchSet.has(record.branch)) {
      blockedUserWorktrees.push({ branch: record.branch, path: record.path })
    }
  }

  return { toolManagedPaths, blockedUserWorktrees }
}
