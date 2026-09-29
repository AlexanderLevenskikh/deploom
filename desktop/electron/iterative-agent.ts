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
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, sep } from 'node:path'

export type IterativeRepairRequest = {
  requestId: string
  reason: string
  failingCommands: Array<{ command: string; exitCode: number }>
  diagnosticsTail: string
}

export type IterativeAgentContext = {
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
    `Заверши ответ строкой "${CHANGED_FILES_MARKER}" и перечисли по одному изменённому файлу на строку (путь относительно корня trial).`,
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
    `Заверши ответ строкой "${CHANGED_FILES_MARKER}" и перечисли по одному изменённому файлу на строку (путь относительно корня trial).`,
  ].join('\n')
}

/** Build the typed feedback the Python apply-feedback accepts. Identities are
 * taken ONLY from the durable candidate context, never from agent text. */
export function buildFeedbackPayload(
  ctx: Pick<IterativeAgentContext, 'runId' | 'candidateId' | 'baseCheckpointId' | 'attemptId'>,
  changedFiles: string[],
  kind: IterativeFeedbackKind,
  reason: string,
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
    proposedScope: {},
    proposedConstraints: {},
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

function skipEntry(name: string): boolean {
  return name === 'node_modules' || name === '.git' || ITERATIVE_FORBIDDEN_NAMES.has(name)
}

/** Durable baseline of the trial project: relative path -> sha256. Written
 * ONCE per agent run; a restart re-uses it, so the coordinator's own files
 * are never counted as agent changes. */
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
      if (skipEntry(name)) continue
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

export function trialBaselineFile(runDir: string): string {
  return join(runDir, 'trial', TRIAL_BASELINE_FILENAME)
}

export function agentPromptFile(runDir: string): string {
  return join(runDir, 'trial', AGENT_PROMPT_FILENAME)
}

/** Changed files = baseline entries whose content moved. New files are
 * reported too; removed files are ignored (deleting a source file is not a
 * repair). Paths are relative to workspaceRoot, matching apply-feedback's
 * scope check. */
export function changedFilesFromBaseline(workspaceRoot: string, baselineFile: string): string[] {
  let baseline: Record<string, string>
  try {
    baseline = JSON.parse(readFileSync(baselineFile, 'utf8')) as Record<string, string>
  } catch {
    return []
  }
  const changed: string[] = []
  for (const [relativePath, expected] of Object.entries(baseline)) {
    const absolute = join(workspaceRoot, relativePath.split('/').join(sep))
    if (!existsSync(absolute)) continue
    let actual
    try {
      actual = fileHash(absolute)
    } catch {
      continue
    }
    if (actual !== expected) changed.push(relativePath)
  }
  // Newly added files (not in the baseline) are also agent changes.
  const baselinePaths = new Set(Object.keys(baseline))
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
      if (skipEntry(name)) continue
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
      } else if (stats.isFile() && !baselinePaths.has(key)) {
        changed.push(key)
      }
    }
  }
  return [...new Set(changed)].sort()
}

/** Path of the project inside the trial, relative to the durable run dir. */
export function trialProjectPath(workspaceRoot: string, projectRelative: string): string {
  return join(workspaceRoot, projectRelative)
}
