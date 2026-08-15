export type ClosureTarget = 'yellow' | 'green'

// One dependency that currently fails its own lag policy, as computed by the
// generator (which owns the version comparison and the live policy boundary).
// `plannedTarget` empty means the current plan has no action for it, i.e. this
// package cannot be fixed by running the agent again -- the distinction that
// decides whether a blocked goal needs more migration work or a scope decision.
export type LagBlocker = {
  package: string
  kind?: string
  group?: number
  current?: string
  required?: string
  lagPolicyMonths?: number
  plannedTarget?: string
  note?: string
}

export type TargetClosure = {
  reached: boolean
  target: ClosureTarget
  current: 'red' | 'yellow' | 'green' | 'unknown'
  lagOkPct?: number
  lagOk?: number
  total?: number
  remainingPackages: string[]
  lagBlockers: LagBlocker[]
  neededForYellow?: number
  plannedLagFixes?: number
  maxLagOkPctAfterPlan?: number
  neededBeyondCurrentPlan?: number
  planCanReachYellow?: boolean
  criticalPackages?: string[]
  uncoveredCriticalPackages?: string[]
  critical?: number
  high?: number
  excluded?: number
  bestEffortReleaseEligible?: boolean
  bestEffortReason?: string
}

const STATUS_RANK = { red: 0, yellow: 1, green: 2 } as const
const NO_ACTION = new Set(['', '-', '—', 'нет действия', 'ничего не делать'])

function semverParts(value: unknown): number[] | undefined {
  const match = /(?:^|[^0-9])(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?/.exec(String(value ?? ''))
  return match ? [Number(match[1]), Number(match[2]), Number(match[3]), match[4] ? -1 : 0] : undefined
}

function targetCoversMinimum(target: unknown, minimum: unknown): boolean {
  const left = semverParts(target)
  const right = semverParts(minimum)
  if (!left || !right) return false
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) return left[index] > right[index]
  }
  return true
}

