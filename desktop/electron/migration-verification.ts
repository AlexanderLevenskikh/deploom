export type VerificationDiagnosticEvidence = {
  lines: string[]
  complete: boolean
  informative: boolean
  truncated: boolean
  totalBytes: number
}

export type VerificationEvidence = {
  command: string
  baselineExit?: number
  postExit?: number
  baselineText?: string
  postText?: string
  baselineDiagnostics?: string[]
  postDiagnostics?: string[]
  baselineDiagnosticsComplete?: boolean
  postDiagnosticsComplete?: boolean
  baselineDiagnosticsInformative?: boolean
  postDiagnosticsInformative?: boolean
  baselineDiagnosticsTruncated?: boolean
  postDiagnosticsTruncated?: boolean
}

export type VerificationFailure = {
  command: string
  code: number
  diagnostics: VerificationDiagnosticEvidence
}

export type BaselineVerificationObservation = {
  code: number
  diagnostics: VerificationDiagnosticEvidence
}

export type BaselineFailureDecision = 'tolerate' | 'probe' | 'regression'

export type MigrationVerificationAssessment = {
  status: 'pass' | 'repair-required' | 'replan-required' | 'unknown'
  regressions: VerificationEvidence[]
  missingPlanGroups: string[]
  evidence: VerificationEvidence[]
  reason?: string
  feedback: string
}

type UnknownRecord = Record<string, unknown>

const DEFAULT_DIAGNOSTIC_MAX_CHARS = 256 * 1024
const DEFAULT_DIAGNOSTIC_MAX_CARRY_CHARS = 16 * 1024
const ANSI_PATTERN = /\u001B\[[0-?]*[ -/]*[@-~]/g
const GENERIC_DIAGNOSTIC_PATTERN = /^(?:error\s+)?command failed(?: with exit code \d+)?\.?$|^process exited with (?:code|status) \d+\.?$|^npm err! lifecycle script .* failed.*$|^npm err! code elifecycle$|^error command failed\.?$/i
const RECOGNIZED_DIAGNOSTIC_PATTERNS = [
  /\b(?:error|warning)\s+TS\d{4}\b/i,
  /^\s*\d+:\d+\s+(?:error|warning)\s+\S/i,
  /\b(?:TypeError|ReferenceError|SyntaxError|RangeError|AssertionError|AggregateError)\s*:\s*\S/i,
  /\b(?:ERR_[A-Z0-9_]+|Cannot find module|Module not found|No overload matches|is not assignable to|does not exist on type|Unexpected token)\b/i,
  /^ERROR(?:\s+in\b|\s*:\s*\S)/i,
  /^error during (?:build|startup|test)\b/i,
  /^(?:FAIL|FAILED)\b/i,
  /[✖×]\s+\S/,
] as const

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : {}
}

function parseExit(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isInteger(value)) return value
  if (typeof value !== 'string') return undefined
  const text = value.trim()
  if (!text || /\bn\/?a\b|not run|не запуск/i.test(text)) return undefined
  const match = /(?:exit(?:\s+code)?\s*[:=]?\s*|(?:baseline|post(?:-update)?)\s*[:=]\s*)(\d+)/i.exec(text)
  if (match) return Number(match[1])
  if (/^(?:pass|passed|success|ok)$/i.test(text)) return 0
  return undefined
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function diagnosticArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const result = value
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim().replace(/\\/g, '/').replace(/\s+/g, ' '))
    .filter(Boolean)
  return result.length ? [...new Set(result)] : undefined
}

function booleanValue(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}

function canonicalizeDiagnosticLine(line: string, cwd?: string): string | undefined {
  let normalized = line.replace(ANSI_PATTERN, '').trim()
  if (!normalized) return undefined

  const root = String(cwd ?? '').trim()
  if (root) {
    const variants = [...new Set([
      root,
      root.replace(/\\/g, '/'),
      root.replace(/\//g, '\\'),
    ].filter(Boolean))].sort((left, right) => right.length - left.length)
    for (const variant of variants) {
      normalized = normalized.replace(new RegExp(escapeRegExp(variant), 'gi'), '<project>')
    }
  }

  normalized = normalized
    .replace(/\\/g, '/')
    .replace(/\s+/g, ' ')
    .replace(/\bpid[=: ]+\d+\b/gi, 'pid=<pid>')
    .replace(/\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds|minutes|min)\b/gi, '<duration>')
    .trim()

  if (/^Done in <duration>\.?$/i.test(normalized)) return undefined
  if (/^(?:real|user|sys)\s+<duration>$/i.test(normalized)) return undefined
  if (/^yarn run v\d+/i.test(normalized)) return undefined
  if (/^(?:info|verbose)\s+.*progress/i.test(normalized)) return undefined

  return normalized
}

