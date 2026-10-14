import { copyFile, writeFile } from 'node:fs/promises'
import { createReadStream, createWriteStream } from 'node:fs'
import { pipeline } from 'node:stream/promises'
import { closeSync, fstatSync, openSync, readSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { basename } from 'node:path'
import { listJournalSources, selectJournalSource, type JournalScope, type JournalSource } from './project-journal-catalog.js'
import { readJournalPage, type JournalCursor, type JournalPage } from './project-journal.js'

type ScopeInput = { workspaceId?: string; projectName: string }
type Request = ScopeInput & { sourceId: string; cursor?: JournalCursor }
type Ipc = { handle: (channel: string, handler: (event: any, input: any) => unknown) => void }
type Dependencies = { scope: (input: ScopeInput) => JournalScope; savePath: (event: unknown, filename: string) => Promise<string | undefined> }

/** Old files have no reliable stream/attempt metadata. Preserve their exact
 * bytes in a separate selectable source instead of merging them with live data. */
export function readLegacyJournal(source: JournalSource, scope: JournalScope, cursor?: JournalCursor): JournalPage {
  const fd = openSync(source.artifactPath, 'r')
  try {
    const stat = fstatSync(fd)
    const epoch = createHash('sha256').update(JSON.stringify([stat.dev, stat.ino, stat.birthtimeMs])).digest('hex')
    const valid = cursor?.epoch === epoch && Number.isSafeInteger(cursor.offset) && cursor.offset >= 0 && cursor.offset <= stat.size
    const start = valid ? cursor!.offset : Math.max(0, stat.size - 256 * 1024)
    const data = Buffer.alloc(Math.min(256 * 1024, stat.size - start))
    const length = readSync(fd, data, 0, data.length, start)
    let pos = 0
    if (!valid && start) { const end = data.indexOf(10); pos = end < 0 ? length : end + 1 }
    const events: JournalPage['events'] = []
    let consumed = start + pos
    while (pos < length && events.length < 800) {
      const newline = data.indexOf(10, pos)
      if (newline < 0) break
      const offset = start + pos
      const text = data.subarray(pos, newline).toString('utf8').replace(/\r$/, '')
      consumed = start + newline + 1
      const header = /^\[([^\]]+)\] \[(stdout|stderr|system)\] ?/.exec(text)
      events.push({ workspaceId: scope.workspaceId, projectName: scope.projectName, runId: source.runId, source: 'legacy', stream: header ? header[2] as 'stdout' | 'stderr' | 'system' : 'system', text: header ? text.slice(header[0].length) : text, at: header ? Date.parse(header[1]) || 0 : 0, id: epoch + ':' + offset, offset, endOffset: consumed })
      pos = newline + 1
    }
    return { events, cursor: { epoch, offset: consumed }, reset: !valid, truncated: !valid && start > 0, bytes: stat.size }
  } finally { closeSync(fd) }
}

export function registerProjectJournalHandlers(ipc: Ipc, dependencies: Dependencies): void {
  ipc.handle('flow:journal:list', (_event, input: ScopeInput) => {
    const scope = dependencies.scope(input)
    return { sources: listJournalSources(scope).map(({ directory: _directory, ...source }) => source) }
  })
  ipc.handle('flow:journal:read', (_event, input: Request) => {
    const scope = dependencies.scope(input)
    const source = selectJournalSource(scope, input.sourceId)
    const page = source.format === 'events' ? readJournalPage(source.directory, input.cursor, source) : readLegacyJournal(source, scope, input.cursor)
    // Durable files must also carry the owning context. A foreign or corrupt
    // event never becomes a selected-project event merely by being in its folder.
    return { ...page, sourceId: source.id, events: page.events.filter(event => event.workspaceId === scope.workspaceId && event.projectName === scope.projectName && (source.startOffset === undefined || event.attemptId === source.attemptId)) }
  })
  ipc.handle('flow:journal:export', async (event, input: Request) => {
    const scope = dependencies.scope(input)
    const source = selectJournalSource(scope, input.sourceId)
    const path = await dependencies.savePath(event, basename(source.artifactPath))
    if (!path) return { canceled: true }
    // Revalidate after a dialog: project removal or a changed artifact cannot
    // turn an old selector into permission to read an arbitrary path.
    const latest = selectJournalSource(dependencies.scope(input), input.sourceId)
    if (latest.startOffset !== undefined && latest.endOffset !== undefined) {
      if (latest.endOffset === latest.startOffset) await writeFile(path, '')
      else await pipeline(createReadStream(latest.artifactPath, { start: latest.startOffset, end: latest.endOffset - 1 }), createWriteStream(path))
    } else await copyFile(latest.artifactPath, path)
    return { canceled: false, path }
  })
}
