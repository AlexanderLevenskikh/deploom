/** One bounded journal read at a time; disposal drops delayed IPC replies. */
export function startAttemptJournalPolling<T>(read: () => Promise<T>, apply: (result: T) => void, intervalMs = 2000): () => void {
  let alive = true
  let pending = false
  const poll = async () => {
    if (!alive || pending) return
    pending = true
    try {
      const result = await read()
      if (alive) apply(result)
    } catch { /* Diagnostics are best-effort; retry the next poll. */ }
    finally { pending = false }
  }
  void poll()
  const timer = setInterval(() => { void poll() }, intervalMs)
  return () => { alive = false; clearInterval(timer) }
}
