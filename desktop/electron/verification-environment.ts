import { existsSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { verificationCommandKey } from './migration-verification.js'

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

export function liveBaselineObservationCacheKey(
  projectName: string,
  packageRelativePath: string,
  baseCommit: string,
  command: string,
): string {
  return `${projectName}\u0000${packageRelativePath}\u0000${baseCommit}\u0000${verificationCommandKey(command)}`
}
