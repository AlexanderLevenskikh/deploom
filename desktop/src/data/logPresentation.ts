import type { JobOutput, JobOutputSource } from '../types'

export function mergeLogSources(logs: readonly JobOutput[], knownSources: readonly JobOutputSource[] = []): [string, JobOutputSource][] {
  const found = new Map<string, JobOutputSource>()
  for (const source of knownSources) found.set(`${source.kind}:${source.id}`, source)
  for (const entry of logs) if (entry.source) found.set(`${entry.source.kind}:${entry.source.id}`, entry.source)
  return [...found.entries()].sort((left, right) => {
    const leftLabel = left[1].kind === 'planner' ? 'Planner' : `Группа: ${left[1].label}`
    const rightLabel = right[1].kind === 'planner' ? 'Planner' : `Группа: ${right[1].label}`
    return leftLabel.localeCompare(rightLabel, 'ru')
  })
}

export type PresentedLog = {
  kind: 'system' | 'user' | 'message' | 'tool' | 'warning' | 'error' | 'raw'
  body: string
  title?: string
  detail?: string
  source?: JobOutput['source']
}

export type TokenUsage = {
  total: number
  input: number
  output: number
  reasoning: number
  cacheRead: number
  cacheWrite: number
  cost: number
}

const ANSI_PATTERN = new RegExp(`${String.fromCharCode(27)}\\[[0-?]*[ -/]*[@-~]`, 'g')
const TOOL_LABELS: Record<string, string> = {
  bash: 'Выполняю команду',
  edit: 'Изменяю файл',
  glob: 'Ищу файлы',
  grep: 'Ищу по коду',
  list: 'Просматриваю каталог',
  read: 'Читаю файл',
  task: 'Запускаю подзадачу',
  write: 'Записываю файл',
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.replace(ANSI_PATTERN, '').trim() : ''
}

function compact(value: string, limit = 220): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length > limit ? `${normalized.slice(0, limit - 1)}…` : normalized
}

function inputSummary(value: unknown): string {
  if (!value || typeof value !== 'object') return ''
  const input = value as Record<string, unknown>
  for (const key of ['description', 'command', 'pattern', 'path', 'filePath', 'query']) {
    const summary = clean(input[key])
    if (summary) return compact(summary)
  }
  return ''
}

function outputSummary(value: unknown): string {
  const output = compact(clean(value))
  if (!output) return ''
  if (/^no files found$/i.test(output)) return 'Ничего не найдено'
  return output
}

