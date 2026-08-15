import { existsSync, readFileSync } from 'node:fs'
import { migrationPlanFromPrompt, migrationScopeManifestFromPrompt } from './migration-progress.js'

export type PlannerStatus = 'refresh-plan' | 'expand-plan' | 'defer-blockers' | 'approval-required' | 'blocked'
export type PlannerResult = { status: PlannerStatus; reason: string; executorGuidance?: string; proposedScopeAdditions?: string[]; deferPackages?: string[] }
type ScopeRow = {
  key: string
  package: string
  current: string
  target: string
  action: string
  shouldUpdate: boolean
  scopeExcluded: boolean
  compatibilityCohort: string
  compatibilityNote: string
}
type ScopeAction = ScopeRow & { shouldUpdate: true }
export type PromptRevisionAssessment = { safe: boolean; changed: boolean; additions: string[]; removals: string[]; autoAdditions: string[]; reason: string }
function comparableVersion(value: string): number[] | undefined {
  const match = /(?:^|[^0-9])(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?/.exec(value)
  return match ? [Number(match[1]), Number(match[2]), Number(match[3]), match[4] ? -1 : 0] : undefined
}

function compareTargets(left: string, right: string): number | undefined {
  const a = comparableVersion(left)
  const b = comparableVersion(right)
  if (!a || !b) return left === right ? 0 : undefined
  for (let index = 0; index < a.length; index += 1) if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1
  return 0
}

function manifestRows(markdown: string, projectName: string): ScopeRow[] {
  const manifest = migrationScopeManifestFromPrompt(markdown)
  const columns = Array.isArray(manifest?.columns) ? manifest.columns.filter((value): value is string => typeof value === 'string') : undefined
  if (!manifest || !Array.isArray(manifest.rows)) return []
  return manifest.rows.flatMap((value): ScopeRow[] => {
    const raw = Array.isArray(value) && columns ? Object.fromEntries(columns.map((column, index) => [column, value[index]])) : value
    if (!raw || typeof raw !== 'object') return []
    const row = raw as Record<string, unknown>
    if (String(row.project ?? '') !== projectName) return []
    const packageName = String(row.package ?? row.name ?? '')
    const section = String(row.section ?? '')
    if (!packageName || !section) return []
    return [{
      key: `${section}:${packageName}`,
      package: packageName,
      current: String(row.current ?? ''),
      target: String(row.target ?? ''),
      action: String(row.action ?? (row.shouldUpdate === true ? 'update' : 'deferred')),
      shouldUpdate: row.shouldUpdate === true,
      scopeExcluded: row.scopeExcluded === true || row.action === 'excluded',
      compatibilityCohort: String(row.compatibilityCohort ?? ''),
      compatibilityNote: String(row.compatibilityNote ?? ''),
    }]
  })
}

export type PlannerDeferralPartition = { apply: string[]; ignored: string[]; rejected: string[] }

export function partitionPlannerDeferrals(
  markdown: string,
  projectName: string,
  executablePackages: ReadonlySet<string>,
  values: readonly string[],
): PlannerDeferralPartition {
  const rows = manifestRows(markdown, projectName)
  const byPackage = new Map<string, ScopeRow[]>()
  for (const row of rows) byPackage.set(row.package, [...(byPackage.get(row.package) ?? []), row])
  const result: PlannerDeferralPartition = { apply: [], ignored: [], rejected: [] }

  for (const raw of [...new Set(values.map((value) => value.trim()).filter(Boolean))]) {
    let packageName = raw
    if (!byPackage.has(packageName)) {
      const separator = raw.lastIndexOf('@')
      const suffix = separator > 0 ? raw.slice(separator + 1) : ''
      const candidate = separator > 0 && /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(suffix) ? raw.slice(0, separator) : ''
      if (candidate && byPackage.has(candidate)) packageName = candidate
    }
    const matches = byPackage.get(packageName) ?? []
    if (executablePackages.has(packageName)) {
      result.apply.push(packageName)
    } else if (matches.length && matches.every((row) => !row.shouldUpdate)) {
      // Version-qualified echoes of already deferred/excluded direct rows do
      // not widen or weaken executable scope: they are a safe no-op.
      result.ignored.push(packageName)
    } else {
      result.rejected.push(raw)
    }
  }
  result.apply = [...new Set(result.apply)]
  result.ignored = [...new Set(result.ignored)]
  return result
}
function manifestActions(markdown: string, projectName: string): ScopeAction[] {
  return manifestRows(markdown, projectName).filter((row): row is ScopeAction => row.shouldUpdate)
}

export function residualStabilityTargets(markdown: string, projectName: string): Record<string, string> {
  const grouped = new Map<string, string[]>()
  for (const row of manifestActions(markdown, projectName)) {
    if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(row.target)) continue
    grouped.set(row.package, [...(grouped.get(row.package) ?? []), row.target])
  }
  const result: Record<string, string> = {}
  for (const [packageName, targets] of grouped) {
    const unique = [...new Set(targets)]
    if (unique.length === 1) result[packageName] = unique[0]
  }
  return result
}

