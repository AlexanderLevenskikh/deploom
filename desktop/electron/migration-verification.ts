export type VerificationEvidence = {
  command: string
  baselineExit?: number
  postExit?: number
  baselineText?: string
  postText?: string
}

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
  // Covers compact evidence such as: "BASELINE: lint:types=0, lint:styles=0"
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
  const baselineText = value.baseline === undefined ? undefined : String(value.baseline)
  const postRaw = value.post ?? value.result ?? value.current
  const postText = postRaw === undefined ? undefined : String(postRaw)
  return {
    command,
    ...(baselineText !== undefined ? { baselineText, baselineExit: parseExit(baselineText) } : {}),
    ...(postText !== undefined ? { postText, postExit: parseExit(postText) } : {}),
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
  // Do not treat an arbitrary mention of a group as a blocker. Require the
  // agent to have explicitly classified it as CROSS-COHORT / cross cohort.
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

  // The structured `verification.commands` form is preferred for new runs,
  // but historical checkpoints stored the same objects directly in commands.
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

// Which of a set of currently-failing verification commands have no defined
// baseline exit code recorded anywhere in a checkpoint's evidence. These are
// the only safe candidates for a live baseline probe (running the command
// fresh against the plan's own baseBranch) -- a command some group's own
// focused checks DID record, whether tolerated already or a genuine
// baseline=0-to-failing regression, must never be re-judged by a live probe;
// that stricter, evidence-backed classification always wins. Without this
// distinction, a gate command no group's scope ever happened to cover (e.g.
// an eslint step unrelated to any updated package) has no evidence anywhere
// and can never be forgiven, which is exactly what exhausted 3 real repair
// attempts on a chronic, migration-unrelated .eslintrc.js/tsconfig.json
// mismatch on a real deps-demo-merged run.
export function unexplainedFailures(failures: readonly { command: string }[], evidence: readonly VerificationEvidence[]): { command: string }[] {
  const explainedKeys = new Set(
    evidence.filter((item) => item.baselineExit !== undefined).map((item) => verificationCommandKey(item.command)),
  )
  return failures.filter((item) => !explainedKeys.has(verificationCommandKey(item.command)))
}
