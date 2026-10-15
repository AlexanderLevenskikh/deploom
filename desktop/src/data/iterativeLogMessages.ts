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

export function iterativeLogEntries(log: string): Array<{ text: string; tone: 'error' | 'info' }> {
  return log.split(/\r?\n/).flatMap(line => {
    let failed = /ITERATIVE_MIGRATION_FAILURE_V1|\bFORBIDDEN_MUTATION\b|\bAGENT_REPAIR_BUDGET_EXHAUSTED\b|^(?:Agent failed:|Autopilot stopped \(error\)|RuntimeError:|Error:|npm ERR!|npm error|error[: ]|fatal[: ]|Traceback \(|\[error\])/i.test(line.trim())
    try {
      const event = JSON.parse(line)
      failed ||= event.type === 'error' || Boolean(event.error) || event.part?.type === 'error'
    } catch { /* plain output */ }
    return iterativeLogMessages(line).map(text => ({ text, tone: failed ? 'error' as const : 'info' as const }))
  })
}
