/** Coalesce slow status IPC reads; heartbeats must not start overlapping Python readers. */
export function createStatusRequestGate<T>() {
  const pending = new Map<string, Promise<T>>()
  return (scope: string, read: () => Promise<T>): Promise<T> => {
    const existing = pending.get(scope)
    if (existing) return existing
    const request = Promise.resolve().then(read).finally(() => {
      if (pending.get(scope) === request) pending.delete(scope)
    })
    pending.set(scope, request)
    return request
  }
}
