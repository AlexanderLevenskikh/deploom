import { existsSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

export function latestAgentCheckpoint(runsDir: string, projectName: string, branchName: string, notBeforeMs: number): string | undefined {
  if (!existsSync(runsDir)) return undefined
  const suffix = `_${projectName}_${branchName}_state.json`
  let latest: { path: string; mtimeMs: number } | undefined
  for (const name of readdirSync(runsDir)) {
    if (!name.endsWith(suffix)) continue
    const path = join(runsDir, name)
    try {
      const mtimeMs = statSync(path).mtimeMs
      if (mtimeMs < notBeforeMs || (latest && latest.mtimeMs >= mtimeMs)) continue
      latest = { path, mtimeMs }
    } catch { /* ignore a checkpoint that disappeared during the scan */ }
  }
  return latest?.path
}