function isDeterministicAutoAddition(previous: ScopeRow | undefined, next: ScopeRow): boolean {
  if (!previous || previous.scopeExcluded || next.scopeExcluded || !next.shouldUpdate) return false
  if (previous.shouldUpdate) return false
  // Revisions are compared only after the deterministic Dashboard generator has
  // rebuilt the roadmap.  Re-activating an already-present direct row is not an
  // LLM scope expansion: it is residual work selected by the generator from the
  // same health universe.  Explicit exclusions remain untouchable and the
  // target must be a comparable non-downgrade.  Magic markers still cover
  // peer-closure/supervisor additions, but ordinary residual rows no longer
  // create giant MIGRATION_PLAN_APPROVAL_REQUIRED banners after a replan.
  const direction = compareTargets(next.target, next.current)
  const existingDirectResidual = next.action === 'update' && direction !== undefined && direction >= 0
  return existingDirectResidual || /\b(?:AUTO_PEER_CLOSURE|AUTO_RESIDUAL_PLAN|SUPERVISOR_SCOPE_EXPANSION)\b/.test(next.compatibilityNote)
}

export type SupervisorScopeAddition = { package: string; target: string }

export function validateSupervisorScopeAdditions(markdown: string, projectName: string, values: readonly string[]): { accepted: SupervisorScopeAddition[]; rejected: string[] } {
  const rows = manifestRows(markdown, projectName)
  const byPackage = new Map<string, ScopeRow[]>()
  for (const row of rows) byPackage.set(row.package, [...(byPackage.get(row.package) ?? []), row])
  const accepted: SupervisorScopeAddition[] = []
  const rejected: string[] = []
  for (const raw of [...new Set(values.map((value) => value.trim()).filter(Boolean))]) {
    const separator = raw.lastIndexOf('@')
    const packageName = separator > 0 ? raw.slice(0, separator) : ''
    const target = separator > 0 ? raw.slice(separator + 1) : ''
    const matches = byPackage.get(packageName) ?? []
    if (!packageName || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(target)) {
      rejected.push(`${raw}: ожидается existing-direct-package@exact-semver`)
    } else if (matches.length !== 1) {
      rejected.push(`${raw}: package отсутствует в direct manifest или неоднозначен по section`)
    } else if (matches[0].scopeExcluded) {
      rejected.push(`${raw}: explicit exclusion нельзя обходить автоматически`)
    } else if (compareTargets(target, matches[0].current) === undefined || (compareTargets(target, matches[0].current) ?? 0) < 0) {
      rejected.push(`${raw}: target несравним с current ${matches[0].current} или является downgrade`)
    } else {
      accepted.push({ package: packageName, target })
    }
  }
  return { accepted, rejected }
}

function branchByPackage(markdown: string, projectName: string): Map<string, string> {
  const plan = migrationPlanFromPrompt(markdown, projectName)
  const result = new Map<string, string>()
  for (const branch of plan?.branches ?? []) for (const packageName of branch.packages) result.set(packageName, branch.scopeBranch ?? branch.branch)
  return result
}

