export function iterativeLogMessages(log: string): string[] {
  return log.split(/\r?\n/).flatMap(line => {
    line = line.replace(/\u001b\[[0-9;]*m/g, '').trim()
    if (!line) return []
    if (line.startsWith('…')) return ['Earlier messages are available in the full log artifact.']
    const envelope = line.match(/^ITERATIVE_MIGRATION_(?:STATUS|FAILURE)_V1 (\{.*\})$/)
    try {
      const event = JSON.parse(envelope?.[1] ?? line)
      if (event.run && event.schemaVersion) return [`Run state: ${event.run.phase}; checkpoint ${event.run.activeCheckpointId}`]
      if (event.type === 'error' || event.error) return [String(event.error?.data?.message ?? event.error?.message ?? event.error?.name ?? 'Provider error')]
      if (event.part?.text) return [String(event.part.text)]
      if (event.message || event.summary) return [String(event.message ?? event.summary)]
      if (event.event) return [String(event.event)]
      if (event.type) return [String(event.type)]
    } catch { /* ordinary process output */ }
    return [line]
  })
}
