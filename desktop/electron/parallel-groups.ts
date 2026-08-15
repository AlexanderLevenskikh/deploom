import type { MigrationBranchProgress } from './migration-progress.js'

export type ParallelGroupWave = {
  branches: MigrationBranchProgress[]
  skipped: string[]
}

// Build the whole safe queue, not just its first fixed-size wave. The executor
// keeps at most maxParallelGroups active and immediately feeds the next item
// into a freed slot, so one slow group cannot strand the remaining capacity.
export function selectParallelGroupQueue(branches: readonly MigrationBranchProgress[]): ParallelGroupWave {
  const selected: MigrationBranchProgress[] = []
  const seenPackages = new Set<string>()
  const skipped: string[] = []
  for (const branch of branches) {
    // Every incomplete branch may occupy a parallel slot. Existing checkouts
    // are filtered by the executor's ownership check: Dependency Flow can adopt
    // its own saved worktree (including partial dirty progress), but never the
    // canonical checkout or an arbitrary user worktree.
    if (!['waiting', 'created', 'partial', 'changes'].includes(branch.status)) continue
    const overlap = branch.packages.filter((packageName) => seenPackages.has(packageName))
    if (overlap.length) {
      skipped.push(`${branch.branch}: overlapping package scope (${overlap.join(', ')})`)
      continue
    }
    selected.push(branch)
    for (const packageName of branch.packages) seenPackages.add(packageName)
  }
  return { branches: selected, skipped }
}

// Kept as the small selection primitive used by tests/older callers.
export function selectParallelGroupWave(branches: readonly MigrationBranchProgress[], maxParallelGroups: number): ParallelGroupWave {
  const limit = Math.max(1, Math.trunc(maxParallelGroups || 1))
  if (limit <= 1) return { branches: [], skipped: [] }
  const queue = selectParallelGroupQueue(branches)
  const selected = queue.branches.slice(0, limit)
  return selected.length >= 2 ? { branches: selected, skipped: queue.skipped } : { branches: [], skipped: queue.skipped }
}

export async function runParallelQueue<T, R>(
  items: readonly T[],
  maxParallel: number,
  execute: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  if (!items.length) return []
  const results = new Array<R>(items.length)
  let nextIndex = 0
  const takeNext = async (): Promise<void> => {
    for (;;) {
      const index = nextIndex
      nextIndex += 1
      if (index >= items.length) return
      results[index] = await execute(items[index], index)
    }
  }
  const concurrency = Math.min(items.length, Math.max(1, Math.trunc(maxParallel || 1)))
  await Promise.all(Array.from({ length: concurrency }, () => takeNext()))
  return results
}
