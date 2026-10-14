import { appendFileSync, mkdirSync, statSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { flushJournal, JOURNAL_FILENAME } from './project-journal.js'
export const ATTEMPT_HISTORY_FILENAME = 'journal-attempts.ndjson'
/** Record only attempt boundaries; no duplicate copy of child output. */
export function recordJournalAttempt(directory: string, attempt: { attemptId: string; workspaceId?: string; projectName: string; stage: string; startedAt: number }): void {
  flushJournal(directory)
  let offset = 0
  try { offset = statSync(join(directory, JOURNAL_FILENAME)).size } catch { /* first event */ }
  let runId = attempt.attemptId
  try { runId = JSON.parse(readFileSync(join(directory, 'run.json'), 'utf8')).runId || runId } catch { /* standalone check */ }
  mkdirSync(directory, {recursive:true})
  appendFileSync(join(directory, ATTEMPT_HISTORY_FILENAME), JSON.stringify({ ...attempt, runId, offset }) + '\n', 'utf8')
}