export function isGenericVerificationDiagnostic(line: string): boolean {
  return GENERIC_DIAGNOSTIC_PATTERN.test(line.trim())
}

export function isRecognizedVerificationDiagnostic(line: string): boolean {
  const normalized = line.trim()
  if (!normalized || isGenericVerificationDiagnostic(normalized)) return false
  return RECOGNIZED_DIAGNOSTIC_PATTERNS.some((pattern) => pattern.test(normalized))
}

export function createVerificationDiagnosticCollector(
  cwd?: string,
  maxStoredChars = DEFAULT_DIAGNOSTIC_MAX_CHARS,
  maxCarryChars = Math.min(DEFAULT_DIAGNOSTIC_MAX_CARRY_CHARS, Math.max(1024, maxStoredChars)),
): {
  push: (text: string, stream?: 'stdout' | 'stderr') => void
  finish: () => VerificationDiagnosticEvidence
} {
  const carries: Record<'stdout' | 'stderr', string> = { stdout: '', stderr: '' }
  const droppingLongLine: Record<'stdout' | 'stderr', boolean> = { stdout: false, stderr: false }
  let storedChars = 0
  let totalBytes = 0
  let truncated = false
  let finished = false
  const lines: string[] = []
  const seen = new Set<string>()

  const store = (raw: string): void => {
    const line = canonicalizeDiagnosticLine(raw, cwd)
    if (!line || seen.has(line)) return
    const cost = line.length + 1
    if (storedChars + cost > maxStoredChars) {
      truncated = true
      return
    }
    seen.add(line)
    lines.push(line)
    storedChars += cost
  }

  const push = (text: string, stream: 'stdout' | 'stderr' = 'stdout'): void => {
    if (finished || !text) return
    totalBytes += Buffer.byteLength(text)
    let incoming = text

    if (droppingLongLine[stream]) {
      const boundary = incoming.search(/[\r\n]/)
      if (boundary < 0) return
      incoming = incoming.slice(boundary + 1)
      droppingLongLine[stream] = false
    }

    const combined = carries[stream] + incoming
    const parts = combined.split(/\r\n|\n|\r/)
    const tail = parts.pop() ?? ''
    for (const part of parts) store(part)

    if (tail.length > maxCarryChars) {
      truncated = true
      carries[stream] = ''
      droppingLongLine[stream] = true
    } else {
      carries[stream] = tail
    }
  }

  const finish = (): VerificationDiagnosticEvidence => {
    if (!finished) {
      finished = true
      for (const stream of ['stdout', 'stderr'] as const) {
        if (carries[stream]) store(carries[stream])
        carries[stream] = ''
        if (droppingLongLine[stream]) truncated = true
        droppingLongLine[stream] = false
      }
    }
    const informative = lines.some(isRecognizedVerificationDiagnostic)
    return {
      lines: [...lines],
      complete: !truncated,
      informative,
      truncated,
      totalBytes,
    }
  }

  return { push, finish }
}

export function verificationDiagnosticEvidenceFromText(
  text: string,
  cwd?: string,
  maxStoredChars = DEFAULT_DIAGNOSTIC_MAX_CHARS,
): VerificationDiagnosticEvidence {
  const collector = createVerificationDiagnosticCollector(cwd, maxStoredChars)
  collector.push(text)
  return collector.finish()
}

export function normalizeVerificationDiagnostics(text: string, cwd?: string): string[] {
  return verificationDiagnosticEvidenceFromText(text, cwd).lines
}

