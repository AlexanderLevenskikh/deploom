/** Shared diagnostic DTOs; no Node/Electron runtime dependency in the renderer. */
export type JournalContext = { workspaceId: string; projectName: string; runId: string; attemptId?: string }
export type JournalRecord = JournalContext & { stream: 'stdout' | 'stderr' | 'system'; source: string; sourceLabel?: string; agentGroup?: string; text: string; at: number; group?: string; fragment?: number }
export type JournalEvent = JournalRecord & { id: string; offset: number; endOffset: number }
export type JournalCursor = { epoch: string; offset: number }
export type JournalPage = { events: JournalEvent[]; cursor: JournalCursor; reset: boolean; truncated: boolean; bytes: number }
export type JournalSource = { id: string; kind: 'iterative' | 'flow'; format: 'events' | 'legacy'; runId: string; attemptId?: string; action: string; startedAt?: number; status?: string; current: boolean; startOffset?: number; endOffset?: number; artifactPath: string; directory: string }
export type JournalScope = { workspaceId: string; projectName: string; workspacePath: string; iterativeDirectory: string }
