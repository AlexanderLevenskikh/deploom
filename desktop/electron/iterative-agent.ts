// Desktop -> repair agent handoff for the iterative migration (increment 2).
//
// The repair agent works in the isolated trial (the Python-created private
// tree at <runDir>/trial/workspace) on the OpenRepairRequests the Python
// REPAIRING phase publishes. This module owns the CONTRACT between that agent
// and the authoritative Python CLI:
//   - the prompt embeds the ТЗ task text, the exact open repair requests
//     (failing commands + diagnostics tail) and hard guardrails (only source/
//     config files under the trial's project, never node_modules or the
//     dependency-declaring files Python itself owns);
//   - the agent's real edits are measured as a HASH DIFF against a durable
//     baseline captured BEFORE the agent runs (the trial is a copied private
//     tree without a guaranteed .git), so a restart re-writes the same
//     baseline and never counts the coordinator's own files;
//   - the typed feedback JSON is built ONLY from the durable candidate
//     identity (runId/candidateId/baseCheckpointId/attemptId), never from
//     agent text; apply-feedback (the authoritative verifier of staleness and
//     changed-file scope) is then invoked with the real CLI.
import { migrationResearchPrompt } from './migration-research-prompt.js'
import { createHash } from 'node:crypto'
import { agentLaunchProviderError } from './agent-launch-errors.js'
import { existsSync, mkdirSync, readdirSync, readFileSync, lstatSync, statSync, unlinkSync, writeFileSync, renameSync, appendFileSync } from 'node:fs'
import { dirname, isAbsolute, join } from 'node:path'
import type { AgentLaunchDiagnostics } from './agent-launch-errors.js'
import { runtimePolicyFor, type AgentRuntimeScope, type ClassifiedTrialChange, type TrialMutationKind } from './agent-runtime-policy.js'

export { AGENT_RUNTIME_POLICY_VERSION, openCodeRuntimeManifestPaths } from './agent-runtime-policy.js'
export type { AgentRuntimeScope, TrialMutationKind } from './agent-runtime-policy.js'

export type IterativeRepairRequest = {
  requestId: string
  reason: string
  failingCommands: Array<{ command: string; exitCode: number }>
  diagnosticsTail: string
}

export type IterativeAgentContext = {
  researchTool?: string
  verificationCommands?: string[]
  runId: string
  candidateId: string
  baseCheckpointId: string
  attemptId: number
  projectName: string
  targetLevel: string
  workspaceRoot: string
  projectRelative: string
  assignment: Array<[string, string]>
  repairRequests: IterativeRepairRequest[]
  taskText: string
}

export type IterativeBootstrapContext = {
  verificationCommands?: string[]
  runId: string
  checkpointId: string
  projectName: string
  targetLevel: string
  workspaceRoot: string
  projectRelative: string
  assignment: Array<[string, string]>
  repairRequests: IterativeRepairRequest[]
  taskText: string
}

export type IterativeFeedbackKind =
  | 'REPAIRING'
  | 'READY_FOR_VERIFY'
  | 'NEEDS_COHORT_EXPANSION'
  | 'NEEDS_ALTERNATIVE'
  | 'INCONCLUSIVE'
  | 'INFRA_BLOCKED'

// Files the agent may never touch in the isolated trial; the controller is
// the only writer of dependency state (mirrors FORBIDDEN_TRIAL_RELATIVES).
export const ITERATIVE_FORBIDDEN_NAMES = new Set([
  'package.json',
  'package-lock.json',
  'yarn.lock',
  'pnpm-lock.yaml',
  'npm-shrinkwrap.json',
  '.npmrc',
  '.yarnrc',
  '.yarnrc.yml',
  '.pnpmfile.cjs',
])

const CHANGED_FILES_MARKER = 'CHANGED_FILES:'
export const TRIAL_BASELINE_FILENAME = 'agent-baseline.json'
export const AGENT_PROMPT_FILENAME = 'agent-prompt.md'

function verificationProfileText(commands: string[] = []): string {
  return [
    '## Настроенные проверки / Configured verification commands',
    commands.length ? commands.map(command => `  $ ${command}`).join('\n') : 'Список проверок недоступен; не угадывай команды. / Verification commands are unavailable; do not guess.',
    'Используй точные команды выше, включая аргументы и область тестов. Не заменяй scoped unit-проверку общим yarn test и не добавляй Playwright/e2e без явного требования профиля.',
    'Use the exact commands above, including arguments and test scope. Do not replace a scoped unit check with plain yarn test or add Playwright/e2e unless the profile requires it.',
    'Дополнительные проверки отмечай отдельно: они не меняют критерии принятия. Называй сбой pre-existing только при наличии сопоставимого результата той же команды на исходном checkpoint; иначе причина не подтверждена.',
    'Label additional checks separately; they do not change acceptance criteria. Call a failure pre-existing only with comparable evidence from the same command on the base checkpoint; otherwise its origin is unconfirmed.',
    'Окончательное принятие выполняет контроллер после свежей верификации. / The controller accepts changes only after fresh verification.',
  ].join('\n')
}

/** RU repair prompt. The task text is the ТЗ body; repair requests carry the
 * exact failing commands the PRE-check reported. */
