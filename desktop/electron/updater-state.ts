export type UpdateStatus = { state: 'idle' | 'checking' | 'available' | 'downloading' | 'current' | 'ready' | 'error'; version?: string; percent?: number; message?: string; authRequired?: boolean }

// Feed checks report discovery again, even for an active or cached download.
// Those events must not rewind the package's installation state.
export function nextUpdaterStatus(current: UpdateStatus, incoming: UpdateStatus, autoDownload: boolean): UpdateStatus {
  const packageActive = current.state === 'downloading' || current.state === 'ready'
  if (packageActive && (incoming.state === 'checking' || incoming.state === 'current')) return current
  if (packageActive && incoming.state === 'available' && incoming.version === current.version) return current
  if (current.state === 'ready' && incoming.state === 'downloading' && incoming.version === current.version) return current
  if (incoming.state === 'available' && autoDownload) return { state: 'downloading', version: incoming.version }
  return incoming
}