export function baselineObservationMatchesFailure(
  failure: VerificationFailure,
  baseline: BaselineVerificationObservation,
): boolean {
  if (failure.code === 0 || baseline.code === 0) return false
  if (!failure.diagnostics.complete || !baseline.diagnostics.complete) return false
  if (failure.diagnostics.truncated || baseline.diagnostics.truncated) return false
  if (!failure.diagnostics.informative || !baseline.diagnostics.informative) return false
  if (!failure.diagnostics.lines.length || !baseline.diagnostics.lines.length) return false

  const known = new Set(baseline.diagnostics.lines)
  return failure.diagnostics.lines.every((diagnostic) => known.has(diagnostic))
}

function evidenceBaselineObservation(evidence: VerificationEvidence): BaselineVerificationObservation | undefined {
  if (evidence.baselineExit === undefined || evidence.baselineExit === 0) return undefined
  if (!evidence.baselineDiagnostics?.length) return undefined
  if (evidence.baselineDiagnosticsComplete !== true) return undefined
  if (evidence.baselineDiagnosticsInformative !== true) return undefined
  if (evidence.baselineDiagnosticsTruncated === true) return undefined
  return {
    code: evidence.baselineExit,
    diagnostics: {
      lines: evidence.baselineDiagnostics,
      complete: true,
      informative: true,
      truncated: false,
      totalBytes: 0,
    },
  }
}

export function baselineFailureDecision(
  failure: VerificationFailure,
  evidence: VerificationEvidence | undefined,
): BaselineFailureDecision {
  if (!evidence || evidence.baselineExit === undefined) return 'probe'
  if (evidence.baselineExit === 0) return 'regression'
  const baseline = evidenceBaselineObservation(evidence)
  if (!baseline) return 'probe'
  return baselineObservationMatchesFailure(failure, baseline) ? 'tolerate' : 'probe'
}

export function baselineFailuresNeedingProbe(
  failures: readonly VerificationFailure[],
  evidence: readonly VerificationEvidence[],
): VerificationFailure[] {
  const byCommand = new Map(evidence.map((item) => [verificationCommandKey(item.command), item]))
  return failures.filter((failure) =>
    baselineFailureDecision(failure, byCommand.get(verificationCommandKey(failure.command))) === 'probe')
}

export function verificationCommandKey(command: string): string {
  return command
    .trim()
    .replace(/^\s*(?:yarn(?:\s+run)?|npm\s+run|pnpm(?:\s+run)?)\s+/i, '')
    .replace(/\s+\([^)]*\)\s*$/, '')
    .trim()
    .toLowerCase()
}

