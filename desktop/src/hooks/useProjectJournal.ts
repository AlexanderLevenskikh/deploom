import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { applyJournalPage, clearJournalView, emptyJournalView } from '../../electron/project-journal-view'
import type { JournalViewState } from '../../electron/project-journal-view'
import type { DependencyFlowApi, JobOutput, ProjectJournalSource } from '../types'

type Snapshot = { scope: string; sources: ProjectJournalSource[]; sourceId?: string; view?: JournalViewState; loading: boolean; error?: string; manual: boolean }
const empty: Snapshot = { scope: '', sources: [], loading: false, manual: false }

/** Mounted by the application owner, above all project tabs. IPC events only
 * wake a scoped disk read: they are never concatenated with durable replay. */
export function useProjectJournal(api: DependencyFlowApi | undefined, workspaceId?: string, projectName?: string, preferredRunId?: string) {
  const scopeKey = JSON.stringify([workspaceId, projectName])
  const [snapshot, setSnapshot] = useState<Snapshot>(empty)
  const generation = useRef(0)
  const cache = useRef(new Map<string, Snapshot>())
  const preferred = useRef(preferredRunId)
  preferred.current = preferredRunId
  const commands = useRef<{ refresh: () => Promise<void>; select: (id: string) => void; clear: () => void; export: () => Promise<unknown> } | undefined>(undefined)

  useEffect(() => {
    const ownGeneration = ++generation.current
    if (!api?.journalSources || !workspaceId || !projectName) { setSnapshot({ ...empty, scope: scopeKey }); commands.current = undefined; return }
    const scope = { workspaceId, projectName }
    let state: Snapshot = { ...(cache.current.get(scopeKey) ?? empty), scope: scopeKey, loading: true }
    if (state.view) state = { ...state, view: { ...state.view, generation: ownGeneration } }
    let live = true
    let reading = false
    let listing = false
    let queuedRead = false
    let sequence = 0
    let selectionEpoch = 0
    let failures = 0
    let nextRead = 0
    let ticks = 0
    const current = () => live && generation.current === ownGeneration
    const publish = () => {
      if (!current()) return
      cache.current.set(scopeKey, state)
      if (cache.current.size > 20) cache.current.delete(cache.current.keys().next().value!)
      setSnapshot(state)
    }
    const read = async () => {
      if (!current() || !state.sourceId) return
      if (reading) { queuedRead = true; return }
      reading = true
      const sourceId = state.sourceId
      const requestedSelection = selectionEpoch
      const request = { generation: ownGeneration, sourceId, sequence: ++sequence }
      const before = state.view ?? emptyJournalView(workspaceId, projectName, sourceId, ownGeneration)
      try {
        const page = await api.journalRead({ ...scope, sourceId, cursor: before.cursor })
        if (!current() || state.sourceId !== sourceId || requestedSelection !== selectionEpoch) return
        const view = applyJournalPage(state.view ?? before, request, page)
        const changed = page.events.length > 0 || page.reset || state.loading || state.error || view.bytes !== state.view?.bytes
        state = { ...state, view, loading: false, error: undefined }
        failures = 0; nextRead = 0
        if (changed) publish()
      } catch (error) {
        if (!current() || state.sourceId !== sourceId || requestedSelection !== selectionEpoch) return
        state = { ...state, loading: false, error: error instanceof Error ? error.message : String(error) }
        nextRead = Date.now() + Math.min(30_000, 1500 * 2 ** Math.min(++failures, 5))
        publish()
      } finally {
        reading = false
        if (queuedRead && current()) { queuedRead = false; void read() }
      }
    }
    const list = async () => {
      if (!current() || listing) return
      listing = true
      try {
        const { sources } = await api.journalSources(scope)
        if (!current()) return
        const selected = state.manual && sources.find(item => item.id === state.sourceId)
        const desired = selected || sources.find(item => item.runId === preferred.current && item.format === 'events') || sources.find(item => item.current && item.format === 'events') || sources[0]
        const changed = desired?.id !== state.sourceId
        if (changed) selectionEpoch++
        state = { ...state, sources, sourceId: desired?.id, loading: Boolean(desired) && state.loading, ...(changed ? { view: desired ? emptyJournalView(workspaceId, projectName, desired.id, ownGeneration) : undefined } : {}) }
        publish()
        if (desired) await read()
      } catch (error) {
        if (current()) { state = { ...state, loading: false, error: error instanceof Error ? error.message : String(error) }; publish() }
      } finally { listing = false }
    }
    commands.current = {
      refresh: list,
      select: id => {
        if (!state.sources.some(item => item.id === id)) return
        selectionEpoch++
        state = { ...state, sourceId: id, manual: true, loading: true, view: emptyJournalView(workspaceId, projectName, id, ownGeneration) }
        publish(); void read()
      },
      clear: () => { if (state.view) { state = { ...state, view: clearJournalView(state.view) }; publish() } },
      export: async () => state.sourceId ? api.journalExport({ ...scope, sourceId: state.sourceId }) : undefined,
    }
    publish(); void list()
    const timer = window.setInterval(() => { if (++ticks % 7 === 0) void list(); else if (Date.now() >= nextRead) void read() }, 1500)
    const wake = (event: { workspaceId?: string; projectName?: string }) => { if (event.workspaceId === workspaceId && event.projectName === projectName && Date.now() >= nextRead) void read() }
    const stopOutput = api.onJobOutput(wake)
    const stopAttempt = api.onIterativeAttempt(wake)
    return () => { live = false; window.clearInterval(timer); stopOutput(); stopAttempt(); commands.current = undefined }
  }, [api, workspaceId, projectName, scopeKey])

  useEffect(() => { void commands.current?.refresh() }, [preferredRunId])
  const visible = snapshot.scope === scopeKey ? snapshot : { ...empty, scope: scopeKey, loading: Boolean(projectName) }
  const logs = useMemo<JobOutput[]>(() => (visible.view?.events ?? []).map(event => ({ jobId: event.runId, workspaceId: event.workspaceId, projectName: event.projectName, receivedAt: event.at || undefined, stream: event.stream, line: event.text, source: { kind: event.agentGroup ? 'group' : 'planner', id: event.agentGroup || event.source, label: event.sourceLabel || event.source } })), [visible.view?.events])
  const selectSource = useCallback((id: string) => commands.current?.select(id), [])
  const clear = useCallback(() => commands.current?.clear(), [])
  const refresh = useCallback(() => commands.current?.refresh(), [])
  const exportLog = useCallback(() => commands.current?.export(), [])
  return { ...visible, logs, selectSource, clear, refresh, exportLog }
}
export type ProjectJournalController = ReturnType<typeof useProjectJournal>
