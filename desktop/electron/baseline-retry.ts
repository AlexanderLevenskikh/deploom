/** Programming defects stop immediately; package/transport failures retain bounded retries. */
export function isDeterministicToolFailure(result: { code: number; stderr: string; stdout: string }): boolean {
  if (result.code === 0) return false
  const text = `${result.stderr}\n${result.stdout}`
  for (const line of text.split(/\r?\n/)) {
    const start = line.indexOf('{')
    if (start < 0) continue
    try {
      const record: unknown = JSON.parse(line.slice(start))
      if (record && typeof record === 'object' && 'schemaVersion' in record &&
          record.schemaVersion === 'DEPLOOM_FAILURE_V2' && 'category' in record &&
          record.category === 'TOOL_INTERNAL_ERROR') return true
    } catch { /* Ordinary command output is not a failure envelope. */ }
  }
  // The tool also prints this summary if structured output was truncated.
  if (/^Baseline stopped safely: TOOL_INTERNAL_ERROR\s*$/m.test(text)) return true
  return /Traceback \(most recent call last\):/i.test(text) &&
    /(?:TypeError|AttributeError|AssertionError|NameError|UnboundLocalError|SyntaxError|IndentationError|ModuleNotFoundError|ImportError):[^\r\n]*\s*$/i.test(text)
}

/**
 * Deterministic source checkout preflight outcomes (SOURCE_* guard rejects).
 * They describe the project's Git state before any registry analysis, solving
 * or physical verification started, so retrying them cannot make them
 * transiently disappear and only repeats the guard work.
 */
const SOURCE_PREFLIGHT_PATTERN = /SOURCE_CHECKOUT_[A-Z_]+|SOURCE_BRANCH_[A-Z_]+|SOURCE_REMOTE_NOT_FOUND|SOURCE_NOT_GIT_REPOSITORY|SOURCE_PROJECT_NOT_FOUND|SOURCE_FETCH_FAILED|SOURCE_BRANCH_DIVERGED|GIT_NOT_FOUND/

export function isDeterministicSourcePreflightFailure(result: { code: number; stderr: string; stdout: string }): boolean {
  if (result.code === 0) return false
  return SOURCE_PREFLIGHT_PATTERN.test(`${result.stderr}\n${result.stdout}`)
}

/**
 * Parses the dirty tracked files out of a SOURCE_CHECKOUT_DIRTY diagnostic
 * line: `[error] SOURCE_CHECKOUT_DIRTY: <project>: commit/stash/remove changes
 * before generation: M package.json; M yarn.lock` -> ["package.json", ...].
 * Returns undefined when the line/format is absent or unexpected.
 */
export function formatSourceCheckoutDirtyFailure(stderr: string): { files: string[] } | undefined {
  const line = stderr.split(/\r?\n/).find((candidate) => candidate.includes('SOURCE_CHECKOUT_DIRTY'))
  if (!line) return undefined
  const marker = 'commit/stash/remove changes before generation: '
  const markerIndex = line.indexOf(marker)
  if (markerIndex < 0) return undefined
  const tail = line.slice(markerIndex + marker.length).trim()
  if (!tail) return undefined
  const files = tail
    .split(';')
    .map((entry) => {
      const trimmed = entry.trim()
      const status = /^([A-Z?]{1,2})\s+(.*)$/.exec(trimmed)
      return status ? status[2].trim() : trimmed
    })
    .filter(Boolean)
  return { files }
}
