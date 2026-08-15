import { createHash } from 'node:crypto'
import { mkdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import type { PlannerResult } from './planner-session.js'

export type PlannerCacheIdentity = {
  projectName: string
  promptMarkdown: string
  normalizedFailure: string
  git: { branch: string; head: string; status: string; refs: string }
}

export function plannerResultCacheKey(identity: PlannerCacheIdentity): string {
  const promptHash = createHash('sha256').update(identity.promptMarkdown).digest('hex')
  return createHash('sha256').update(JSON.stringify({
    projectName: identity.projectName,
    promptHash,
    normalizedFailure: identity.normalizedFailure,
    git: identity.git,
  })).digest('hex')
}

export function plannerResultCachePath(cacheRoot: string, key: string): string {
  return join(cacheRoot, `${key}.json`)
}

export function writePlannerResultCache(path: string, result: PlannerResult): void {
  mkdirSync(dirname(path), { recursive: true })
  const temporary = `${path}.${process.pid}.tmp`
  writeFileSync(temporary, `${JSON.stringify(result, null, 2)}\n`, 'utf8')
  renameSync(temporary, path)
}
