type Progress = { processed: number; total: number }
export function discoveryProgress(current: Progress | undefined, event: { event?: string; processed?: unknown; total?: unknown; managedDependencies?: unknown }): Progress | undefined {
  if (event.event === 'begin.capture') {
    if (current?.total) return current
    const total = event.managedDependencies
    return typeof total === 'number' && Number.isInteger(total) && total > 0 ? { processed: 0, total } : undefined
  }
  if (event.event === 'begin.discovery') return current?.total ? { processed: current.total, total: current.total } : undefined
  if (event.event !== 'begin.discovery-progress') return undefined
  const { processed, total } = event
  if (typeof processed !== 'number' || typeof total !== 'number' || !Number.isInteger(processed) || !Number.isInteger(total) || total <= 0 || processed < 0 || processed > total) return undefined
  if (current?.total && current.total !== total) return undefined
  return { processed: Math.max(processed, current?.processed || 0), total }
}
