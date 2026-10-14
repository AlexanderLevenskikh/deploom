import { appendFileSync, closeSync, existsSync, fstatSync, mkdirSync, openSync, readSync, statSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { join } from 'node:path'

/** Diagnostic data only. Neither replay nor a journal message grants proof authority. */
import type { JournalRecord, JournalEvent, JournalCursor, JournalPage } from './project-journal-types.js'
export type { JournalContext, JournalRecord, JournalEvent, JournalCursor, JournalPage } from './project-journal-types.js'
export const JOURNAL_FILENAME = 'journal.ndjson'
const PAGE_BYTES = 256 * 1024
const MAX_EVENTS = 800
const FRAGMENT_CHARS = 4096

/** Keep each persisted record smaller than a bounded reader page, even for a
 * single enormous tool response. The original chunks remain lossless in order. */
function encodeJournalRecord(record: JournalRecord): string {
  const parts: string[] = []
  const group = record.text.length > FRAGMENT_CHARS ? randomUUID() : undefined
  let offset = 0
  do {
    let end = Math.min(record.text.length, offset + FRAGMENT_CHARS)
    // Do not split a UTF-16 surrogate pair before encoding the JSON record.
    if (end < record.text.length && /[\uD800-\uDBFF]/.test(record.text[end - 1])) end--
    parts.push(JSON.stringify({ ...record, text: record.text.slice(offset, end), ...(group ? { group, fragment: parts.length } : {}) }) + '\n')
    offset = end
  } while (offset < record.text.length)
  return parts.join('')
}

export function appendJournalRecord(directory: string, record: JournalRecord): void {
  mkdirSync(directory, { recursive: true })
  appendFileSync(join(directory, JOURNAL_FILENAME), encodeJournalRecord(record), 'utf8')
}

const pending = new Map<string, { chunks: string[]; chars: number; timer?: ReturnType<typeof setTimeout> }>()

/** Match the existing durable-log batching, with a fixed per-directory cap. */
export function queueJournalRecord(directory: string, record: JournalRecord): void {
  let entry = pending.get(directory)
  if (!entry) { entry = { chunks: [], chars: 0 }; pending.set(directory, entry) }
  const chunk = encodeJournalRecord(record)
  entry.chunks.push(chunk); entry.chars += chunk.length
  if (entry.chars >= 64 * 1024) flushJournal(directory)
  else if (!entry.timer) { entry.timer = setTimeout(() => flushJournal(directory), 250); entry.timer.unref() }
}

export function flushJournal(directory?: string): void {
  for (const [path, entry] of pending) {
    if (directory !== undefined && directory !== path) continue
    if (entry.timer) clearTimeout(entry.timer)
    pending.delete(path)
    try {
      mkdirSync(path, { recursive: true })
      appendFileSync(join(path, JOURNAL_FILENAME), entry.chunks.join(''), 'utf8')
    } catch { /* The original durable raw log remains available if structured diagnostics fail. */ }
  }
}

/** Byte offsets identify events, so identical real messages remain distinct.
 * Live IPC is only an invalidation signal; replay is the canonical data stream.
 * The epoch also prevents a late page from a replaced file being appended. */
export function readJournalPage(directory: string, cursor?: JournalCursor, bounds?: { startOffset?: number; endOffset?: number }): JournalPage {
  flushJournal(directory)
  const path = join(directory, JOURNAL_FILENAME)
  if (!existsSync(path)) return { events: [], cursor: { epoch: 'missing', offset: 0 }, reset: true, truncated: false, bytes: 0 }
  const fd = openSync(path, 'r')
  try {
    const stat = fstatSync(fd)
    const epoch = createHash('sha256').update(JSON.stringify([stat.dev, stat.ino, stat.birthtimeMs])).digest('hex')
    const lower = Math.max(0, Math.min(stat.size, bounds?.startOffset ?? 0))
    const upper = Math.max(lower, Math.min(stat.size, bounds?.endOffset ?? stat.size))
    const valid = cursor?.epoch === epoch && Number.isSafeInteger(cursor.offset) && cursor.offset >= lower && cursor.offset <= upper
    const start = valid ? cursor!.offset : Math.max(lower, upper - PAGE_BYTES)
    const buffer = Buffer.alloc(Math.min(PAGE_BYTES, upper - start))
    const length = readSync(fd, buffer, 0, buffer.length, start)
    let position = 0
    if (!valid && start > lower) { const newline = buffer.indexOf(10); position = newline < 0 ? length : newline + 1 }
    const events: JournalEvent[] = []
    let consumed = start + position
    while (position < length && events.length < MAX_EVENTS) {
      const newline = buffer.indexOf(10, position)
      if (newline < 0) break // Incomplete append is retried from the same byte.
      const offset = start + position
      consumed = start + newline + 1
      try {
        const value = JSON.parse(buffer.subarray(position, newline).toString('utf8')) as JournalRecord
        if (typeof value.text === 'string' && typeof value.workspaceId === 'string' && typeof value.projectName === 'string' && typeof value.runId === 'string' && ['stdout', 'stderr', 'system'].includes(value.stream)) {
          events.push({ ...value, id: epoch + ':' + offset, offset, endOffset: consumed })
        }
      } catch { /* Invalid diagnostic records have no execution authority. */ }
      position = newline + 1
    }
    return { events, cursor: { epoch, offset: consumed }, reset: !valid, truncated: start > lower && !valid, bytes: upper - lower }
  } finally { closeSync(fd) }
}

export function journalPresent(directory: string): boolean {
  try { return statSync(join(directory, JOURNAL_FILENAME)).isFile() } catch { return false }
}