export function buildIterativeRepairPrompt(ctx: IterativeAgentContext): string {
  const forbidden = [...ITERATIVE_FORBIDDEN_NAMES].sort().join(', ')
  const assignment = ctx.assignment.map(([name, version]) => `  - ${name} = ${version}`).join('\n')
  const requests = ctx.repairRequests.map((request, index) => {
    const commands = request.failingCommands
      .map((item) => `  $ ${item.command}  (exit ${item.exitCode})`)
      .join('\n')
    const tail = request.diagnosticsTail ? `\nХвост диагностики (последние строки вывода):\n${request.diagnosticsTail}` : ''
    return (
      `### Запрос на ремонт ${index + 1}: ${request.requestId}\n` +
      `Причина: ${request.reason}\n` +
      `Падающие проверки:\n${commands}${tail}`
    )
  }).join('\n\n')

  return [
    `# Ремонт кандидата обновления зависимостей (DepLoom, iterative migration)`,
    ``,
    `Проект: ${ctx.projectName} (целевой уровень: ${ctx.targetLevel}).`,
    `Кандидат: ${ctx.candidateId} на базе checkpoint ${ctx.baseCheckpointId} (попытка ${ctx.attemptId}).`,
    verificationProfileText(ctx.verificationCommands),
    ``,
    `Точное назначение версий:`,
    assignment,
    ``,
    migrationResearchPrompt(ctx.researchTool),
    `## Задание на адаптацию (ТЗ)`,
    ctx.taskText,
    ``,
    `## Открытые запросы на ремонт`,
    requests,
    ``,
    `## Правила работы`,
    `- Работай ТОЛЬКО внутри изолированного trial: ${join(ctx.workspaceRoot, ctx.projectRelative)}.`,
    `- Меняй только исходный/конфигурационный код проекта. НЕЛЬЗЯ трогать: node_modules и файлы ${forbidden} — это владения контроллера зависимостей.`,
    `- НЕ запускай установку пакетов и не редактируй lock-файлы.`,
    `- Для каждого изменённого файла/ключа/блока объясни в ответе, что изменилось, зачем и с каким обновлением связано. Неочевидные source/config адаптации поясняй краткими полнострочными комментариями: native comment prefix + DEPLOOM-MIGRATION-NOTE:${ctx.runId}: <причина>. Помечай только новые пояснения этого прогона; не меняй обычные комментарии и не вставляй маркер в строки/данные. JSON/lockfile объясняй в отчёте, не добавляя комментариев.`,
    `- Проверки зависимостей и верификация выполнятся после тебя контроллером.`,
    `- Если ремонт невозможен без изменения зависимостей — опиши это словами и предложи альтернативу.`,
    ``,
    `## Формат ответа`,
    `- Ремонт выполнен (изменены исходники): напиши строку "${CHANGED_FILES_MARKER}" и перечисли по одному изменённому файлу на строку, затем подтверди: FEEDBACK_KIND: READY_FOR_VERIFY.`,
    `- Текущий набор версий непригоден, нужен другой вариант: FEEDBACK_KIND: NEEDS_ALTERNATIVE, затем PROPOSALS: (по одной строке «пакет = версия») и REASON:.`,
    `- Нужен соседний пакет или обязательный peer: FEEDBACK_KIND: NEEDS_COHORT_EXPANSION, затем PROPOSALS: по одной строке «пакет = точная версия X.Y.Z» и REASON: с evidence по peer/runtime. Контроллер может добавить отсутствующий registry-пакет в зависимости trial и проверит весь кандидат.`,
    `- Предлагай конкретный следующий вариант: согласованное семейство версий, недостающий peer, адаптацию исходников или конфигурации. Для альтернативы тоже нужны точные версии. Не повторяй прежний неработающий набор.`,
    `- resolutions не устанавливает отсутствующий peer. Не маскируй runtime-ошибки заглушками типов, отключением проверок или подавлением ошибок; опиши компромисс в REASON:.`,
    `- Инфраструктура заблокировала работу: FEEDBACK_KIND: INFRA_BLOCKED и REASON:.`,
    `- Результат неясен: FEEDBACK_KIND: INCONCLUSIVE и REASON:.`,
    `Текст ответа не считается доказательством; контроллер проверит файлы и перезапустит проверки.`,
  ].join('\n')
}

/** RU bootstrap repair prompt. This is VERSION-NEUTRAL: C0 control failed on
 * source/config, not dependency versions. The agent fixes source/config in the
 * isolated version-neutral trial (C0 bytes), and MAY NOT touch dependency
 * versions or lockfiles; verify-bootstrap re-runs the control afterwards. */
export function buildIterativeBootstrapPrompt(ctx: IterativeBootstrapContext): string {
  const forbidden = [...ITERATIVE_FORBIDDEN_NAMES].sort().join(', ')
  const requests = ctx.repairRequests.map((request, index) => {
    const commands = request.failingCommands
      .map((item) => `  $ ${item.command}  (exit ${item.exitCode})`)
      .join('\n')
    const tail = request.diagnosticsTail ? `\nХвост диагностики (последние строки вывода):\n${request.diagnosticsTail}` : ''
    return (
      `### Запрос на ремонт ${index + 1}: ${request.requestId}\n` +
      `Причина: ${request.reason}\n` +
      `Падающие проверки:\n${commands}${tail}`
    )
  }).join('\n\n')

  return [
    `# Bootstrap-ремонт исходников (DepLoom, iterative migration)`,
    ``,
    `Проект: ${ctx.projectName} (целевой уровень: ${ctx.targetLevel}).`,
    `Контрольная проверка C0 (${ctx.checkpointId}) упала на ИСХОДНОМ состоянии зависимостей.`,
    verificationProfileText(ctx.verificationCommands),
    ``,
    `Назначение версий НЕ меняется:`,
    ``,
    `## Текущее назначение (зафиксировано, менять нельзя)`,
    ctx.assignment.map(([name, version]) => `  - ${name} = ${version}`).join('\n'),
    ``,
    `## Задание на адаптацию (ТЗ)`,
    ctx.taskText,
    ``,
    `## Открытые запросы на ремонт`,
    requests,
    ``,
    `## Правила работы`,
    `- Работай ТОЛЬКО внутри изолированного version-neutral trial: ${join(ctx.workspaceRoot, ctx.projectRelative)}.`,
    `- Меняй только исходный/конфигурационный код проекта. НЕЛЬЗЯ трогать: node_modules и файлы ${forbidden} — версии и lock-файлы принадлежат контроллеру.`,
    `- НЕ запускай установку пакетов, не правь package.json и lock-файлы. Это bootstrap: зависимости оставляем как есть.`,
    `- Контрольная проверка перезапустится после тебя контроллером.`,
    `- Если контроль не проходим без изменения зависимостей — опиши это словами и предложи альтернативу.`,
    ``,
    `## Формат ответа`,
    `- Ремонт исходников выполнен: напиши строку "${CHANGED_FILES_MARKER}" и перечисли по одному изменённому файлу на строку, затем подтверди: FEEDBACK_KIND: READY_FOR_VERIFY.`,
    `- Контроль не проходим без изменения зависимостей: FEEDBACK_KIND: NEEDS_ALTERNATIVE, затем PROPOSALS: (по одной строке «пакет = версия») и REASON:.`,
    `- Инфраструктура заблокировала работу: FEEDBACK_KIND: INFRA_BLOCKED и REASON:.`,
    `- Результат неясен: FEEDBACK_KIND: INCONCLUSIVE и REASON:.`,
    `Текст ответа не считается доказательством; контроллер проверит файлы и перезапустит контроль.`,
  ].join('\n')
}

