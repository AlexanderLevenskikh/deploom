import { join } from 'node:path'

const DATABASE_LOCK_RE = /(?:database\s+is\s+locked|SQLITE_BUSY|SQLITE_LOCKED|database table is locked)/i

function safeRuntimeSegment(value: string): string {
  const compact = value.replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '')
  return compact || 'job'
}

export function openCodeDatabaseLocked(output: string): boolean {
  return DATABASE_LOCK_RE.test(output)
}

export function openCodeRuntimePaths(tempDir: string, ownerId: string, generation = 0): { root: string; directory: string; databasePath: string } {
  const root = join(tempDir, 'dependency-flow-opencode', safeRuntimeSegment(ownerId))
  const directory = join(root, `runtime-${Math.max(0, Math.trunc(generation))}`)
  return { root, directory, databasePath: join(directory, 'opencode.db') }
}

export function openCodeDatabaseEnv(base: NodeJS.ProcessEnv, databasePath: string): NodeJS.ProcessEnv {
  return { ...base, FORCE_COLOR: '0', OPENCODE_DB: databasePath }
}