export function targetClosureFromRoadmap(value: unknown, project: string, target: ClosureTarget): TargetClosure {
  const roadmap = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const healthByProject = roadmap.project_health && typeof roadmap.project_health === 'object' ? roadmap.project_health as Record<string, unknown> : {}
  const rawHealth = healthByProject[project] && typeof healthByProject[project] === 'object' ? healthByProject[project] as Record<string, unknown> : {}
  const status = rawHealth.status === 'red' || rawHealth.status === 'yellow' || rawHealth.status === 'green' ? rawHealth.status : 'unknown'
  const projects = roadmap.projects && typeof roadmap.projects === 'object' ? roadmap.projects as Record<string, unknown> : {}
  const rows = Array.isArray(projects[project]) ? projects[project] as unknown[] : []
  const targetField = `target_${target}`
  const remainingPackages = rows.flatMap((raw): string[] => {
    if (!raw || typeof raw !== 'object') return []
    const row = raw as Record<string, unknown>
    if (row.scope_excluded === true) return []
    const candidate = String(row[targetField] ?? '').trim().toLowerCase()
    const name = String(row.name ?? '').trim()
    return name && !NO_ACTION.has(candidate) ? [name] : []
  })
  const lagBlockers = (Array.isArray(rawHealth.lag_blockers) ? rawHealth.lag_blockers : []).flatMap((raw): LagBlocker[] => {
    if (!raw || typeof raw !== 'object') return []
    const blocker = raw as Record<string, unknown>
    const name = String(blocker.package ?? '').trim()
    if (!name) return []
    // Which target matters depends on the goal being pursued, not on the
    // generator's default mode: a row the yellow planner has an action for is
    // closable by re-running the agent even when default mode skipped it.
    const modeField = target === 'green' ? 'plannedTargetGreen' : 'plannedTargetYellow'
    const plannedTarget = [blocker[modeField], blocker.plannedTarget]
      .map((raw) => (typeof raw === 'string' ? raw.trim() : ''))
      .find(Boolean) ?? ''
    return [{
      package: name,
      ...(typeof blocker.kind === 'string' ? { kind: blocker.kind } : {}),
      ...(typeof blocker.group === 'number' ? { group: blocker.group } : {}),
      ...(typeof blocker.current === 'string' ? { current: blocker.current } : {}),
      ...(typeof blocker.required === 'string' ? { required: blocker.required } : {}),
      ...(typeof blocker.lagPolicyMonths === 'number' ? { lagPolicyMonths: blocker.lagPolicyMonths } : {}),
      ...(plannedTarget ? { plannedTarget } : {}),
      ...(typeof blocker.note === 'string' && blocker.note ? { note: blocker.note } : {}),
    }]
  })
  const reached = status !== 'unknown' && STATUS_RANK[status] >= STATUS_RANK[target] && remainingPackages.length === 0
  const fixableBlockers = lagBlockers.filter((blocker) => Boolean(blocker.plannedTarget))
  const plannedLagFixes = new Set(fixableBlockers
    .filter((blocker) => !blocker.required || targetCoversMinimum(blocker.plannedTarget, blocker.required))
    .map((blocker) => blocker.package)).size
  const criticalRows = rows.flatMap((raw): Array<{ package: string; covered: boolean }> => {
    if (!raw || typeof raw !== 'object') return []
    const row = raw as Record<string, unknown>
    if (row.scope_excluded === true || row.action === 'excluded') return []
    const criticalMatch = /(?:^|[,\s])C:(\d+)/.exec(String(row.current_vulns ?? row.currentVulns ?? ''))
    if (!criticalMatch || Number(criticalMatch[1]) <= 0) return []
    const packageName = String(row.name ?? row.package ?? '').trim()
    const planned = row[targetField]
    const minimum = row.min_no_critical ?? row.minNoCritical
    return packageName ? [{ package: packageName, covered: targetCoversMinimum(planned, minimum) }] : []
  })
  const lagOk = typeof rawHealth.lag_ok_12m === 'number' ? rawHealth.lag_ok_12m : undefined
  const total = typeof rawHealth.total === 'number' ? rawHealth.total : undefined
  const yellowThreshold = total !== undefined ? Math.ceil(total * 0.8) : undefined
  const projectedLagOk = target === 'yellow' && typeof rawHealth.yellow_projected_lag_ok === 'number'
    ? rawHealth.yellow_projected_lag_ok
    : undefined
  const maxLagOkAfterPlan = projectedLagOk !== undefined && total !== undefined
    ? Math.min(total, projectedLagOk)
    : lagOk !== undefined && total !== undefined ? Math.min(total, lagOk + plannedLagFixes) : undefined
  const maxLagOkPctAfterPlan = maxLagOkAfterPlan !== undefined && total ? (maxLagOkAfterPlan * 100) / total : undefined
  const neededBeyondCurrentPlan = yellowThreshold !== undefined && maxLagOkAfterPlan !== undefined ? Math.max(0, yellowThreshold - maxLagOkAfterPlan) : undefined
  const critical = typeof rawHealth.critical === 'number' ? rawHealth.critical : undefined
  const criticalPackages = criticalRows.map((row) => row.package)
  const uncoveredCriticalPackages = criticalRows.filter((row) => !row.covered).map((row) => row.package)
  if ((critical ?? 0) > 0 && !criticalRows.length) uncoveredCriticalPackages.push('неизвестный Critical source')
  const planCanReachYellow = neededBeyondCurrentPlan !== undefined
    ? neededBeyondCurrentPlan === 0 && uncoveredCriticalPackages.length === 0
    : undefined
  // Best-effort release is not a verifier bypass.  It is allowed only when the
  // *current executable plan is exhausted* (nothing left for the executor to
  // do), the roadmap is trustworthy enough to classify the level, and there
  // are no Critical vulnerabilities.  Build/type/lint/repository hooks still
  // run normally during release.
  const bestEffortReleaseEligible = !reached
    && status !== 'unknown'
    && remainingPackages.length === 0
    && fixableBlockers.length === 0
    && critical !== undefined
    && critical === 0
  const bestEffortReason = bestEffortReleaseEligible
    ? `текущий автоматический план исчерпан: actionable targets=0, fixable blockers=0, Critical=0; целевой health-level пока ${status}`
    : undefined
  return {
    reached,
    target,
    current: status,
    ...(typeof rawHealth.lag_ok_pct === 'number' ? { lagOkPct: rawHealth.lag_ok_pct } : {}),
    ...(typeof rawHealth.lag_ok_12m === 'number' ? { lagOk: rawHealth.lag_ok_12m } : {}),
    ...(typeof rawHealth.total === 'number' ? { total: rawHealth.total } : {}),
    ...(typeof rawHealth.lag_needed_for_yellow === 'number' ? { neededForYellow: rawHealth.lag_needed_for_yellow } : {}),
    ...(plannedLagFixes ? { plannedLagFixes } : {}),
    ...(maxLagOkPctAfterPlan !== undefined ? { maxLagOkPctAfterPlan } : {}),
    ...(neededBeyondCurrentPlan !== undefined ? { neededBeyondCurrentPlan, planCanReachYellow } : {}),
    ...(criticalPackages.length ? { criticalPackages } : {}),
    ...(uncoveredCriticalPackages.length ? { uncoveredCriticalPackages } : {}),
    ...(typeof rawHealth.critical === 'number' ? { critical: rawHealth.critical } : {}),
    ...(typeof rawHealth.high === 'number' ? { high: rawHealth.high } : {}),
    ...(typeof rawHealth.excluded === 'number' ? { excluded: rawHealth.excluded } : {}),
    ...(bestEffortReleaseEligible ? { bestEffortReleaseEligible, bestEffortReason } : {}),
    remainingPackages,
    lagBlockers,
  }
}