/** Build the typed feedback the Python apply-feedback accepts. Identities are
 * taken ONLY from the durable candidate context, never from agent text.
 * proposedScope/proposedConstraints carry the agent's typed proposals (e.g.
 * NEEDS_COHORT_EXPANSION companions); they never grant verification. */
export function buildFeedbackPayload(
  ctx: Pick<IterativeAgentContext, 'runId' | 'candidateId' | 'baseCheckpointId' | 'attemptId'>,
  changedFiles: string[],
  kind: IterativeFeedbackKind,
  reason: string,
  proposedScope: Record<string, unknown> = {},
  proposedConstraints: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schemaVersion: 1,
    runId: ctx.runId,
    candidateId: ctx.candidateId,
    baseCheckpointId: ctx.baseCheckpointId,
    attemptId: ctx.attemptId,
    kind,
    reason,
    changedFiles,
    diagnosticsRefs: [],
    proposedScope,
    proposedConstraints,
  }
}

export function iterativeApplyFeedbackInvocation(
  runDir: string,
  feedbackFile: string,
  scriptPath: string,
  python: string = 'python',
): { command: string; args: string[] } {
  return { command: python, args: [scriptPath, '--run-dir', runDir, 'apply-feedback', '--feedback-file', feedbackFile] }
}

/** The changed-file block the agent must emit last. Returns relative paths. */
export function parseChangedFilesFromAgentOutput(output: string): string[] {
  const lines = output.split(/\r?\n/)
  const index = lines.findIndex((line) => line.trim().startsWith(CHANGED_FILES_MARKER))
  if (index < 0) return []
  const result: string[] = []
  for (const line of lines.slice(index + 1)) {
    const value = line.trim().replace(/^[-*]\s*/, '')
    if (value === '' || value.startsWith('`') || !/^[^\s:]+$/.test(value)) continue
    if (value.includes('*') || value.startsWith('#')) continue
    result.push(value.replace(/^\.\//, '').replace(/\\/g, '/'))
    if (result.length >= 200) break
  }
  return result
}

function fileHash(filePath: string): string {
  return createHash('sha256').update(readFileSync(filePath)).digest('hex')
}

function walkSkip(name: string): boolean {
  // Installation/dependency state (node_modules, .git) is never agent work.
  // Everything else — INCLUDING planner-owned manifest/lock files — is hashed
  // so a forbidden mutation is detected, never skipped (the controller is the
  // only writer of dependency state, but the detection must not trust that).
  const key = process.platform === 'win32' ? name.toLowerCase() : name
  return key === 'node_modules' || key === '.git'
}

/** True when any path segment is a planner-owned forbidden name. */
export function isForbiddenTrialRelative(relativePath: string): boolean {
  for (const segment of relativePath.split('/')) {
    if (ITERATIVE_FORBIDDEN_NAMES.has(process.platform === 'win32' ? segment.toLowerCase() : segment)) return true
  }
  return false
}

export type TrialMutation = { path: string; kind: TrialMutationKind; forbidden: boolean }

type CollectedTrialMutations = {
  baselineUnreadable: boolean
  mutations: TrialMutation[]
  unreadable: TrialMutation[]
  hashes: Record<string, string>
}

/** Do not follow links while measuring repair evidence. Reading a link target
 * could read outside the trial or turn a replaced directory into false changes. */
function scanTrialFiles(workspaceRoot: string): { hashes: Record<string, string>; unreadable: TrialMutation[] } {
  const hashes: Record<string, string> = Object.create(null)
  const unreadable: TrialMutation[] = []
  const stack = ['']
  const fail = (path: string) => unreadable.push({ path: path || '.', kind: 'modified', forbidden: isForbiddenTrialRelative(path) })
  while (stack.length) {
    const directory = stack.pop()!
    const absolute = join(workspaceRoot, directory)
    let entries: string[]
    try {
      const info = lstatSync(absolute)
      if (info.isSymbolicLink() || !info.isDirectory()) { fail(directory); continue }
      entries = readdirSync(absolute)
    } catch { fail(directory); continue }
    for (const name of entries) {
      if (walkSkip(name)) continue
      const path = directory ? directory + '/' + name : name
      try {
        const info = lstatSync(join(workspaceRoot, path))
        if (info.isSymbolicLink()) fail(path)
        else if (info.isDirectory()) stack.push(path)
        else if (info.isFile()) hashes[path] = fileHash(join(workspaceRoot, path))
        else fail(path)
      } catch { fail(path) }
    }
  }
  return { hashes, unreadable }
}

function validBaseline(value: unknown): value is Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  return Object.entries(value).every(([path, hash]) => {
    const parts = path.split('/')
    return path.length > 0 && !isAbsolute(path) && !path.includes('\\') && !path.includes(':')
      && parts.every(part => part !== '' && part !== '.' && part !== '..' && !walkSkip(part))
      && typeof hash === 'string' && /^[a-f0-9]{64}$/.test(hash)
  })
}

/** Validate durable evidence before paying for another agent invocation. */
export function assertTrialBaselineReadable(baselineFile: string): void {
  try {
    if (validBaseline(JSON.parse(readFileSync(baselineFile, 'utf8')))) return
  } catch { /* Missing and invalid evidence share the same explicit failure. */ }
  throw new Error('AGENT_BASELINE_UNREADABLE: original repair baseline is missing or invalid')
}

function collectTrialMutations(workspaceRoot: string, baselineFile: string): CollectedTrialMutations {
  let baseline: unknown
  try { baseline = JSON.parse(readFileSync(baselineFile, 'utf8')) } catch {
    return { baselineUnreadable: true, mutations: [], unreadable: [], hashes: {} }
  }
  if (!validBaseline(baseline)) return { baselineUnreadable: true, mutations: [], unreadable: [], hashes: {} }
  const { hashes, unreadable } = scanTrialFiles(workspaceRoot)
  const mutations: TrialMutation[] = []
  for (const [path, hash] of Object.entries(hashes)) {
    const previous = Object.hasOwn(baseline, path) ? baseline[path] : undefined
    if (previous !== hash) mutations.push({ path, kind: previous === undefined ? 'added' : 'modified', forbidden: isForbiddenTrialRelative(path) })
  }
  for (const path of Object.keys(baseline)) {
    if (Object.hasOwn(hashes, path)) continue
    if (unreadable.some(item => item.path === '.' || path === item.path || path.startsWith(item.path + '/'))) continue
    mutations.push({ path, kind: 'removed', forbidden: isForbiddenTrialRelative(path) })
  }
  return { baselineUnreadable: false, mutations, unreadable, hashes }
}

/** Backwards-compatible mutation list (see collectTrialMutations). Unreadable
 *  content and an unreadable baseline yield an empty list so callers that
 *  grant progress (repairProgressFingerprint) fail closed on that. */
export function classifyTrialMutations(workspaceRoot: string, baselineFile: string): TrialMutation[] {
  const observed = collectTrialMutations(workspaceRoot, baselineFile)
  return observed.baselineUnreadable || observed.unreadable.length ? [] : observed.mutations
}

/** Per-change verdict from ONE pass over the trial, combining the dependency
 *  guard with the provider runtime policy. Fail-closed fields let the caller
 *  refuse to trust an unmeasurable baseline. */
export type TrialChangeReport = {
  baselineUnreadable: boolean
  policyVersion: number
  runtimePaths: Set<string>
  changes: ClassifiedTrialChange[]
  unreadable: ClassifiedTrialChange[]
  changedFiles: string[]
  forbidden: Array<`${TrialMutationKind}:${string}`>
  progressFingerprint: string
}

/** Shared production boundary: incomplete filesystem evidence never reaches
 * feedback, bootstrap verification or a repair-budget extension. */
export function assertTrialChangesReadable(report: TrialChangeReport): void {
  if (report.baselineUnreadable) throw new Error('AGENT_BASELINE_UNREADABLE: original repair baseline is missing or invalid')
  if (report.unreadable.length) throw new Error('AGENT_TRIAL_UNREADABLE: ' + report.unreadable.map(item => item.path).sort().join(', '))
}

function classifyMutation(mutation: TrialMutation, runtimePaths: Set<string>): ClassifiedTrialChange {
  if (runtimePaths.has(mutation.path)) {
    return { path: mutation.path, kind: mutation.kind, category: 'confirmed-runtime', reason: 'provider runtime file confirmed by its narrow plugin shape' }
  }
  if (mutation.forbidden) {
    return { path: mutation.path, kind: mutation.kind, category: 'dependency-blocked', reason: 'planner-owned dependency file name (ITERATIVE_FORBIDDEN_NAMES)' }
  }
  return { path: mutation.path, kind: mutation.kind, category: 'source-config', reason: 'ordinary source/config change' }
}

/** Classify every trial mutation ONCE under the provider runtime policy. The
 *  policy is recomputed against CURRENT bytes so recognition is never stale;
 *  escaping project paths and unknown/strict providers exempt nothing. */
export function classifyTrialChanges(workspaceRoot: string, baselineFile: string, scope?: AgentRuntimeScope): TrialChangeReport {
  const policy = runtimePolicyFor(scope?.provider ?? 'unknown', { workspaceRoot, projectRelative: scope?.projectRelative ?? '.' })
  const runtimePaths = policy.runtimeRelativePaths()
  const collected = collectTrialMutations(workspaceRoot, baselineFile)
  const changes = collected.mutations.map(mutation => classifyMutation(mutation, runtimePaths))
  const unreadable = collected.unreadable.map(mutation => classifyMutation(mutation, runtimePaths))
  for (const item of unreadable) item.category = 'unreadable'
  const source = changes.filter(item => item.category === 'source-config').sort((a, b) => a.path.localeCompare(b.path))
  const complete = !collected.baselineUnreadable && unreadable.length === 0 && !changes.some(item => item.category === 'dependency-blocked')
  const progressFingerprint = complete && source.length
    ? createHash('sha256').update(JSON.stringify(source.map(item => [item.path, item.kind,
      item.kind === 'removed' ? '' : collected.hashes[item.path]]))).digest('hex') : ''
  return {
    baselineUnreadable: collected.baselineUnreadable,
    progressFingerprint,
    policyVersion: policy.version,
    runtimePaths,
    changes,
    unreadable,
    changedFiles: changes.filter(item => item.category === 'source-config').map(item => item.path).sort(),
    forbidden: changes.filter(item => item.category === 'dependency-blocked').map(item => `${item.kind}:${item.path}` as `${TrialMutationKind}:${string}`).sort(),
  }
}

/** Planner-owned files the agent changed or deleted. A non-empty result MUST
 * reject the repair before feedback/verification. */
/** Hash a completed repair once for both source evidence and guard violations.
 * The returned observation is consumed immediately, never cached across work. */
export function inspectTrialChanges(workspaceRoot: string, baselineFile: string, scope?: AgentRuntimeScope): TrialChangeReport {
  return classifyTrialChanges(workspaceRoot, baselineFile, scope)
}

export function forbiddenTrialViolations(workspaceRoot: string, baselineFile: string, scope?: AgentRuntimeScope): string[] {
  const report = classifyTrialChanges(workspaceRoot, baselineFile, scope)
  assertTrialChangesReadable(report)
  return report.forbidden
}

/** Durable baseline of the trial project: relative path -> sha256. Written
 * ONCE per agent run; a restart re-uses it, so the coordinator's own files
 * are never counted as agent changes. Covers planner-owned manifests too. */
export function writeTrialBaseline(workspaceRoot: string, baselineFile: string): number {
  const observed = scanTrialFiles(workspaceRoot)
  if (observed.unreadable.length) throw new Error('AGENT_TRIAL_UNREADABLE: ' + observed.unreadable.map(item => item.path).join(', '))
  mkdirSync(dirname(baselineFile), { recursive: true })
  const temporary = baselineFile + '.' + process.pid + '.tmp'
  writeFileSync(temporary, JSON.stringify(observed.hashes), 'utf8')
  renameSync(temporary, baselineFile)
  return Object.keys(observed.hashes).length
}

export function trialBaselineFile(runDir: string, identity?: Pick<IterativeAgentContext, 'runId' | 'candidateId' | 'baseCheckpointId'>): string {
  if (!identity) return join(runDir, 'trial', TRIAL_BASELINE_FILENAME)
  const key = createHash('sha256').update(JSON.stringify([identity.runId, identity.candidateId, identity.baseCheckpointId])).digest('hex')
  return join(runDir, 'trial', `agent-baseline-${key}.json`)
}

export function agentPromptFile(runDir: string): string {
  return join(runDir, 'trial', AGENT_PROMPT_FILENAME)
}

/** Real agent trial changes (modified + added + removed), EXCLUDING
 * planner-forbidden paths (those are violations, see
 * forbiddenTrialViolations) and installation noise. Paths are relative to
 * workspaceRoot, matching apply-feedback's scope check. */
export function changedFilesFromBaseline(workspaceRoot: string, baselineFile: string): string[] {
  const report = classifyTrialChanges(workspaceRoot, baselineFile)
  assertTrialChangesReadable(report)
  return report.changedFiles
}

/** Path of the project inside the trial, relative to the durable run dir. */
export function trialProjectPath(workspaceRoot: string, projectRelative: string): string {
  return join(workspaceRoot, projectRelative)
}

// R5: the agent answers with a TYPED structured outcome. Its text is never
// proof; only the kind marker (validated against the Python FEEDBACK_KINDS)
// plus a zero process exit and real file changes may claim READY_FOR_VERIFY.
export const ITERATIVE_FEEDBACK_KINDS = new Set<string>([
  'REPAIRING',
  'READY_FOR_VERIFY',
  'NEEDS_COHORT_EXPANSION',
  'NEEDS_ALTERNATIVE',
  'INCONCLUSIVE',
  'INFRA_BLOCKED',
])

export type AgentParsedOutcome = {
  kind: IterativeFeedbackKind
  reason: string
  proposals: string[]
  changedFiles: string[]
}

const KIND_RE = /FEEDBACK_KIND\s*:\s*([A-Z_]+)/i
const REASON_RE = /^\s*(?:#{1,6}\s+)?(?:REASON|ПРИЧИНА)\s*:\s*([^\r\n]+)$/im
const PROPOSALS_RE = /^\s*(?:#{1,6}\s+)?(?:PROPOSALS|ПРЕДЛОЖЕНИЯ|ALTERNATIVES|КОМПАНЬОНЫ)\s*:/im

export function parseAgentOutcome(output: string, exitCode: number, changedFiles: string[]): AgentParsedOutcome {
  // OpenCode streams JSON events: parse the final assistant text, never tool
  // input/output containing a quoted prompt or a competing feedback marker.
  output = output.split(/\r?\n/).flatMap(line => {
    try {
      const event = JSON.parse(line)
      if (event.type === 'text' && typeof event.part?.text === 'string') return [event.part.text]
      if (event.type === 'result' && typeof event.result === 'string') return [event.result]
      if (event.type === 'item.completed' && event.item?.type === 'agent_message' && typeof event.item.text === 'string') return [event.item.text]
      return []
    } catch { return [line] }
  }).join('\n')
  const kindMatch = [...output.matchAll(new RegExp(KIND_RE.source, 'gi'))].at(-1)
  const kindRaw = kindMatch ? kindMatch[1].toUpperCase() : ''
  let kind: IterativeFeedbackKind = kindRaw && ITERATIVE_FEEDBACK_KINDS.has(kindRaw)
    ? (kindRaw as IterativeFeedbackKind)
    : 'READY_FOR_VERIFY'
  const reasonMatch = output.match(REASON_RE)
  const reason = reasonMatch ? reasonMatch[1].trim().slice(0, 600) : ''
  const proposals: string[] = []
  const headerMatch = output.match(PROPOSALS_RE)
  if (headerMatch) {
    const start = (headerMatch.index ?? 0) + headerMatch[0].length
    for (const line of output.slice(start).split(/\r?\n/)) {
      const value = line.trim().replace(/^[-*\d.)\s]+/, '')
      if (value === '' || value.startsWith('```')) continue
      if (/^(?:#{1,6}\s+)?(CHANGED_FILES|FEEDBACK_KIND|REASON|ПРИЧИНА)\s*:/i.test(value)) break
      proposals.push(value.slice(0, 200))
      if (proposals.length >= 10) break
    }
  }
  // A completed repair must be backed by a zero exit of the agent process AND
  // real file changes; an errored or no-op run cannot claim READY_FOR_VERIFY.
  if (kind === 'READY_FOR_VERIFY' && (exitCode !== 0 || changedFiles.length === 0)) {
    kind = 'INCONCLUSIVE'
  }
  const files = kind === 'READY_FOR_VERIFY' ? [...new Set(changedFiles)].sort() : []
  return { kind, reason, proposals, changedFiles: files }
}

// R6: a durable dispatch lease for the repair agent. Persisted BEFORE the
// agent is awaited (session/provider/database/attempt), it lets a restarted
// Desktop recognize that the previous attempt is still in-flight and refuse a
// SECOND repair session instead of silently spending another attempt.
// The in-memory per-project guard only protects one process lifetime; the
// lease spans app restarts while the repair (or its verification) runs.
/** Provider failures are infrastructure, never repair feedback or proof. */
export function agentProviderFailure(output: string, exitCode: number): string | undefined {
  const providerError = agentLaunchProviderError(output)
  if (providerError) {
    const event = JSON.parse(providerError)
    const error = event.error ?? event
    return String(error.data?.message ?? error.message ?? error.name ?? 'Provider error').slice(0, 2000)
  }
  // A model error quoted in assistant/tool JSON is ordinary project evidence.
  const plain = output.split(/\r?\n/).filter(line => {
    try { JSON.parse(line); return false } catch { return true }
  }).join('\n')
  if (/ProviderModelNotFoundError|Model not found:/i.test(plain)) return plain.trim().slice(-2000)
  if (exitCode !== 0 && output.trim()) return output.trim().slice(-2000)
  return undefined
}

/** Observability only: a completed agent dispatch never grants project PASS. */
export function recordAgentDispatchTiming(runDir: string, context: { runId: string; candidateId: string; attemptId: number }, startedAt: number, status: string, finishedAt = Date.now()): void {
  if (!Number.isFinite(startedAt) || !Number.isFinite(finishedAt) || finishedAt < startedAt) return
  try {
    const file = join(runDir, 'cohort-agent-telemetry.jsonl')
    mkdirSync(runDir, { recursive: true })
    if (existsSync(file) && statSync(file).size > 8 * 1024 * 1024) {
      const archive = `${file}.1`
      if (existsSync(archive)) unlinkSync(archive)
      renameSync(file, archive)
    }
    appendFileSync(file, `${JSON.stringify({ ...context, stage: 'agent', outcome: 'UNKNOWN', status, durationSeconds: (finishedAt - startedAt) / 1000, timingScope: 'dispatch including provider wait and feedback; no project verification authority', finishedAt })}\n`, 'utf8')
  } catch { /* Telemetry never turns a completed repair into failure. */ }
}

export const AGENT_LEASE_FILENAME = 'agent-lease.json'

export type AgentLease = {
  schemaVersion: 1
  sessionId: string
  provider: string
  databasePath: string
  runId: string
  candidateId: string
  attemptId: number
  pid: number
  childPid?: number
  autopilot?: boolean
  startedAt: string
  // Launch-wait resilience: a retryable provider outage (rate limit / temp /
  // unknown with budget left) parks the lease as WAITING with its retry
  // parameters instead of failing the repair. A restarted Desktop reads the
  // same lease and either continues waiting or retries exactly once the wait
  // is over — never a second concurrent agent and never a re-spent attempt.
  waiting?: boolean
  waitKind?: 'rate-limited' | 'temporary' | 'unknown'
  waitDetail?: string
  retryAt?: number
  retryAfterSeconds?: number
  launchAttempts?: number
  launchDiagnostics?: AgentLaunchDiagnostics
  repairBudget?: { windows: number; deadlineAt: number; fingerprint: string; paused?: boolean; waitRemainingMs?: number }
}

/** Every exit, including a parked wait and exhausted budget, releases the
 * project lock. The callback runs synchronously until its first await. */
export async function withAgentDispatchLock<T>(inFlight: Set<string>, key: string, execute: () => Promise<T>): Promise<T | { ok: false; error: string }> {
  if (inFlight.has(key)) return { ok: false, error: 'STEP_IN_PROGRESS' }
  inFlight.add(key)
  try { return await execute() } finally { inFlight.delete(key) }
}

export function agentLeaseFile(runDir: string): string {
  return join(runDir, 'trial', AGENT_LEASE_FILENAME)
}

export function validRepairBudget(value: unknown): value is NonNullable<AgentLease['repairBudget']> {
  const b = value as NonNullable<AgentLease['repairBudget']> | undefined
  return Boolean(b && Number.isInteger(b.windows) && b.windows >= 1 && b.windows <= 3
    && Number.isFinite(b.deadlineAt) && typeof b.fingerprint === 'string'
    && (b.waitRemainingMs === undefined || (Number.isFinite(b.waitRemainingMs) && b.waitRemainingMs >= 0 && b.waitRemainingMs <= 15 * 60_000)))
}

/** Hash actual allowed source mutations; provider prose and repeated heartbeats
 * cannot extend a repair. Protected mutations retain their ordinary guard. */
export function repairProgressFingerprint(root: string, baseline: string, scope?: AgentRuntimeScope): string {
  return classifyTrialChanges(root, baseline, scope).progressFingerprint
}

export function extendRepairBudget(budget: NonNullable<AgentLease['repairBudget']>, fingerprint: string,
  now = Date.now(), windowMs = 15 * 60_000): NonNullable<AgentLease['repairBudget']> | undefined {
  if (budget.windows >= 3 || !fingerprint || fingerprint === budget.fingerprint) return undefined
  return { windows: budget.windows + 1, deadlineAt: now + windowMs, fingerprint }
}

export const REPAIR_DEADLINE_EXPIRED = 'AGENT_REPAIR_DEADLINE_EXPIRED: время работы агента исчерпано. Правки trial и проверенный checkpoint сохранены. Повторный запуск начнёт новую сессию ремонта.'

/** Provider waits suspend the unused active window, never award a new one. */
export function parkRepairBudget(budget: NonNullable<AgentLease['repairBudget']>, now = Date.now()): NonNullable<AgentLease['repairBudget']> {
  const waitRemainingMs = Math.max(0, budget.deadlineAt - now)
  if (waitRemainingMs === 0) throw new Error(REPAIR_DEADLINE_EXPIRED)
  return { ...budget, waitRemainingMs }
}

export function resumeRepairBudget(saved: AgentLease['repairBudget'], now = Date.now()): NonNullable<AgentLease['repairBudget']> {
  if (!saved) return { windows: 1, deadlineAt: now + 15 * 60_000, fingerprint: '' }
  if (saved.paused) {
    if (saved.windows >= 3) throw new Error('AGENT_REPAIR_BUDGET_EXHAUSTED: три окна ремонта использованы')
    return { windows: saved.windows + 1, deadlineAt: now + 15 * 60_000, fingerprint: saved.fingerprint }
  }
  const { waitRemainingMs, ...budget } = saved
  if (waitRemainingMs !== undefined) budget.deadlineAt = now + waitRemainingMs
  if (budget.deadlineAt <= now) throw new Error(REPAIR_DEADLINE_EXPIRED)
  return budget
}

export function writeAgentLease(file: string, lease: AgentLease): void {
  mkdirSync(dirname(file), { recursive: true })
  const temporary = `${file}.${process.pid}.tmp`
  writeFileSync(temporary, JSON.stringify(lease), 'utf8')
  renameSync(temporary, file)
}

export function readAgentLease(file: string): AgentLease | undefined {
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>
    if (!parsed || typeof parsed !== 'object') return undefined
    if (Number(parsed.schemaVersion) !== 1) return undefined
    if (typeof parsed.sessionId !== 'string' || typeof parsed.startedAt !== 'string') return undefined
    const waitKind = parsed.waitKind === 'rate-limited' || parsed.waitKind === 'temporary' || parsed.waitKind === 'unknown' ? parsed.waitKind : undefined
    return {
      schemaVersion: 1,
      sessionId: parsed.sessionId,
      provider: String(parsed.provider ?? ''),
      databasePath: String(parsed.databasePath ?? ''),
      runId: String(parsed.runId ?? ''),
      candidateId: String(parsed.candidateId ?? ''),
      attemptId: Number(parsed.attemptId ?? 0),
      pid: Number(parsed.pid ?? 0),
      ...(typeof parsed.childPid === 'number' ? { childPid: parsed.childPid } : {}),
      ...(parsed.autopilot === true ? { autopilot: true } : {}),
      startedAt: parsed.startedAt,
      ...(validRepairBudget(parsed.repairBudget) ? { repairBudget: parsed.repairBudget } : {}),
      ...(parsed.waiting === true ? { waiting: true } : {}),
      ...(waitKind ? { waitKind } : {}),
      ...(typeof parsed.waitDetail === 'string' && parsed.waitDetail ? { waitDetail: parsed.waitDetail } : {}),
      ...(typeof parsed.retryAt === 'number' ? { retryAt: parsed.retryAt } : {}),
      ...(typeof parsed.retryAfterSeconds === 'number' ? { retryAfterSeconds: parsed.retryAfterSeconds } : {}),
      ...(typeof parsed.launchAttempts === 'number' && Number.isInteger(parsed.launchAttempts) && parsed.launchAttempts > 0 ? { launchAttempts: parsed.launchAttempts } : {}),
      ...(parsed.launchDiagnostics && typeof parsed.launchDiagnostics === 'object' ? { launchDiagnostics: parsed.launchDiagnostics as AgentLaunchDiagnostics } : {}),
    }
  } catch {
    return undefined
  }
}

export function agentLeaseAlive(lease: AgentLease | undefined, now = Date.now(), maxAgeMs = 2 * 60 * 60 * 1000): boolean {
  if (!lease) return false
  const started = Date.parse(lease.startedAt)
  if (Number.isNaN(started)) return false
  return now - started < maxAgeMs
}

export function clearAgentLease(file: string): void {
  try {
    unlinkSync(file)
  } catch {
    // best-effort cleanup; a stale lease simply expires via agentLeaseAlive
  }
}

// Repeated-blocker durability: a persisted record that makes the SAME
// forbidden-mutation defect (same provider, same policy version, same base
// checkpoint, same violation profile) fatal for a NEW candidate instead of
// spending another attempt. The record lives in the run dir, survives agent
// restarts, and lists the candidateIds it has already rejected.
export const AGENT_BLOCKER_FILENAME = 'agent-blocker.json'

export function agentBlockerFile(runDir: string): string {
  return join(runDir, AGENT_BLOCKER_FILENAME)
}

/** Canonical violation shape: case-insensitive operation, forward slashes,
 * no leading "./"; path case is folded only on Windows. */
export function normalizeViolationPath(value: string): string {
  const normalized = value.trim().replace(/\\/g, '/').replace(/^(?:\.\/)+/, '')
  const operation = /^(added|modified|removed):(.*)$/i.exec(normalized)
  const path = (operation?.[2] ?? normalized).replace(/^(?:\.\/)+/, '')
  return (operation ? operation[1].toLowerCase() + ':' : '') + (process.platform === 'win32' ? path.toLowerCase() : path)
}

export type RepeatedBlockerRecord = {
  schemaVersion: 1
  provider: string
  policyVersion: number
  reasonCode: string
  baseCheckpointId: string
  fingerprint: string
  violations: string[]
  candidateIds: string[]
}

/** Sign one blocking outcome. The candidate id is intentionally EXCLUDED (it
 *  is the thing being matched), and baseCheckpointId is INCLUDED so a repair
 *  against a newer baseline is a fresh defect, not a repeat. */
export function repeatedBlockerFingerprint(context: {
  provider: string
  policyVersion: number
  reasonCode: string
  baseCheckpointId: string
  violations: string[]
}): string {
  const normalized = [...new Set(context.violations.map(normalizeViolationPath))].sort()
  return createHash('sha256').update(JSON.stringify([context.provider, context.policyVersion, context.reasonCode, context.baseCheckpointId, normalized])).digest('hex')
}

export function readAgentBlocker(file: string): RepeatedBlockerRecord | undefined {
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8')) as unknown
    if (!parsed || typeof parsed !== 'object') return undefined
    const record = parsed as Partial<RepeatedBlockerRecord>
    if (record.schemaVersion !== 1 || typeof record.provider !== 'string' || typeof record.fingerprint !== 'string') return undefined
    if (!Number.isInteger(record.policyVersion) || typeof record.reasonCode !== 'string' || typeof record.baseCheckpointId !== 'string') return undefined
    if (!Array.isArray(record.violations) || !record.violations.every(value => typeof value === 'string')
      || !Array.isArray(record.candidateIds) || !record.candidateIds.every(value => typeof value === 'string')) return undefined
    const valid = record as RepeatedBlockerRecord
    if (valid.fingerprint !== repeatedBlockerFingerprint(valid)) return undefined
    return valid
  } catch {
    return undefined
  }
}

export function writeAgentBlocker(file: string, record: RepeatedBlockerRecord): void {
  mkdirSync(dirname(file), { recursive: true })
  const temporary = `${file}.${process.pid}.tmp`
  writeFileSync(temporary, JSON.stringify(record), 'utf8')
  renameSync(temporary, file)
}

/** Durable per-candidate blocking. Persists the blocking signature on its FIRST
 *  occurrence and rejects later candidates that repeat it. A different provider,
 *  policy version or baseline is a different defect and must not match. */
export function resolveRepeatedBlocker(file: string, current: {
  provider: string
  policyVersion: number
  reasonCode: string
  baseCheckpointId: string
  candidateId: string
  violations: string[]
}): { repeated: boolean; stored: RepeatedBlockerRecord | undefined } {
  const fingerprint = repeatedBlockerFingerprint(current)
  const existing = readAgentBlocker(file)
  if (existing && existing.fingerprint === fingerprint) {
    if (!existing.candidateIds.includes(current.candidateId)) {
      writeAgentBlocker(file, { ...existing, candidateIds: [...existing.candidateIds, current.candidateId] })
    }
    return { repeated: true, stored: existing }
  }
  const stored: RepeatedBlockerRecord = {
    schemaVersion: 1,
    provider: current.provider,
    policyVersion: current.policyVersion,
    reasonCode: current.reasonCode,
    baseCheckpointId: current.baseCheckpointId,
    fingerprint,
    violations: [...new Set(current.violations.map(normalizeViolationPath))].sort(),
    candidateIds: [current.candidateId],
  }
  writeAgentBlocker(file, stored)
  return { repeated: false, stored }
}

/** Refuse a known, still-present blocker before launching another paid agent.
 * A repaired trial, new policy/provider/base or different violation can retry. */
export function assertNoPersistedAgentBlocker(file: string, workspaceRoot: string, baseline: string,
  scope: AgentRuntimeScope, baseCheckpointId: string): void {
  const stored = readAgentBlocker(file)
  if (!stored || stored.provider !== scope.provider || stored.baseCheckpointId !== baseCheckpointId) return
  const report = classifyTrialChanges(workspaceRoot, baseline, scope)
  assertTrialChangesReadable(report)
  if (!report.forbidden.length) return
  const fingerprint = repeatedBlockerFingerprint({ provider: scope.provider, policyVersion: report.policyVersion,
    reasonCode: 'FORBIDDEN_MUTATION', baseCheckpointId, violations: report.forbidden })
  if (stored.fingerprint === fingerprint) throw new Error('AGENT_REPEATED_BLOCKER: FORBIDDEN_MUTATION: ' + report.forbidden.join(', '))
}

/** True when the durable lease currently describes a parked launch-wait
 *  (retryable provider outage waiting for its retry time). */
export function isWaitingAgentLease(lease: AgentLease | undefined): boolean {
  return Boolean(lease?.waiting)
}

/** Cancel a parked launch-wait: clears the lease and returns whether a wait
 *  was actually active. A running agent lease is left untouched — cancelling a
 *  wait is NOT cancelling a live agent process. Pure enough for the contract
 *  check (no timers are managed here). */
export function cancelWaitingAgentLease(file: string): boolean {
  const lease = readAgentLease(file)
  if (!isWaitingAgentLease(lease)) return false
  clearAgentLease(file)
  return true
}

// #5: what a fresh `flow:iterative:agent` dispatch must do about a durable
// lease left on disk:
//   - none: the lease is absent or older than the expiry window -> fresh run.
//   - in-progress: the lease is fresh AND its issuing Desktop process is still
//     alive (a second window / a live run) -> refuse without spending attempts.
//   - resume: the lease is fresh but the issuing Desktop was killed or crashed
//     mid-run -> resume the SAME provider session (same attempt, durable trial
//     edits), never a second attempt for the same repair.
//   - wait: the lease is a parked launch-wait whose retry time has NOT arrived
//     -> keep waiting; no attempt is consumed and nothing is spawned.
//   - retry: the lease is a parked launch-wait whose retry time has arrived ->
//     retry the launch FRESH (no provider session exists yet) carrying the
//     durable launchAttempts so the retry budget is honored across restarts.
//   - give-up: the lease is a parked launch-wait whose finite retry budget is
//     exhausted -> end with a clear diagnostic, no further retries.
// A waiting lease holder keeps its own process pid, so a LIVE owner is NOT
// refused as in-progress while a wait is parked: the wait itself is the
// single-owner guard until its retry time or the cancel.
// The owner-liveness probe is injected so the decision is testable without OS
// process-table races.
export type AgentLeaseDecision =
  | { action: 'none' }
  | { action: 'in-progress' }
  | { action: 'resume'; sessionId: string }
  | { action: 'wait'; retryAt: number; detail: string }
  | { action: 'retry'; launchAttempts: number; detail: string; sessionId?: string }
  | { action: 'give-up'; detail: string }

/** A parked launch-wait is retained far longer than a live-agent lease: a
 *  long provider reset (up to AGENT_LAUNCH_MAX_RETRY_AFTER_MS) must not be
 *  silently downgraded to a fresh dispatch because 2 h elapsed. */
export function decideAgentLeaseDispatch(
  lease: AgentLease | undefined,
  isOwnerAlive: (pid: number) => boolean,
  now = Date.now(),
): AgentLeaseDecision {
  if (!lease) return { action: 'none' }
  // A child surviving Desktop must not expire into a duplicate paid repair.
  if (!lease.waiting && lease.childPid && isOwnerAlive(lease.childPid)) return { action: 'in-progress' }
  if (!lease.waiting && !lease.repairBudget && !agentLeaseAlive(lease, now)) return { action: 'none' }
  if (lease.repairBudget?.paused) {
    if (lease.repairBudget.windows >= 3) return { action: 'give-up', detail: 'AGENT_REPAIR_BUDGET_EXHAUSTED: три окна ремонта использованы. Принятый результат и правки trial сохранены; нужен другой вариант обновления.' }
    return lease.sessionId ? { action: 'resume', sessionId: lease.sessionId } : { action: 'retry', launchAttempts: lease.launchAttempts ?? 0, detail: 'Continue preserved trial repair' }
  }
  if (lease.waiting) {
    // Legacy waits have no suspended remainder: never relaunch an expired window.
    const budget = lease.repairBudget
    if (budget && (budget.waitRemainingMs !== undefined ? budget.waitRemainingMs <= 0 : budget.deadlineAt <= now)) {
      return { action: 'give-up', detail: REPAIR_DEADLINE_EXPIRED }
    }
    const attempts = lease.launchAttempts ?? 0
    if (lease.waitKind !== 'rate-limited' && !(lease.autopilot && lease.waitKind === 'temporary')) {
      const budget = lease.waitKind === 'unknown' ? 3 : lease.waitKind === 'temporary' ? 5 : 0
      if (budget > 0 && attempts >= budget) {
        return { action: 'give-up', detail: `AGENT_LAUNCH_BUDGET_EXHAUSTED: провайдер недоступен (${lease.waitKind}) после ${attempts} попыток запуска: ${lease.waitDetail ?? 'причина неизвестна'}. Верификация и последний verified checkpoint сохранились; ремонт не засчитан.` }
      }
      if (budget === 0) {
        return { action: 'give-up', detail: `AGENT_LAUNCH_FAILED: ${lease.waitDetail ?? 'неизвестная причина'}. Проверьте настройки провайдера и повторите ремонт.` }
      }
    }
    if ((lease.retryAt ?? 0) > now) {
      return { action: 'wait', retryAt: lease.retryAt ?? now, detail: lease.waitDetail ?? '' }
    }
    return { action: 'retry', launchAttempts: attempts, detail: lease.waitDetail ?? '', ...(lease.sessionId ? { sessionId: lease.sessionId } : {}) }
  }
  if ((lease.pid > 0 && isOwnerAlive(lease.pid)) || (lease.childPid && isOwnerAlive(lease.childPid))) return { action: 'in-progress' }
  return lease.sessionId ? { action: 'resume', sessionId: lease.sessionId } : { action: 'retry', launchAttempts: lease.launchAttempts ?? 0, detail: 'Agent stopped before publishing a session ID' }
}
