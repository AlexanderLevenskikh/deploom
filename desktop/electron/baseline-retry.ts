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