export function targetClosureFromRoadmapWithTargets(
  value: unknown,
  project: string,
  target: ClosureTarget,
  plannedTargets: Readonly<Record<string, string>>,
): TargetClosure {
  const entries = Object.entries(plannedTargets).filter(([, version]) => Boolean(semverParts(version)))
  if (!entries.length || !value || typeof value !== 'object') return targetClosureFromRoadmap(value, project, target)
  const roadmap = value as Record<string, unknown>
  const projects = roadmap.projects && typeof roadmap.projects === 'object' ? roadmap.projects as Record<string, unknown> : {}
  const sourceRows = Array.isArray(projects[project]) ? projects[project] as unknown[] : []
  const targets = new Map(entries)
  const targetField = `target_${target}`
  const rows = sourceRows.map((raw) => {
    if (!raw || typeof raw !== 'object') return raw
    const row = raw as Record<string, unknown>
    const packageName = String(row.name ?? row.package ?? '')
    const plannedTarget = targets.get(packageName)
    return plannedTarget ? { ...row, [targetField]: plannedTarget } : row
  })
  const healthByProject = roadmap.project_health && typeof roadmap.project_health === 'object' ? roadmap.project_health as Record<string, unknown> : {}
  const rawHealth = healthByProject[project] && typeof healthByProject[project] === 'object' ? healthByProject[project] as Record<string, unknown> : {}
  const health = { ...rawHealth }
  if (Array.isArray(rawHealth.lag_blockers)) {
    const modeField = target === 'green' ? 'plannedTargetGreen' : 'plannedTargetYellow'
    health.lag_blockers = rawHealth.lag_blockers.map((raw) => {
      if (!raw || typeof raw !== 'object') return raw
      const blocker = raw as Record<string, unknown>
      const plannedTarget = targets.get(String(blocker.package ?? ''))
      return plannedTarget ? { ...blocker, [modeField]: plannedTarget } : blocker
    })
  }
  if (target === 'yellow') delete health.yellow_projected_lag_ok
  return targetClosureFromRoadmap({
    ...roadmap,
    projects: { ...projects, [project]: rows },
    project_health: { ...healthByProject, [project]: health },
  }, project, target)
}

// Splits blockers by the only distinction that changes what the user should
// do next: a package the current plan still has an action for is fixed by
// running the agent again; a package with no target cannot be, and needs
// either a scope decision (exclude it, with a reason) or its own separate
// upgrade task.
export type ScopeExpansionCoverage = { required: number; covered: number; missingPackages: string[] }

export function scopeExpansionCoverage(
  closure: TargetClosure,
  additions: readonly { package: string; target: string }[],
): ScopeExpansionCoverage {
  const required = closure.neededBeyondCurrentPlan ?? 0
  const additionsByPackage = new Map(additions.map((item) => [item.package, item.target]))
  const uncovered = closure.lagBlockers.filter((blocker) => !blocker.plannedTarget)
  const coveredPackages = new Set(uncovered
    .filter((blocker) => targetCoversMinimum(additionsByPackage.get(blocker.package), blocker.required))
    .map((blocker) => blocker.package))
  return {
    required,
    covered: coveredPackages.size,
    missingPackages: uncovered.filter((blocker) => !coveredPackages.has(blocker.package)).map((blocker) => blocker.package),
  }
}
export function splitLagBlockers(closure: TargetClosure): { fixable: LagBlocker[]; stuck: LagBlocker[] } {
  return {
    fixable: closure.lagBlockers.filter((blocker) => Boolean(blocker.plannedTarget)),
    stuck: closure.lagBlockers.filter((blocker) => !blocker.plannedTarget),
  }
}

