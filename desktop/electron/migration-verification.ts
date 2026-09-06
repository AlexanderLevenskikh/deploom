export type VerificationEvidence = {
  command: string
  baselineExit?: number
  postExit?: number
  baselineText?: string
  postText?: string
  baselineDiagnostics?: string[]
  postDiagnostics?: string[]
}

export type VerificationFailure = {
  command: string
  code: number
  diagnostics: string[]
}

export type BaselineVerificationObservation = {
  code: number
  diagnostics: string[]
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

export function normalizeVerificationDiagnostics(text: string, cwd?: string): string[] {
  let normalized = String(text ?? '').replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
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

  const lines = normalized
    .replace(/\r/g, '')
    .split('\n')
    .map((line) => line.trim().replace(/\\/g, '/').replace(/\s+/g, ' '))
    .filter(Boolean)
    .filter((line) => !/^Done in \d+(?:\.\d+)?s\.?$/i.test(line))
    .filter((line) => !/^(?:real|user|sys)\s+\d+(?:\.\d+)?/i.test(line))

  const diagnosticLike = lines.filter((line) =>
    /\b(?:error|failed|failure|exception|fatal|err!|ts\d{4})\b|[✖×]/i.test(line)
    || /[:(]\d+[:,]\d+\)?/.test(line),
  )
  return [...new Set(diagnosticLike.length ? diagnosticLike : lines)]
}

export function baselineObservationMatchesFailure(
  failure: VerificationFailure,
  baseline: BaselineVerificationObservation,
): boolean {
  if (failure.code === 0 || baseline.code === 0) return false
  if (!failure.diagnostics.length || !baseline.diagnostics.length) return false
  const known = new Set(baseline.diagnostics)
  return failure.diagnostics.every((diagnostic) => known.has(diagnostic))
}

export function baselineFailureDecision(
  failure: VerificationFailure,
  evidence: VerificationEvidence | undefined,
): BaselineFailureDecision {
  if (!evidence || evidence.baselineExit === undefined) return 'probe'
  if (evidence.baselineExit === 0) return 'regression'
  const baselineDiagnostics = evidence.baselineDiagnostics?.length
    ? evidence.baselineDiagnostics
    : normalizeVerificationDiagnostics(evidence.baselineText ?? '')
  return baselineObservationMatchesFailure(
    failure,
    { code: evidence.baselineExit, diagnostics: baselineDiagnostics },
  ) ? 'tolerate' : 'probe'
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

  return { status, regressions, missingPlanGroups, evidence: [...latest.values()].map((item) => ({ ...item, baselineExit: item.baselineExit ?? baselineByCommand.get(verificationCommandKey(item.command)) })), ...(reason ? { reason } : {}), feedback: parts.join('; ') }
}

// Backwards-compatible helper for older focused checks. The live gate uses
// baselineFailuresNeedingProbe(), because a defined non-zero exit alone is no
// longer sufficient authority to suppress a migration failure.
export function unexplainedFailures(failures: readonly { command: string }[], evidence: readonly VerificationEvidence[]): { command: string }[] {
  const explainedKeys = new Set(
    evidence.filter((item) => item.baselineExit !== undefined).map((item) => verificationCommandKey(item.command)),
  )
  return failures.filter((item) => !explainedKeys.has(verificationCommandKey(item.command)))
}