export function assessPromptRevision(previousMarkdown: string, nextMarkdown: string, projectName: string): PromptRevisionAssessment {
  const previousPlan = migrationPlanFromPrompt(previousMarkdown, projectName)
  const nextPlan = migrationPlanFromPrompt(nextMarkdown, projectName)
  const empty = (reason: string): PromptRevisionAssessment => ({ safe: false, changed: false, additions: [], removals: [], autoAdditions: [], reason })
  if (!previousPlan || !nextPlan) return empty('старый или новый prompt не содержит читаемый Branch plan')
  if (previousPlan.baseBranch !== nextPlan.baseBranch || previousPlan.mergedBranch !== nextPlan.mergedBranch) {
    return { safe: false, changed: true, additions: [], removals: [], autoAdditions: [], reason: `replan меняет Git-контур: ${previousPlan.baseBranch}→${nextPlan.baseBranch}, ${previousPlan.mergedBranch}→${nextPlan.mergedBranch}` }
  }

  const previousRows = new Map(manifestRows(previousMarkdown, projectName).map((row) => [row.key, row]))
  const nextRows = new Map(manifestRows(nextMarkdown, projectName).map((row) => [row.key, row]))
  const previous = new Map(manifestActions(previousMarkdown, projectName).map((row) => [row.key, row]))
  const next = new Map(manifestActions(nextMarkdown, projectName).map((row) => [row.key, row]))
  if (!previousRows.size || !nextRows.size) return empty('scope manifest не содержит строк проекта')

  const additions: string[] = []
  const removals: string[] = []
  const autoAdditions: string[] = []
  const unsafeAdditions: string[] = []
  for (const [key, row] of next) {
    const old = previous.get(key)
    if (!old) {
      const label = `${key}@${row.target}`
      additions.push(label)
      if (isDeterministicAutoAddition(previousRows.get(key), row)) autoAdditions.push(label)
      else unsafeAdditions.push(label)
    } else if (old.action !== row.action) {
      const label = `${key}@${row.target} (было ${old.action}@${old.target})`
      additions.push(label)
      unsafeAdditions.push(label)
    } else if (old.target !== row.target) {
      const direction = compareTargets(row.target, old.target)
      if (direction !== undefined && direction < 0) removals.push(`${key}@${row.target} (было ${old.target})`)
      else {
        const label = `${key}@${row.target} (было ${old.target})`
        additions.push(label)
        unsafeAdditions.push(label)
      }
    }
  }
  for (const [key, row] of previous) if (!next.has(key)) removals.push(`${key}@${row.target}`)

  // Existing actionable packages may not silently move to another work branch.
  // A deterministic companion may join an existing branch, which is exactly
  // what AUTO_PEER_CLOSURE generates; the source package itself stays put.
  const previousBranches = branchByPackage(previousMarkdown, projectName)
  const nextBranches = branchByPackage(nextMarkdown, projectName)
  const movedExisting = [...previous.values()]
    .filter((row) => next.has(row.key))
    .filter((row) => previousBranches.get(row.package) !== nextBranches.get(row.package))
    .map((row) => `${row.package}: ${previousBranches.get(row.package) ?? '—'}→${nextBranches.get(row.package) ?? '—'}`)
  if (movedExisting.length) unsafeAdditions.push(...movedExisting.map((value) => `branch:${value}`))

  const changed = additions.length > 0 || removals.length > 0 || movedExisting.length > 0
  const safe = changed && unsafeAdditions.length === 0
  return {
    safe,
    changed,
    additions,
    removals,
    autoAdditions,
    reason: unsafeAdditions.length
      ? `новый план содержит ${unsafeAdditions.length} недоказанное расширение/перенос scope`
      : autoAdditions.length
        ? `план автоматически расширен на ${autoAdditions.length} детерминированно доказанных direct peer companion и/или сужен на ${removals.length} blocker target`
        : removals.length
          ? `план безопасно сужен на ${removals.length} несовместимых direct target`
          : 'пересчёт вернул тот же immutable scope',
  }
}