export function shouldUseSupervisorSeed(closure: TargetClosure | undefined, target: ClosureTarget, hasCurrentPlan: boolean): boolean {
  return Boolean(hasCurrentPlan && closure && !closure.reached && (target === 'green' || closure.planCanReachYellow === false))
}

export function targetClosureMessage(closure: TargetClosure): string {
  const labels = { red: 'Красный', yellow: 'Жёлтый', green: 'Зелёный', unknown: 'не рассчитан' } as const
  const targetLabel = closure.target === 'yellow' ? 'Жёлтый' : 'Зелёный'
  const percent = typeof closure.lagOkPct === 'number' ? ` · ${closure.lagOkPct.toFixed(1)}%` : ''
  const fraction = typeof closure.lagOk === 'number' && typeof closure.total === 'number' ? ` (${closure.lagOk} из ${closure.total})` : ''
  const threshold = closure.target === 'yellow' ? ' Для жёлтого требуется не менее 80% и отсутствие Critical.' : ' Для зелёного должны быть выполнены все критерии зелёного уровня.'
  const { fixable, stuck } = splitLagBlockers(closure)
  const short = (list: LagBlocker[], limit = 6) => `${list.slice(0, limit).map((blocker) => blocker.package).join(', ')}${list.length > limit ? `, ещё ${list.length - limit}` : ''}`
  // The whole point of this message is that the user should not have to guess
  // which packages the percentage is about, or how much more work is left.
  const need = typeof closure.neededForYellow === 'number' && closure.neededForYellow > 0
    ? ` До порога не хватает ${closure.neededForYellow} ${closure.neededForYellow === 1 ? 'совместимой зависимости' : 'совместимых зависимостей'}.`
    : ''
  const lagCapacity = closure.target === 'yellow' && (closure.neededBeyondCurrentPlan ?? 0) > 0
    ? ` Даже если все действия текущего плана успешны, получится максимум ${closure.maxLagOkPctAfterPlan?.toFixed(1) ?? '?'}%; за пределами плана всё ещё не хватает ${closure.neededBeyondCurrentPlan ?? '?'} совместимых зависимостей.`
    : ''
  const criticalCapacity = closure.uncoveredCriticalPackages?.length
    ? ` Текущий план не устраняет Critical: ${closure.uncoveredCriticalPackages.join(', ')}.`
    : ''
  const fixableText = fixable.length
    ? ` Ещё можно закрыть текущим планом (${fixable.length}): ${short(fixable)}.`
    : ''
  const stuckText = stuck.length
    ? ` Не закрываются этим планом (${stuck.length}, нет target — нужна отдельная задача либо осознанное исключение с причиной): ${short(stuck)}.`
    : ''
  const remaining = closure.remainingPackages.length
    ? ` В согласованном плане осталось действий: ${closure.remainingPackages.length} (${closure.remainingPackages.slice(0, 8).join(', ')}${closure.remainingPackages.length > 8 ? ', …' : ''}).`
    : ''
  // A roadmap built before lag_blockers existed has no blocker detail at all;
  // in that case pending planned actions are the only signal available, so the
  // advice must not claim the plan is exhausted when it simply wasn't measured.
  const planCanStillHelp = fixable.length > 0 || (closure.lagBlockers.length === 0 && closure.remainingPackages.length > 0)
  const next = planCanStillHelp
    ? ' Дальше: выгрузите свежий prompt из Dashboard и прогоните миграцию по оставшимся действиям.'
    : ' Дальше: закрыть цель текущим планом нельзя — исключите объективно заблокированные пакеты с причиной или заведите отдельную задачу на их обновление, затем пересоберите отчёт.'
  return `Цель «${targetLabel}» не достигнута: ${labels[closure.current]}${percent}${fraction}.${threshold}${need}${lagCapacity}${criticalCapacity}${fixableText}${stuckText}${remaining}${next}`
}
