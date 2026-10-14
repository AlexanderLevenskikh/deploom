import { existsSync, readdirSync, readFileSync, realpathSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { isAbsolute, join, relative, resolve } from 'node:path'
import { ATTEMPT_HISTORY_FILENAME } from './project-journal-history.js'
import { flushJournal, journalPresent, JOURNAL_FILENAME } from './project-journal.js'

import type { JournalSource, JournalScope } from './project-journal-types.js'
export type { JournalSource, JournalScope } from './project-journal-types.js'
function json(path: string): Record<string, any> | undefined {
  try { if (statSync(path).size > 1024 * 1024) return; return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, '')) } catch { return }
}
function owned(root: string, candidate: string): boolean {
  try {
    const path = relative(realpathSync(root), realpathSync(candidate))
    return path !== '..' && !path.startsWith('../') && !path.startsWith('..\\') && !isAbsolute(path)
  } catch { return false }
}
function children(root: string): string[] {
  try { return readdirSync(root, { withFileTypes: true }).filter(item => item.isDirectory() && !item.isSymbolicLink()).map(item => join(root, item.name)).filter(path => owned(root, path)) } catch { return [] }
}

/** Catalog IDs are server-generated. The renderer never supplies a filesystem
 * path to replay/export, and workspace events are never attributed to a project. */
export function listJournalSources(scope: JournalScope): JournalSource[] {
  const sources: JournalSource[] = []
  const add = (directory: string, kind: JournalSource['kind'], metadata: Record<string, any>, current: boolean) => {
    if (metadata.projectName !== scope.projectName || metadata.workspaceId && metadata.workspaceId !== scope.workspaceId) return
    flushJournal(directory)
    const run = json(join(directory, 'run.json'))
    const attempt = json(join(directory, 'attempt.json'))
    const runId = String(run?.runId || metadata.jobId || metadata.attemptId || '')
    if (!runId) return
    const raw = kind === 'flow' ? join(directory, 'activity.log') : existsSync(join(directory, 'run.log')) ? join(directory, 'run.log') : join(directory, 'attempt.log')
    const common = { kind, runId, attemptId: typeof attempt?.attemptId === 'string' ? attempt.attemptId : undefined, action: String(metadata.action || attempt?.stage || 'migration'), startedAt: typeof metadata.startedAt === 'number' ? metadata.startedAt : Date.parse(metadata.startedAt ?? '') || undefined, status: typeof metadata.status === 'string' ? metadata.status : undefined, current, artifactPath: raw, directory }
    for (const format of ['events', 'legacy'] as const) {
      const artifactPath = format === 'events' ? join(directory, JOURNAL_FILENAME) : raw
      if (!owned(directory, artifactPath) || (format === 'events' ? !journalPresent(directory) : !existsSync(raw))) continue
      const id = createHash('sha256').update(JSON.stringify([scope.workspaceId, scope.projectName, resolve(directory), format])).digest('hex')
      sources.push({ ...common, artifactPath, id, format })
      if (format === 'events') {
        try {
          const indexPath = join(directory, ATTEMPT_HISTORY_FILENAME)
          if (!owned(directory, indexPath) || statSync(indexPath).size > 1024 * 1024) continue
          const history = readFileSync(indexPath, 'utf8').split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line))
          const size = statSync(artifactPath).size
          for (let position = 0; position < history.length; position++) {
            const entry = history[position]
            const endOffset = history[position + 1]?.offset ?? size
            if (entry.projectName !== scope.projectName || entry.workspaceId !== scope.workspaceId || typeof entry.attemptId !== 'string' || !Number.isSafeInteger(entry.offset) || !Number.isSafeInteger(endOffset) || entry.offset < 0 || endOffset < entry.offset || endOffset > size) continue
            sources.push({ ...common, artifactPath, id: createHash('sha256').update(id + ':' + entry.attemptId).digest('hex'), format, runId: typeof entry.runId === 'string' ? entry.runId : common.runId, attemptId: entry.attemptId, action: String(entry.stage || 'migration'), startedAt: entry.startedAt, current: current && attempt?.attemptId === entry.attemptId, startOffset: entry.offset, endOffset })
          }
        } catch { /* old/unavailable indexes leave full run sources available */ }
      }
    }
  }
  const dir = scope.iterativeDirectory
  const attempt = json(join(dir, 'attempt.json'))
  const run = json(join(dir, 'run.json'))
  if (attempt || run) add(dir, 'iterative', { ...run, ...attempt, projectName: attempt?.projectName ?? run?.projectName }, true)
  const archives = ['repair-archive', 'run-archive'].flatMap(name => children(join(dir, name)))
  for (const archived of [...archives, ...children(join(dir, 'audit-logs')), ...archives.flatMap(path => children(join(path, 'audit-logs')))]) {
    const old = json(join(archived, 'attempt.json')) ?? json(join(archived, 'run.json'))
    if (old) add(archived, 'iterative', old, false)
  }
  for (const child of children(join(scope.workspacePath, '.dependency-roadmap', 'artifacts', 'runs'))) {
    const metadata = json(join(child, 'run.json'))
    if (metadata) add(child, 'flow', metadata, false)
  }
  return sources.sort((a, b) => Number(b.current) - Number(a.current) || (b.startedAt ?? 0) - (a.startedAt ?? 0) || Number(a.format === 'legacy') - Number(b.format === 'legacy'))
}

export function selectJournalSource(scope: JournalScope, id: unknown): JournalSource {
  if (typeof id !== 'string') throw new Error('JOURNAL_SOURCE_REQUIRED')
  const source = listJournalSources(scope).find(item => item.id === id)
  if (!source) throw new Error('JOURNAL_SOURCE_NOT_OWNED')
  return source
}
