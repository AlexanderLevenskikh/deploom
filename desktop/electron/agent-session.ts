import { createHash } from 'node:crypto'

export type SessionProvider = 'codex' | 'opencode' | 'claude'

export type PersistedAgentSession = {
  provider: SessionProvider
  id: string
  interrupted: boolean
  updatedAt: string
  scopeFingerprint?: string
}

export function extractAgentSessionId(line: string, provider: SessionProvider): string | undefined {
  let event: Record<string, unknown>
  try {
    event = JSON.parse(line) as Record<string, unknown>
  } catch {
    return undefined
  }
  const part = event.part && typeof event.part === 'object' ? event.part as Record<string, unknown> : undefined
  const candidates = provider === 'opencode'
    ? [event.sessionID, part?.sessionID, event.session_id]
    : provider === 'claude'
      ? [event.session_id, event.sessionID]
      : [event.thread_id, event.session_id, event.sessionID, part?.thread_id]
  return candidates.find((value): value is string => typeof value === 'string' && value.trim().length > 0)?.trim()
}

// A persisted CLI session is safe to resume only when it belongs to the exact
// immutable migration scope the current run is about to execute. Branch name
// alone is insufficient: a fresh baseline / edited exclusions / regenerated
// targets can reuse the same branch names while making the old conversation
// context actively stale (and very expensive to carry forward).
export function agentScopeFingerprint(input: {
  provider: SessionProvider
  project: string
  branch: string
  scopeHash: string
  model?: string
  promptVersion?: string
}): string {
  return createHash('sha256')
    .update(JSON.stringify({
      provider: input.provider,
      project: input.project,
      branch: input.branch,
      scopeHash: input.scopeHash,
      model: input.model?.trim() || '',
      promptVersion: input.promptVersion?.trim() || '',
    }))
    .digest('hex')
}

// Completion is a property of the immutable semantic batch, not of the CLI
// provider/model/worktree that happened to execute it. Keep a separate stable
// key so an app restart that recreates a temp worktree does not make already
// verified source/config work run again merely because its path changed.
export function agentBatchCompletionFingerprint(input: {
  project: string
  branch: string
  scopeHash: string
  promptVersion?: string
}): string {
  return createHash('sha256')
    .update(JSON.stringify({
      project: input.project,
      branch: input.branch,
      scopeHash: input.scopeHash,
      promptVersion: input.promptVersion?.trim() || '',
    }))
    .digest('hex')
}

export function resumableAgentSessionId(
  saved: PersistedAgentSession | undefined,
  provider: SessionProvider,
  scopeFingerprint: string,
): string | undefined {
  if (!saved?.interrupted) return undefined
  if (saved.provider !== provider) return undefined
  // Sessions written before the fingerprint existed are intentionally not
  // resumed. Paying the small cost of a fresh prompt once is much safer than
  // importing an unbounded, possibly pre-baseline conversation.
  if (!saved.scopeFingerprint || saved.scopeFingerprint !== scopeFingerprint) return undefined
  return saved.id
}
