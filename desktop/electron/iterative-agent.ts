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
import { createHash } from 'node:crypto'
import { agentLaunchProviderError } from './agent-launch-errors.js'
import { existsSync, mkdirSync, readdirSync, readFileSync, realpathSync, lstatSync, statSync, unlinkSync, writeFileSync, renameSync, appendFileSync } from 'node:fs'
import { dirname, join, resolve, sep } from 'node:path'
import type { AgentLaunchDiagnostics } from './agent-launch-errors.js'

export type IterativeRepairRequest = {
  requestId: string
  reason: string
  failingCommands: Array<{ command: string; exitCode: number }>
  diagnosticsTail: string
}

export type IterativeAgentContext = {
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
  return name === 'node_modules' || name === '.git'
}

/** True when any path segment is a planner-owned forbidden name. */
export function isForbiddenTrialRelative(relativePath: string): boolean {
  for (const segment of relativePath.split('/')) {
    if (ITERATIVE_FORBIDDEN_NAMES.has(segment)) return true
  }
  return false
}

export type AgentRuntimeScope = { provider: string; projectRelative: string }

/** Recognize only OpenCode's own plugin installation, never project manifests.
 * The explicit provider/project scope, physical paths and plugin-only shape
 * are all required. Runtime files remain in source verification, but cannot
 * count as source repair evidence or be sent to Python as changed manifests. */
export function openCodeRuntimeManifestPaths(workspaceRoot: string, scope?: AgentRuntimeScope): Set<string> {
  const result = new Set<string>()
  if (scope?.provider !== 'opencode') return result
  const project = resolve(workspaceRoot, scope.projectRelative)
  const root = resolve(workspaceRoot)
  if (project !== root && !project.startsWith(root + sep)) return result
  const directory = join(project, '.opencode')
  try {
    if (realpathSync(directory).toLowerCase() !== directory.toLowerCase()) return result
    const manifestPath = join(directory, 'package.json')
    const lockPath = join(directory, 'package-lock.json')
    if (!lstatSync(manifestPath).isFile() || !lstatSync(lockPath).isFile()) return result
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
    const deps = manifest.dependencies
    if (Object.keys(manifest).some(key => key !== 'dependencies')) return result
    if (!deps || Object.keys(deps).length !== 1 || typeof deps['@opencode-ai/plugin'] !== 'string') return result
    const version = deps['@opencode-ai/plugin']
    if (!/^\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(version)) return result
    const lock = JSON.parse(readFileSync(lockPath, 'utf8'))
    const lockedRoot = lock.packages?.['']
    if (lock.name !== '.opencode' || lock.lockfileVersion !== 3 || !lockedRoot) return result
    if (Object.keys(lockedRoot).some(key => key !== 'dependencies')) return result
    if (JSON.stringify(lockedRoot.dependencies) !== JSON.stringify(deps)) return result
    const plugin = lock.packages?.['node_modules/@opencode-ai/plugin']
    if (plugin?.version !== version) return result
    const prefix = scope.projectRelative.replace(/\\/g, '/').replace(/^\.\/?$/, '').replace(/\/$/, '')
    for (const file of ['package.json', 'package-lock.json']) {
      result.add(`${prefix ? prefix + '/' : ''}.opencode/${file}`)
    }
  } catch {
    // Missing, linked or unrecognized runtime files keep the ordinary guard.
  }
  return result
}

export type TrialMutationKind = 'modified' | 'added' | 'removed'
export type TrialMutation = { path: string; kind: TrialMutationKind; forbidden: boolean }

/** Classify every significant trial mutation against the durable baseline:
 * modified / added / removed, each flagged as planner-forbidden when the path
 * name matches ITERATIVE_FORBIDDEN_NAMES. The agent text is never the source
 * of truth — this hash diff is. */