export function buildPlannerPrompt(input: { projectName: string; projectPath: string; failure: string; savedPromptPath: string; resultPath: string }): string {
  return `# Independent migration planner

Ты — отдельная planner-сессия. Executor уже доказал, что текущий immutable scope не проходит verification.

Проект: ${input.projectName}
Project path: ${input.projectPath}
Saved immutable prompt: ${input.savedPromptPath}

## Failure evidence

${input.failure}

## Полномочия и запреты

- Можно читать проект, package manifests/lockfiles, сохранённый prompt, checkpoints и roadmap artifacts.
- Можно исследовать совместимость пакетов и registry metadata, запускать только read-only проверки.
- Нельзя менять файлы проекта (кроме обязательного result JSON), Git index, ветки, commits, tags или remotes.
- Нельзя исправлять product code и нельзя обходить FLOW/verification.
- Оркестратор независимо проверит, что branch, HEAD и полный git status не изменились.
- Ты управляешь стратегией миграции. Если для достижения цели нужны дополнительные уже существующие direct dependencies проекта, верни expand-plan и перечисли их как exact package@version в proposedScopeAdditions. Оркестратор независимо проверит, что package уже direct, не excluded, target существует в registry, а затем обычные verification gates подтвердят результат.
- Не проси подтверждение только потому, что нужен existing direct companion или безопасная перегруппировка. Используй expand-plan; deterministic generator пометит SUPERVISOR_SCOPE_EXPANSION и заново построит cohort/Branch plan.
- Approval нужен только для действительно новой direct dependency, обхода explicit exclusion, понижения цели/требований, отключения проверки или product-level/architecture refactor.
- Если failure содержит TARGET_PLAN_INSUFFICIENT, deferral не приближает целевой процент: используй expand-plan и предложи достаточно совместимых existing-direct targets, чтобы закрыть указанный дефицит. Только доказанное отсутствие такого набора разрешает approval-required/blocked.
- Если локальный blocker нельзя закрыть без product-level approval, перечисли только package names текущих actionable target в deferPackages (без @current-version и без уже deferred/excluded строк), чтобы сохранить независимый остаточный прогресс. Пакет остаётся в health/blocker отчёте и НЕ становится excluded.
- Предпочитай defer-blockers перед blocked/approval-required, если существует безопасный остаточный план без этих targets. blocked допустим только когда даже после локального deferral нельзя получить воспроизводимое/зелёное состояние.
- Статус blocked вместе с утверждением о существующем safe residual plan является невалидным machine result: в таком случае обязан вернуть defer-blockers и точный deferPackages.

## Machine result

Запиши ровно один JSON в ${input.resultPath}:

{
  "status": "refresh-plan | expand-plan | defer-blockers | approval-required | blocked",
  "reason": "краткая доказанная причина",
  "executorGuidance": "необязательная подсказка следующей executor-сессии",
  "proposedScopeAdditions": ["existing-direct-package@exact-semver; обязательно для expand-plan"],
  "deferPackages": ["необязательные package names текущего executable scope, которые надо временно отложить, чтобы продолжить остальные targets"]
}

Используй refresh-plan, когда deterministic roadmap generator с текущими manifests/state должен безопасно пересчитать cohort/scope. Human prose не является управляющим сигналом.
`
}

export function readPlannerResult(path: string): PlannerResult | undefined {
  if (!existsSync(path)) return undefined
  try {
    const raw = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
    if (!['refresh-plan', 'expand-plan', 'defer-blockers', 'approval-required', 'blocked'].includes(String(raw.status))) return undefined
    const reason = typeof raw.reason === 'string' ? raw.reason.trim() : ''
    if (!reason) return undefined
    const proposedScopeAdditions = Array.isArray(raw.proposedScopeAdditions) ? raw.proposedScopeAdditions.filter((value): value is string => typeof value === 'string' && Boolean(value.trim())) : undefined
    const deferPackages = Array.isArray(raw.deferPackages) ? raw.deferPackages.filter((value): value is string => typeof value === 'string' && Boolean(value.trim())) : undefined
    return {
      status: raw.status as PlannerStatus,
      reason,
      ...(typeof raw.executorGuidance === 'string' && raw.executorGuidance.trim() ? { executorGuidance: raw.executorGuidance.trim() } : {}),
      ...(proposedScopeAdditions?.length ? { proposedScopeAdditions } : {}),
      ...(deferPackages?.length ? { deferPackages } : {}),
    }
  } catch {
    return undefined
  }
}