function parseAgentLine(line: string): PresentedLog | undefined {
  const trimmed = line.trim()
  if (!trimmed) return undefined
  let event: Record<string, unknown>
  try {
    event = JSON.parse(trimmed) as Record<string, unknown>
  } catch {
    if (trimmed.startsWith('{') && /"(?:sessionID|messageID|part|timestamp)"/.test(trimmed)) return undefined
    const severity = /^\[(info|warn|warning|error|done|hint)\]\s*/i.exec(trimmed)
    const body = clean(severity ? trimmed.slice(severity[0].length) : trimmed)
    if (!severity) return { kind: 'raw', body }
    const level = severity[1].toLowerCase()
    if (level === 'warn' || level === 'warning') return { kind: 'warning', title: 'Предупреждение', body }
    if (level === 'error') return { kind: 'error', title: 'Ошибка', body }
    if (level === 'done') return { kind: 'message', title: 'Готово', body }
    return { kind: 'raw', title: level === 'hint' ? 'Подсказка' : 'Ход выполнения', body }
  }

  const type = clean(event.type)
  const part = event.part && typeof event.part === 'object' ? event.part as Record<string, unknown> : undefined
  const partType = clean(part?.type)
  const message = event.message && typeof event.message === 'object' ? event.message as Record<string, unknown> : undefined
  const content = Array.isArray(message?.content) ? message.content as Array<Record<string, unknown>> : undefined
  if (content) {
    if (type === 'assistant') {
      const textBlock = content.find((block) => block.type === 'text')
      const body = clean(textBlock?.text)
      if (body) return { kind: 'message', title: 'Агент', body }
      const toolBlock = content.find((block) => block.type === 'tool_use')
      if (toolBlock) {
        const tool = clean(toolBlock.name) || 'tool'
        const body = inputSummary(toolBlock.input) || tool
        return { kind: 'tool', title: TOOL_LABELS[tool.toLowerCase()] ?? `Инструмент: ${tool}`, body }
      }
      return undefined
    }
    if (type === 'user') {
      const toolResult = content.find((block) => block.type === 'tool_result')
      const resultContent = toolResult?.content
      const detail = outputSummary(Array.isArray(resultContent) ? resultContent.map((block) => clean((block as Record<string, unknown>).text)).join(' ') : resultContent)
      return detail ? { kind: 'tool', title: 'Результат инструмента', body: detail } : undefined
    }
  }
  if (type === 'result') {
    const isError = Boolean(event.is_error)
    const body = clean(event.result) || (isError ? 'Агент завершил работу с ошибкой.' : 'Готово')
    return { kind: isError ? 'error' : 'message', title: isError ? 'Ошибка' : 'Готово', body }
  }
  if (type === 'system') return undefined
  if (type === 'text' || partType === 'text') {
    const body = clean(part?.text ?? event.text)
    return body ? { kind: 'message', title: 'Агент', body } : undefined
  }
  if (type === 'tool_use' || partType === 'tool') {
    const tool = clean(part?.tool ?? event.tool) || 'tool'
    const state = part?.state && typeof part.state === 'object' ? part.state as Record<string, unknown> : undefined
    const body = inputSummary(state?.input) || tool
    const detail = outputSummary(state?.output)
    return {
      // A tool activity card describes the action, not the outcome. Providers
      // may emit an intermediate/failed tool state for an optional probe (for
      // example reading a planner result before it exists). Keep the action
      // neutral; explicit error/result events below remain red.
      kind: 'tool',
      title: TOOL_LABELS[tool] ?? `Инструмент: ${tool}`,
      body,
      ...(detail ? { detail } : {}),
    }
  }
  if (type.includes('error') || partType.includes('error')) {
    const body = clean(event.error ?? event.message ?? part?.error) || 'Агент сообщил об ошибке.'
    return { kind: 'error', title: 'Ошибка агента', body: compact(body, 500) }
  }
  if (type === 'step_start' || type === 'step_finish' || partType === 'step-start' || partType === 'step-finish') return undefined
  if (type || event.sessionID || event.messageID) return undefined
  return { kind: 'raw', body: clean(trimmed) }
}

const STDERR_ERROR_PATTERN = /\b(error|fatal|exception|traceback|denied)\b/i
const STDERR_WARNING_PATTERN = /\bwarn(?:ing)?\b/i

export function presentLogs(logs: JobOutput[]): PresentedLog[] {
  const result: PresentedLog[] = []
  const buffers = new Map<string, string>()
  const bufferSources = new Map<string, JobOutput['source']>()
  const consume = (line: string, stream: JobOutput['stream'], source?: JobOutput['source']) => {
    const presented = parseAgentLine(line)
    if (!presented) return
    // stderr is not exclusively for errors -- git ("warning: CRLF will be
    // replaced..."), yarn/npm and the agent CLIs themselves routinely write
    // plain progress/info text there. Unconditionally red-flagging every
    // unrecognized stderr line (the previous rule) turned routine noise into
    // what looked like a failed step. Only escalate when the text itself
    // reads like a problem; anything else stays neutral, same as stdout.
    if (stream === 'stderr' && presented.kind === 'raw' && !presented.title) {
      if (STDERR_ERROR_PATTERN.test(presented.body)) presented.kind = 'error'
      else if (STDERR_WARNING_PATTERN.test(presented.body)) presented.kind = 'warning'
    }
    result.push({ ...presented, ...(source ? { source } : {}) })
  }

  for (const entry of logs) {
    if (entry.stream === 'system') {
      const body = clean(entry.line)
      const userMessage = /^Вы → агент:\s*(.*)$/s.exec(body)
      if (userMessage) result.push({ kind: 'user', title: 'Вы', body: userMessage[1].trim(), ...(entry.source ? { source: entry.source } : {}) })
      else result.push({ kind: 'system', body, ...(entry.source ? { source: entry.source } : {}) })
      continue
    }
    const key = `${entry.jobId}:${entry.stream}`
    const combined = `${buffers.get(key) ?? ''}${entry.line.replace(ANSI_PATTERN, '')}`
    const lines = combined.split(/\r?\n/)
    buffers.set(key, lines.pop() ?? '')
    if (entry.source) bufferSources.set(key, entry.source)
    for (const line of lines) consume(line, entry.stream, entry.source)
  }
  for (const [key, line] of buffers) {
    if (line.trim()) consume(line, key.endsWith(':stderr') ? 'stderr' : 'stdout', bufferSources.get(key))
  }
  return result
}
function addNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

