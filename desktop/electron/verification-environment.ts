import { createHash } from 'node:crypto'
import { existsSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { verificationCommandKey } from './migration-verification.js'

// These directories are generated package-tool caches, never source-of-truth.
// Real projects have shown both Vite and Vitest leaving stale state here that
// makes an otherwise unchanged build/test fail on the next verification run.
// Deterministic verification owns environment normalization; an LLM repair
// must never be spent merely deleting generated caches.
const EPHEMERAL_VERIFICATION_CACHE_PATHS = [
  'node_modules/.vite',
  'node_modules/.vitest',
  'src/node_modules/.vite',
  'src/node_modules/.vitest',
] as const

export function cleanEphemeralVerificationCaches(projectPath: string): string[] {
  const removed: string[] = []
  for (const relative of EPHEMERAL_VERIFICATION_CACHE_PATHS) {
    const target = join(projectPath, ...relative.split('/'))
    if (!existsSync(target)) continue
    rmSync(target, { recursive: true, force: true })
    removed.push(relative)
  }
  return removed
}

export function baselineVerificationCacheKey(projectName: string, baseBranch: string, command: string): string {
  return `${projectName}\u0000${baseBranch}\u0000${verificationCommandKey(command)}`
}

export type IntegrationVerificationReceiptInput = {
  projectName: string
  projectPath: string
  mergedBranch: string
  sourceBranch: string
  head: string
  commands: readonly string[]
  planFingerprint: string
  baselineEvidenceFingerprint: string
  environment: NodeJS.ProcessEnv
}

export function integrationVerificationReceiptKey(input: IntegrationVerificationReceiptInput): string {
  const environment = Object.entries(input.environment)
    .filter((entry): entry is [string, string] => typeof entry[1] === 'string')
    .sort(([left], [right]) => left.localeCompare(right))
  const payload = JSON.stringify({
    schemaVersion: 1,
    projectName: input.projectName,
    projectPath: input.projectPath,
    mergedBranch: input.mergedBranch,
    sourceBranch: input.sourceBranch,
    head: input.head,
    commands: [...input.commands],
    planFingerprint: input.planFingerprint,
    baselineEvidenceFingerprint: input.baselineEvidenceFingerprint,
    environment,
  })
  return createHash('sha256').update(payload).digest('hex')
}
