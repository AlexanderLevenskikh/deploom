import type { JournalCursor, JournalEvent, JournalPage } from './project-journal-types.js'
export type JournalViewState = { workspaceId: string; projectName: string; sourceId: string; generation: number; lastRequest: number; events: JournalEvent[]; cursor?: JournalCursor; truncated: boolean; bytes: number; cleared: boolean }
export function emptyJournalView(workspaceId: string, projectName: string, sourceId: string, generation: number): JournalViewState {
  return { workspaceId, projectName, sourceId, generation, lastRequest: 0, events: [], truncated: false, bytes: 0, cleared: false }
}
export function applyJournalPage(state: JournalViewState, request: { generation: number; sourceId: string; sequence: number }, page: JournalPage & { sourceId: string }): JournalViewState {
  if (request.generation !== state.generation || request.sourceId !== state.sourceId || page.sourceId !== state.sourceId || request.sequence < state.lastRequest) return state
  if (!page.reset && state.cursor && (state.cursor.epoch !== page.cursor.epoch || state.cursor.offset > page.cursor.offset)) return state
  const events = page.events.filter(event => event.workspaceId === state.workspaceId && event.projectName === state.projectName)
  const previous = page.reset ? [] : state.events
  const ids = new Set(previous.map(event => event.id))
  const appended = events.filter(event => { if (ids.has(event.id)) return false; ids.add(event.id); return true })
  const merged = [...previous, ...appended]
  return { ...state, lastRequest: request.sequence, cursor: page.cursor, events: merged.slice(-2000), truncated: state.truncated || page.truncated || merged.length > 2000, bytes: page.bytes, cleared: state.cleared && appended.length === 0 }
}
export function clearJournalView(state: JournalViewState): JournalViewState {
  return { ...state, events: [], cleared: true }
}