export function classifyTrialMutations(workspaceRoot: string, baselineFile: string): TrialMutation[] {
  let baseline: Record<string, string>
  try {
    baseline = JSON.parse(readFileSync(baselineFile, 'utf8')) as Record<string, string>
  } catch {
    return []
  }
  const mutations: TrialMutation[] = []
  const seen = new Set<string>()
  for (const [relativePath, expected] of Object.entries(baseline)) {
    const key = relativePath.replace(/\\/g, '/')
    seen.add(key)
    const absolute = join(workspaceRoot, relativePath.split('/').join(sep))
    let actual: string | undefined
    try {
      actual = fileHash(absolute)
    } catch {
      actual = undefined
    }
    if (actual === undefined && !existsSync(absolute)) {
      mutations.push({ path: key, kind: 'removed', forbidden: isForbiddenTrialRelative(key) })
    } else if (actual !== undefined && actual !== expected) {
      mutations.push({ path: key, kind: 'modified', forbidden: isForbiddenTrialRelative(key) })
    }
  }
  const stack: string[] = ['']
  while (stack.length > 0) {
    const relativeDir = stack.pop()!
    const absoluteDir = join(workspaceRoot, relativeDir)
    let names: string[]
    try {
      names = readdirSync(absoluteDir)
    } catch {
      continue
    }
    for (const name of names) {
      if (walkSkip(name)) continue
      const absolute = join(absoluteDir, name)
      let stats
      try {
        stats = statSync(absolute)
      } catch {
        continue
      }
      const relativePath = relativeDir ? `${relativeDir}${sep}${name}` : name
      const key = relativePath.replace(/\\/g, '/')
      if (stats.isDirectory()) {
        stack.push(relativePath)
      } else if (stats.isFile() && !seen.has(key)) {
        mutations.push({ path: key, kind: 'added', forbidden: isForbiddenTrialRelative(key) })
      }
    }
  }
  return mutations
}

/** Planner-owned files the agent changed or deleted. A non-empty result MUST
 * reject the repair before feedback/verification. */
export function forbiddenTrialViolations(workspaceRoot: string, baselineFile: string, scope?: AgentRuntimeScope): string[] {
  const runtimePaths = openCodeRuntimeManifestPaths(workspaceRoot, scope)
  return classifyTrialMutations(workspaceRoot, baselineFile)
    .filter((mutation) => mutation.forbidden && !runtimePaths.has(mutation.path))
    .map((mutation) => `${mutation.kind}:${mutation.path}`)
    .sort()
}

/** Durable baseline of the trial project: relative path -> sha256. Written
 * ONCE per agent run; a restart re-uses it, so the coordinator's own files
 * are never counted as agent changes. Covers planner-owned manifests too. */
export function writeTrialBaseline(workspaceRoot: string, baselineFile: string): number {
  const entries: Record<string, string> = {}
  const stack = ['']
  while (stack.length > 0) {
    const relativeDir = stack.pop()!
    const absoluteDir = join(workspaceRoot, relativeDir)
    let names: string[]
    try {
      names = readdirSync(absoluteDir)
    } catch {
      continue
    }
    for (const name of names) {
      if (walkSkip(name)) continue
      const absolute = join(absoluteDir, name)
      let stats
      try {
        stats = statSync(absolute)
      } catch {
        continue
      }
      const relativePath = relativeDir ? `${relativeDir}${sep}${name}` : name
      if (stats.isDirectory()) {
        stack.push(relativePath)
      } else if (stats.isFile()) {
        entries[relativePath.replace(/\\/g, '/')] = fileHash(absolute)
      }
    }
  }
  mkdirSync(dirname(baselineFile), { recursive: true })
  writeFileSync(baselineFile, JSON.stringify(entries), 'utf8')
  return Object.keys(entries).length
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
  return classifyTrialMutations(workspaceRoot, baselineFile)
    .filter((mutation) => !mutation.forbidden)
    .map((mutation) => mutation.path)
    .sort()
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
  if (!lease.waiting && !agentLeaseAlive(lease, now)) return { action: 'none' }
  if (lease.waiting) {
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