function commandLabelFromString(text: string): string | undefined {
  const trimmed = text.trim()
  if (!trimmed || /^baseline\b/i.test(trimmed)) return undefined
  const beforeArrow = trimmed.split(/\s*->\s*/)[0]?.trim()
  const beforeParen = (beforeArrow || trimmed).split(/\s+\(/)[0]?.trim()
  return beforeParen || undefined
}

function baselineSummaryEntries(text: string): Array<{ command: string; exit: number }> {
  if (!/\bbaseline\b/i.test(text)) return []
  const entries: Array<{ command: string; exit: number }> = []
  for (const match of text.matchAll(/([@\w.-]+(?::[\w.-]+)*)\s*=\s*(\d+)/g)) {
    entries.push({ command: match[1], exit: Number(match[2]) })
  }
  return entries
}

function evidenceFromObject(value: UnknownRecord): VerificationEvidence | undefined {
  const command = typeof value.cmd === 'string' ? value.cmd.trim()
    : typeof value.command === 'string' ? value.command.trim()
      : ''
  if (!command) return undefined

  const baselineRaw = value.baseline ?? value.baselineExit
  const postRaw = value.post ?? value.postExit ?? value.result ?? value.current
  const baselineText = baselineRaw === undefined ? undefined : String(baselineRaw)
  const postText = postRaw === undefined ? undefined : String(postRaw)
  const baselineDiagnostics = diagnosticArray(value.baselineDiagnostics)
  const postDiagnostics = diagnosticArray(value.postDiagnostics)

  return {
    command,
    ...(baselineRaw !== undefined ? { baselineText, baselineExit: parseExit(baselineRaw) } : {}),
    ...(postRaw !== undefined ? { postText, postExit: parseExit(postRaw) } : {}),
    ...(baselineDiagnostics ? { baselineDiagnostics } : {}),
    ...(postDiagnostics ? { postDiagnostics } : {}),
    ...(booleanValue(value.baselineDiagnosticsComplete) !== undefined ? { baselineDiagnosticsComplete: booleanValue(value.baselineDiagnosticsComplete) } : {}),
    ...(booleanValue(value.postDiagnosticsComplete) !== undefined ? { postDiagnosticsComplete: booleanValue(value.postDiagnosticsComplete) } : {}),
    ...(booleanValue(value.baselineDiagnosticsInformative) !== undefined ? { baselineDiagnosticsInformative: booleanValue(value.baselineDiagnosticsInformative) } : {}),
    ...(booleanValue(value.postDiagnosticsInformative) !== undefined ? { postDiagnosticsInformative: booleanValue(value.postDiagnosticsInformative) } : {}),
    ...(booleanValue(value.baselineDiagnosticsTruncated) !== undefined ? { baselineDiagnosticsTruncated: booleanValue(value.baselineDiagnosticsTruncated) } : {}),
    ...(booleanValue(value.postDiagnosticsTruncated) !== undefined ? { postDiagnosticsTruncated: booleanValue(value.postDiagnosticsTruncated) } : {}),
  }
}

function evidenceFromString(text: string): VerificationEvidence | undefined {
  const command = commandLabelFromString(text)
  if (!command) return undefined
  const baselineMatch = /\bbaseline(?:\s+fail)?\s*(?::|=|\s)\s*(?:exit\s*)?(\d+)/i.exec(text)
  const postMatch = /\bpost(?:-update)?\s*(?::|=|\s)\s*(?:exit\s*)?(\d+)/i.exec(text)
  const arrowExit = /->\s*exit\s*(\d+)/i.exec(text)
  const genericExit = /\bexit\s*(\d+)/i.exec(text)
  const baselineExit = baselineMatch ? Number(baselineMatch[1]) : undefined
  const postExit = postMatch ? Number(postMatch[1]) : arrowExit ? Number(arrowExit[1]) : genericExit ? Number(genericExit[1]) : undefined
  return { command, baselineExit, postExit, baselineText: text, postText: text }
}

function explicitOutcome(checkpoint: UnknownRecord): { status?: MigrationVerificationAssessment['status']; reason?: string } {
  const raw = asRecord(checkpoint.migrationOutcome ?? checkpoint.verificationOutcome)
  const value = String(raw.status ?? '').trim().toLowerCase()
  const reason = typeof raw.reason === 'string' ? raw.reason.trim() : undefined
  if (['ready', 'pass', 'passed'].includes(value)) return { status: 'pass', reason }
  if (['repair-required', 'repair_required', 'fix-required'].includes(value)) return { status: 'repair-required', reason }
  if (['replan-required', 'replan_required', 'blocked-plan-level', 'plan-blocked'].includes(value)) return { status: 'replan-required', reason }
  return { reason }
}

function planGroupNames(planBranches: readonly string[]): Set<string> {
  const result = new Set<string>()
  for (const branch of planBranches) {
    const matches = branch.match(/group-[\w.-]+/gi) ?? []
    matches.forEach((value) => result.add(value.toLowerCase()))
  }
  return result
}

function missingCrossCohortGroups(checkpoint: UnknownRecord, branches: readonly string[]): string[] {
  const known = planGroupNames(branches)
  const haystack = JSON.stringify(checkpoint)
  const missing = new Set<string>()
  for (const match of haystack.matchAll(/cross[- _]?cohort[^\n\r"]{0,240}\b(group-[\w.-]+)/gi)) {
    const group = match[1].toLowerCase()
    if (!known.has(group)) missing.add(group)
  }
  return [...missing].sort()
}

export function assessMigrationCheckpoint(checkpointValue: unknown, planBranches: readonly string[] = []): MigrationVerificationAssessment {
  const checkpoint = asRecord(checkpointValue)
  if (!Object.keys(checkpoint).length) {
    return { status: 'unknown', regressions: [], missingPlanGroups: [], evidence: [], feedback: 'verification checkpoint отсутствует или не читается' }
  }

  const baselineByCommand = new Map<string, number>()
  const latest = new Map<string, VerificationEvidence>()
  const commands = Array.isArray(checkpoint.commands) ? checkpoint.commands : []
  for (const raw of commands) {
    if (typeof raw === 'string') {
      for (const entry of baselineSummaryEntries(raw)) baselineByCommand.set(verificationCommandKey(entry.command), entry.exit)
      const evidence = evidenceFromString(raw)
      if (evidence) {
        const key = verificationCommandKey(evidence.command)
        if (evidence.baselineExit !== undefined) baselineByCommand.set(key, evidence.baselineExit)
        latest.set(key, evidence)
      }
      continue
    }
    const evidence = evidenceFromObject(asRecord(raw))
    if (!evidence) continue
    const key = verificationCommandKey(evidence.command)
    if (evidence.baselineExit !== undefined) baselineByCommand.set(key, evidence.baselineExit)
    latest.set(key, evidence)
  }

  const verification = asRecord(checkpoint.verification)
  const structuredCommands = Array.isArray(verification.commands) ? verification.commands : []
  for (const raw of structuredCommands) {
    const evidence = evidenceFromObject(asRecord(raw))
    if (!evidence) continue
    const key = verificationCommandKey(evidence.command)
    if (evidence.baselineExit !== undefined) baselineByCommand.set(key, evidence.baselineExit)
    latest.set(key, evidence)
  }

  const regressions: VerificationEvidence[] = []
  for (const [key, value] of latest) {
    const baselineExit = value.baselineExit ?? baselineByCommand.get(key)
    if (baselineExit === 0 && value.postExit !== undefined && value.postExit !== 0) {
      regressions.push({ ...value, baselineExit })
    }
  }

  const missingPlanGroups = missingCrossCohortGroups(checkpoint, planBranches)
  const explicit = explicitOutcome(checkpoint)
  const statusText = String(checkpoint.status ?? '').toLowerCase()
  const nextAction = String(checkpoint.nextAction ?? '')
  const planBlocked = /blocked[- _]?plan|plan[- _]?level/i.test(statusText)
    || /^\s*blocked\b/i.test(nextAction)
    || /replan[- _]?required/i.test(nextAction)

  let status: MigrationVerificationAssessment['status'] = 'pass'
  let reason = explicit.reason
  if (explicit.status === 'replan-required' || planBlocked || missingPlanGroups.length) {
    status = 'replan-required'
    reason = reason || (missingPlanGroups.length
      ? `agent сослался на CROSS-COHORT ${missingPlanGroups.join(', ')}, которого нет в текущем Branch plan`
      : 'checkpoint помечен как plan-level blocker')
  } else if (explicit.status === 'repair-required' || regressions.length) {
    status = 'repair-required'
    reason = reason || 'после обновления появились новые падения относительно зелёного baseline'
  } else if (explicit.status === 'pass') {
    status = 'pass'
  }

  const parts: string[] = []
  if (regressions.length) {
    parts.push(`новые verification-регрессии: ${regressions.map((item) => `${item.command}: baseline=${item.baselineExit}, post=${item.postExit}`).join('; ')}`)
  }
  if (missingPlanGroups.length) parts.push(`CROSS-COHORT с отсутствующей веткой Branch plan: ${missingPlanGroups.join(', ')}`)
  if (reason && !parts.some((part) => part.includes(reason as string))) parts.push(reason)
  if (!parts.length) parts.push(status === 'pass' ? 'новых regression gates в checkpoint не обнаружено' : 'verification evidence недостаточно')

  return {
    status,
    regressions,
    missingPlanGroups,
    evidence: [...latest.values()].map((item) => ({
      ...item,
      baselineExit: item.baselineExit ?? baselineByCommand.get(verificationCommandKey(item.command)),
    })),
    ...(reason ? { reason } : {}),
    feedback: parts.join('; '),
  }
}

export function unexplainedFailures(failures: readonly { command: string }[], evidence: readonly VerificationEvidence[]): { command: string }[] {
  const explainedKeys = new Set(
    evidence.filter((item) => item.baselineExit !== undefined).map((item) => verificationCommandKey(item.command)),
  )
  return failures.filter((item) => !explainedKeys.has(verificationCommandKey(item.command)))
}
