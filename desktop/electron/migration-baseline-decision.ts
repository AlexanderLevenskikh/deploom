export const BASELINE_DECISION_MARKER = 'DEPLOOM_BASELINE_DECISION_V1 '

function validDecisionEnvelope(candidate: string): boolean {
  if (!candidate.startsWith(BASELINE_DECISION_MARKER)) return false
  const payload = candidate.slice(BASELINE_DECISION_MARKER.length).trim()
  if (!payload) return false
  try {
    const parsed = JSON.parse(payload)
    return Boolean(parsed && typeof parsed === 'object' && !Array.isArray(parsed))
  } catch {
    return false
  }
}

/**
 * Extract the last valid Baseline human-decision envelope from command output.
 *
 * The envelope is a control signal, not proof. Keeping it byte-for-byte from
 * the marker onward lets the existing renderer parse the same schema whether
 * the Baseline was launched directly or nested inside deterministic migration
 * replanning.
 */
export function extractBaselineDecisionEnvelope(stdout = '', stderr = ''): string | undefined {
  const lines = `${stdout}\n${stderr}`.split(/\r?\n/)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index]
    const markerIndex = line.indexOf(BASELINE_DECISION_MARKER)
    if (markerIndex < 0) continue
    const candidate = line.slice(markerIndex).trim()
    if (validDecisionEnvelope(candidate)) return candidate
  }
  return undefined
}

export function containsBaselineDecisionEnvelope(message: string): boolean {
  return extractBaselineDecisionEnvelope(message, '') !== undefined
}