// `total` is always derived from the parts we track, never trusted from a
// provider-reported field: a cache-using OpenCode step already folds cache
// reads into its own `tokens.total`, while Claude's `result.usage` reports
// input/output with cache as separate sibling fields and no total at all. Two
// different shapes for "the total" meant the tooltip breakdown (input+output+
// reasoning) silently excluded cache and no longer summed to the headline
// number -- on a real run where cache reads were the majority of the session
// (21.8M of 29.8M tokens), that made an already-large number look additionally
// broken.
export function summarizeTokenUsage(logs: JobOutput[]): TokenUsage {
  const usage: TokenUsage = { total: 0, input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0, cost: 0 }
  const buffers = new Map<string, string>()
  const consume = (line: string) => {
    let event: Record<string, unknown>
    try {
      event = JSON.parse(line) as Record<string, unknown>
    } catch {
      return
    }
    const part = event.part && typeof event.part === 'object' ? event.part as Record<string, unknown> : undefined
    const type = clean(event.type)
    const partType = clean(part?.type)
    if (type === 'result') {
      // Anthropic's Messages API usage shape (used by Claude Code's stream-json
      // `result` event): input_tokens/output_tokens are fresh tokens billed at
      // full price; cache_read/cache_creation are the same context served from
      // or written to the prompt cache, usually far cheaper.
      const usageObj = event.usage && typeof event.usage === 'object' ? event.usage as Record<string, unknown> : undefined
      usage.input += addNumber(usageObj?.input_tokens)
      usage.output += addNumber(usageObj?.output_tokens)
      usage.cacheRead += addNumber(usageObj?.cache_read_input_tokens)
      usage.cacheWrite += addNumber(usageObj?.cache_creation_input_tokens)
      usage.cost += addNumber(event.total_cost_usd)
      return
    }
    if (type !== 'step_finish' && partType !== 'step-finish') return
    const tokens = part?.tokens && typeof part.tokens === 'object' ? part.tokens as Record<string, unknown> : undefined
    const cache = tokens?.cache && typeof tokens.cache === 'object' ? tokens.cache as Record<string, unknown> : undefined
    usage.input += addNumber(tokens?.input)
    usage.output += addNumber(tokens?.output)
    usage.reasoning += addNumber(tokens?.reasoning)
    usage.cacheRead += addNumber(cache?.read)
    usage.cacheWrite += addNumber(cache?.write)
    usage.cost += addNumber(part?.cost)
  }

  for (const entry of logs) {
    if (entry.stream === 'system') continue
    const key = `${entry.jobId}:${entry.stream}`
    const combined = `${buffers.get(key) ?? ''}${entry.line.replace(ANSI_PATTERN, '')}`
    const lines = combined.split(/\r?\n/)
    buffers.set(key, lines.pop() ?? '')
    for (const line of lines) consume(line)
  }
  for (const line of buffers.values()) {
    if (line.trim()) consume(line)
  }
  usage.total = usage.input + usage.output + usage.reasoning + usage.cacheRead + usage.cacheWrite
  return usage
}

// The token counter should answer "what did the run I'm looking at cost", not
// "what has this open desktop window accumulated since it was last cleared".
// `logs` keeps full history across every stage for the activity/raw views
// (by design, for later review), so scope the counter to the most recent real
// job instead of summing everything ever logged in the session.
export function latestJobId(logs: JobOutput[]): string | undefined {
  for (let index = logs.length - 1; index >= 0; index -= 1) {
    const { jobId } = logs[index]
    if (jobId !== 'system' && jobId !== 'download') return jobId
  }
  return undefined
}
