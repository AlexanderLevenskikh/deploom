import { AlertTriangle, Search, ShieldCheck, X } from 'lucide-react'
import { useDeferredValue, useEffect, useMemo, useState } from 'react'
import { useLanguage } from '../i18n'
import type { BaselineControlMode, BaselineDecision, BaselineIntent, BaselineIntentPlan, BaselinePackagePolicy, BaselineProofMode, BaselineSearchMode } from '../types'
import { QuickSelect } from './QuickSelect'

type Props = {
  mode: 'prepare' | 'decision'
  plan: BaselineIntentPlan
  decision?: BaselineDecision
  onCancel: () => void
  onSubmit: (intent: BaselineIntent) => Promise<void>
}

type DeferredCohort = NonNullable<BaselineIntent['deferredCohorts']>[number]

const DECISION_TRANCHE = 8

function normalizedControlMode(intent: BaselineIntent): BaselineControlMode {
  if (intent.controlMode === 'AUTONOMOUS' || intent.controlMode === 'CONFIRM_SIGNIFICANT') return intent.controlMode
  return intent.executionMode === 'BACKGROUND' ? 'AUTONOMOUS' : 'CONFIRM_SIGNIFICANT'
}

function boundedInteger(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(min, Math.min(max, Math.round(parsed)))
}

function normalizedSearchMode(value: BaselineIntent['searchMode']): BaselineSearchMode {
  return value === 'EXHAUSTIVE' ? 'EXHAUSTIVE' : 'AUTO'
}

function policyFingerprint(policies: Record<string, BaselinePackagePolicy>): string {
  return Object.entries(policies)
    .filter(([, policy]) => policy !== 'auto')
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, policy]) => `${name}\u0000${policy}`)
    .join('\u0001')
}

function cohortFingerprint(cohorts: DeferredCohort[]): string {
  return [...cohorts]
    .map((cohort) => `${cohort.id}\u0000${[...cohort.packages].sort().join('\u0001')}`)
    .sort()
    .join('\u0002')
}

function reconcileDeferredCohorts(cohorts: DeferredCohort[], policies: Record<string, BaselinePackagePolicy>): DeferredCohort[] {
  return cohorts
    .map((cohort) => ({ ...cohort, packages: cohort.packages.filter((name) => (policies[name] ?? 'auto') === 'keep-current') }))
    .filter((cohort) => cohort.packages.length > 0)
}

export function BaselineIntentDialog({ mode, plan, decision, onCancel, onSubmit }: Props) {
  const { text } = useLanguage()
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<'all' | 'runtime' | 'dev' | 'peer'>('all')
  const [policies, setPolicies] = useState<Record<string, BaselinePackagePolicy>>({ ...plan.intent.policies })
  const [controlMode, setControlMode] = useState<BaselineControlMode>(normalizedControlMode(plan.intent))
  const [budgetMinutes, setBudgetMinutes] = useState(boundedInteger(plan.intent.budgetMinutes, 30, 5, 240))
  const [maxKnownCritical, setMaxKnownCritical] = useState(boundedInteger(plan.intent.acceptancePolicy?.maxKnownCritical, 0, 0, 99))
  const [maxKnownHigh, setMaxKnownHigh] = useState(boundedInteger(plan.intent.acceptancePolicy?.maxKnownHigh, 1, 0, 99))
  const [searchDepth, setSearchDepth] = useState<BaselineSearchMode>(normalizedSearchMode(plan.intent.searchMode))
  const [deferredCohorts, setDeferredCohorts] = useState<DeferredCohort[]>([...(plan.intent.deferredCohorts ?? [])])
  const [busy, setBusy] = useState(false)
  const deferredQuery = useDeferredValue(query)

  useEffect(() => {
    setPolicies({ ...plan.intent.policies })
    setControlMode(normalizedControlMode(plan.intent))
    setBudgetMinutes(boundedInteger(plan.intent.budgetMinutes, 30, 5, 240))
    setMaxKnownCritical(boundedInteger(plan.intent.acceptancePolicy?.maxKnownCritical, 0, 0, 99))
    setMaxKnownHigh(boundedInteger(plan.intent.acceptancePolicy?.maxKnownHigh, 1, 0, 99))
    setSearchDepth(normalizedSearchMode(plan.intent.searchMode))
    setDeferredCohorts([...(plan.intent.deferredCohorts ?? [])])
  }, [plan])

  const visible = useMemo(() => {
    const needle = deferredQuery.trim().toLowerCase()
    return plan.candidates.filter((item) => {
      if (kind !== 'all' && item.kind !== kind) return false
      return !needle || item.name.toLowerCase().includes(needle)
    })
  }, [deferredQuery, kind, plan.candidates])

  const counts = useMemo(() => {
    const result = { auto: 0, 'keep-current': 0, required: 0 }
    for (const item of plan.candidates) result[policies[item.name] ?? 'auto'] += 1
    return result
  }, [plan.candidates, policies])

  const dirty = useMemo(
    () => policyFingerprint(policies) !== policyFingerprint(plan.intent.policies)
      || controlMode !== normalizedControlMode(plan.intent)
      || budgetMinutes !== boundedInteger(plan.intent.budgetMinutes, 30, 5, 240)
      || maxKnownCritical !== boundedInteger(plan.intent.acceptancePolicy?.maxKnownCritical, 0, 0, 99)
      || maxKnownHigh !== boundedInteger(plan.intent.acceptancePolicy?.maxKnownHigh, 1, 0, 99)
      || searchDepth !== normalizedSearchMode(plan.intent.searchMode)
      || cohortFingerprint(deferredCohorts) !== cohortFingerprint(plan.intent.deferredCohorts ?? []),
    [budgetMinutes, controlMode, deferredCohorts, maxKnownCritical, maxKnownHigh, plan.intent, policies, searchDepth],
  )

  const buildIntent = ({
    extra = 0,
    grant = 0,
    nextPolicies = policies,
    searchMode = searchDepth as BaselineIntent['searchMode'],
    nextControlMode = controlMode,
    proofMode = 'VERIFIED' as BaselineProofMode,
    nextDeferredCohorts = deferredCohorts,
    cohortAction,
  }: {
    extra?: number
    grant?: number
    nextPolicies?: Record<string, BaselinePackagePolicy>
    searchMode?: BaselineIntent['searchMode']
    nextControlMode?: BaselineControlMode
    proofMode?: BaselineProofMode
    nextDeferredCohorts?: DeferredCohort[]
    cohortAction?: BaselineIntent['cohortAction']
  } = {}): BaselineIntent => {
    return {
      schemaVersion: 2,
      policies: Object.fromEntries(Object.entries(nextPolicies).filter(([, value]) => value !== 'auto')),
      controlMode: nextControlMode,
      budgetMinutes: boundedInteger(budgetMinutes, 30, 5, 240),
      acceptancePolicy: {
        maxKnownCritical: boundedInteger(maxKnownCritical, 0, 0, 99),
        maxKnownHigh: boundedInteger(maxKnownHigh, 1, 0, 99),
      },
      extraIterations: Math.max(0, Number(plan.intent.extraIterations ?? 0) + extra),
      decisionGrantIterations: grant,
      // Transport-only compatibility hints for the current Python engine.
      searchMode,
      executionMode: nextControlMode === 'AUTONOMOUS' ? 'BACKGROUND' : 'FAST',
      proofMode,
      deferredCohorts: reconcileDeferredCohorts(nextDeferredCohorts, nextPolicies),
      ...(cohortAction ? { cohortAction } : {}),
    }
  }

  const submit = async (intent: BaselineIntent) => {
    setBusy(true)
    try { await onSubmit(intent) } finally { setBusy(false) }
  }

  const requestCancel = () => {
    if (busy) return
    if (dirty && !window.confirm(text(
      'Изменения состава Baseline ещё не применены. Закрыть окно и отбросить их?',
      'Baseline scope changes have not been applied yet. Close and discard them?',
    ))) return
    onCancel()
  }

  const setPolicy = (name: string, policy: BaselinePackagePolicy) => {
    setPolicies((current) => ({ ...current, [name]: policy }))
    if (policy !== 'keep-current') {
      setDeferredCohorts((current) => current
        .map((cohort) => ({ ...cohort, packages: cohort.packages.filter((item) => item !== name) }))
        .filter((cohort) => cohort.packages.length > 0))
    }
  }

  const suggestedCohort = decision?.suggestedCohort

  const deferSuggestedCohort = () => {
    if (!suggestedCohort?.packages.length) return
    const nextPolicies = { ...policies }
    const alreadyCohortOwned = new Set(deferredCohorts.flatMap((cohort) => cohort.packages))
    // Preserve manual keep-current ownership while allowing overlapping cohorts.
    const cohortOwnedPackages = suggestedCohort.packages.filter((name) =>
      (nextPolicies[name] ?? 'auto') === 'auto' || alreadyCohortOwned.has(name))
    if (!cohortOwnedPackages.length) return
    for (const name of cohortOwnedPackages) {
      if ((nextPolicies[name] ?? 'auto') === 'auto') nextPolicies[name] = 'keep-current'
    }
    const existing = deferredCohorts.find((cohort) => cohort.id === suggestedCohort.id)
    const nextCohort: DeferredCohort = {
      id: suggestedCohort.id,
      label: suggestedCohort.label,
      packages: [...new Set([...(existing?.packages ?? []), ...cohortOwnedPackages])].sort(),
      predicate: suggestedCohort.predicate,
      confidence: suggestedCohort.confidence,
      authority: 'DIAGNOSTIC_HINT',
      deferredAt: existing?.deferredAt ?? new Date().toISOString(),
      decisionId: suggestedCohort.decisionId,
      boundaryPackages: suggestedCohort.boundaryPackages,
      warningPackages: suggestedCohort.warningPackages,
    }
    const nextDeferred = [...deferredCohorts.filter((cohort) => cohort.id !== nextCohort.id), nextCohort]
    setPolicies(nextPolicies)
    setDeferredCohorts(nextDeferred)
    void submit(buildIntent({
      extra: DECISION_TRANCHE,
      grant: DECISION_TRANCHE,
      nextPolicies,
      searchMode: 'BOUNDED_IMPROVEMENT',
      nextControlMode: controlMode,
      nextDeferredCohorts: nextDeferred,
      cohortAction: {
        kind: 'DEFER',
        cohortId: nextCohort.id,
        label: nextCohort.label,
        packages: cohortOwnedPackages,
        predicate: nextCohort.predicate,
        confidence: nextCohort.confidence,
        decisionId: nextCohort.decisionId,
      },
    }))
  }

  const reactivateCohort = (cohort: DeferredCohort) => {
    const nextPolicies = { ...policies }
    const nextDeferred = deferredCohorts.filter((item) => item.id !== cohort.id)
    const stillCohortOwned = new Set(nextDeferred.flatMap((item) => item.packages))
    for (const name of cohort.packages) {
      if (nextPolicies[name] === 'keep-current' && !stillCohortOwned.has(name)) delete nextPolicies[name]
    }
    setPolicies(nextPolicies)
    setDeferredCohorts(nextDeferred)
    void submit(buildIntent({
      nextPolicies,
      searchMode: 'AUTO',
      nextControlMode: controlMode,
      nextDeferredCohorts: nextDeferred,
      cohortAction: {
        kind: 'REACTIVATE',
        cohortId: cohort.id,
        label: cohort.label,
        packages: cohort.packages,
        predicate: cohort.predicate,
        confidence: cohort.confidence,
        decisionId: cohort.decisionId,
      },
    }))
  }

  const continueExhaustive = () => void submit(buildIntent({
    extra: DECISION_TRANCHE * 8,
    grant: DECISION_TRANCHE * 8,
    searchMode: 'EXHAUSTIVE',
    nextControlMode: 'AUTONOMOUS',
  }))

  const applyAndContinue = () => void submit(buildIntent({
    extra: mode === 'decision' ? DECISION_TRANCHE : 0,
    grant: mode === 'decision' ? DECISION_TRANCHE : 0,
    searchMode: mode === 'decision' ? 'BOUNDED_IMPROVEMENT' : searchDepth,
  }))

  const buildDraft = () => void submit(buildIntent({
    proofMode: 'DRAFT',
    searchMode: 'AUTO',
    nextControlMode: controlMode,
  }))

  const keepFocusAndContinue = () => {
    if (!decision?.package) return
    const next = { ...policies, [decision.package]: 'keep-current' as const }
    setPolicies(next)
    void submit(buildIntent({ extra: DECISION_TRANCHE, grant: DECISION_TRANCHE, nextPolicies: next, searchMode: 'BOUNDED_IMPROVEMENT', nextControlMode: controlMode }))
  }

  const kindOptions = [
    { value: 'all', label: text('Все типы', 'All types') },
    { value: 'runtime', label: 'runtime' },
    { value: 'dev', label: 'dev' },
    { value: 'peer', label: 'peer' },
  ]

  return (
    <div className="baseline-intent-backdrop" role="presentation">
      <section className="baseline-intent-dialog" role="dialog" aria-modal="true" aria-labelledby="baseline-intent-title">
        <header className="baseline-intent-header">
          <div>
            {mode === 'decision' ? <AlertTriangle size={18} /> : <ShieldCheck size={18} />}
            <div>
              <strong id="baseline-intent-title">{mode === 'decision' ? text('Нужно решение по Baseline', 'Baseline decision required') : text('Проверенный Baseline', 'Verified Baseline')}</strong>
              <span>{mode === 'decision'
                ? text('DepLoom сохранил подтверждённые ограничения. Основной Flow пытается быстро получить рабочий verified результат, откладывая только локально проблемную группу после вашего подтверждения.', 'DepLoom preserved confirmed constraints. The main flow aims for a working verified result quickly and defers only a locally problematic group after your confirmation.')
                : text('Сначала пробуем весь выбранный scope. Если совместимость локально блокирует поиск, DepLoom предложит временно отложить связанную группу и вернуться к ней следующим проходом.', 'We first try the full selected scope. If compatibility is locally blocked, DepLoom will suggest temporarily deferring the related group and revisiting it in a later pass.')}</span>
            </div>
          </div>
          <button type="button" className="icon-button" aria-label={text('Закрыть без применения', 'Close without applying')} onClick={requestCancel}><X size={17} /></button>
        </header>

        <div className="baseline-fast-flow">
          <div>
            <strong>{text('Как работать', 'How to run')}</strong>
            <span>{text('DepLoom ищет первый physically verified полезный результат, затем улучшает его, пока есть смысл. Acceptance и freshness теперь независимы.', 'DepLoom seeks the first physically verified useful result, then improves it while worthwhile. Acceptance and freshness are now independent.')}</span>
          </div>
          <div className="baseline-run-controls">
            <div className="baseline-run-control">
              <span className="baseline-run-control-label">{text('Контроль', 'Control')}</span>
              <div className="baseline-mode-toggle" role="group" aria-label={text('Режим контроля Baseline', 'Baseline control mode')}>
                <button type="button" className={controlMode === 'AUTONOMOUS' ? 'active' : ''} aria-pressed={controlMode === 'AUTONOMOUS'} disabled={busy} onClick={() => setControlMode('AUTONOMOUS')}>{text('Автономно', 'Autonomous')}</button>
                <button type="button" className={controlMode === 'CONFIRM_SIGNIFICANT' ? 'active' : ''} aria-pressed={controlMode === 'CONFIRM_SIGNIFICANT'} disabled={busy} onClick={() => setControlMode('CONFIRM_SIGNIFICANT')}>{text('Подтверждать существенное', 'Confirm significant')}</button>
              </div>
              <small>{controlMode === 'AUTONOMOUS'
                ? text('Без обычных continuation-диалогов: только safety/authority stop. Deferred cohorts сохраняются для следующих проходов.', 'No ordinary continuation dialogs: only safety/authority stops. Deferred cohorts remain available for later passes.')
                : text('DepLoom попросит решение, когда нужно существенно менять scope или продолжать дорогой поиск.', 'DepLoom asks when it needs a significant scope change or an expensive continuation.')}</small>
            </div>
            <div className="baseline-run-control">
              <span className="baseline-run-control-label">{text('Бюджет Baseline / планирования', 'Baseline / planning budget')}</span>
              <label>{text('Минуты', 'Minutes')}<input type="number" min={5} max={240} step={5} value={budgetMinutes} disabled={busy} onChange={(event) => setBudgetMinutes(boundedInteger(event.target.value, 30, 5, 240))} /></label>
              <small>{text('Ограничивает дорогой поиск и physical verification на этапе Baseline. Это не таймер всего FLOW.', 'Bounds expensive search and physical verification during Baseline. It is not a timer for the whole FLOW.')}</small>
            </div>
            <div className="baseline-run-control">
              <span className="baseline-run-control-label">{text('Acceptance policy', 'Acceptance policy')}</span>
              <label>Critical ≤ <input type="number" min={0} max={99} value={maxKnownCritical} disabled={busy} onChange={(event) => setMaxKnownCritical(boundedInteger(event.target.value, 0, 0, 99))} /></label>
              <label>High ≤ <input type="number" min={0} max={99} value={maxKnownHigh} disabled={busy} onChange={(event) => setMaxKnownHigh(boundedInteger(event.target.value, 1, 0, 99))} /></label>
              <small>{text('По умолчанию C=0/H≤1. UNKNOWN audit evidence никогда не считается accepted.', 'Default is C=0/H≤1. UNKNOWN audit evidence is never accepted.')}</small>
            </div>
          </div>
          <details className="baseline-advanced-actions">
            <summary>{text('Техническая глубина поиска', 'Technical search depth')}</summary>
            <div className="baseline-mode-toggle" role="group" aria-label={text('Техническая глубина поиска Baseline', 'Technical Baseline search depth')}>
              <button type="button" className={searchDepth === 'AUTO' ? 'active' : ''} aria-pressed={searchDepth === 'AUTO'} disabled={busy} onClick={() => setSearchDepth('AUTO')}>AUTO</button>
              <button type="button" className={searchDepth === 'EXHAUSTIVE' ? 'active' : ''} aria-pressed={searchDepth === 'EXHAUSTIVE'} disabled={busy} onClick={() => setSearchDepth('EXHAUSTIVE')}>EXHAUSTIVE</button>
            </div>
            <small>{text('Это диагностическая настройка алгоритма, а не цель результата.', 'This is an algorithm diagnostic setting, not a result goal.')}</small>
          </details>
        </div>

        {deferredCohorts.length ? (
          <div className="baseline-deferred-queue">
            <div>
              <strong>{text('Отложено на следующие проходы', 'Deferred for later passes')}</strong>
              <span>{text('Это очередь улучшения verified scope, а не список «несовместимых» пакетов.', 'This is a verified-scope improvement queue, not a list of packages proven incompatible.')}</span>
            </div>
            {deferredCohorts.map((cohort) => (
              <div className="baseline-deferred-cohort" key={cohort.id}>
                <div><strong>{cohort.label}</strong><small>{cohort.packages.length} package(s)</small></div>
                <div className="baseline-cohort-packages">{cohort.packages.map((name) => <code key={name}>{name}</code>)}</div>
                <button type="button" className="button secondary" disabled={busy} onClick={() => reactivateCohort(cohort)}>{text('Вернуться к этой группе', 'Work on this group next')}</button>
              </div>
            ))}
          </div>
        ) : null}

        {mode === 'decision' && decision ? (
          <>
            {suggestedCohort?.packages.length ? (
              <div className="baseline-cohort-suggestion">
                <strong>{text(`Рекомендуем пока отложить: ${suggestedCohort.label}`, `Recommended for now: defer ${suggestedCohort.label}`)}</strong>
                <span>{text('Это динамическая compatibility-группа, выведенная из failure predicate, reverse dependency paths, interaction graph и ecosystem prior. Подсказка не является proof.', 'This dynamic compatibility group is inferred from the failure predicate, reverse dependency paths, interaction graph and an ecosystem prior. The suggestion is not proof.')}</span>
                <div className="baseline-cohort-packages">{suggestedCohort.packages.map((name) => <code key={name}>{name}</code>)}</div>
                {suggestedCohort.blockedPackages.length ? <small>{text(`Не будут отложены автоматически: ${suggestedCohort.blockedPackages.join(', ')} (Required/Critical)`, `Will not be deferred: ${suggestedCohort.blockedPackages.join(', ')} (Required/Critical)`)}</small> : null}
                {suggestedCohort.warningPackages.length ? <small className="warning">{text(`В группе есть High-priority security: ${suggestedCohort.warningPackages.join(', ')}. Проверьте решение перед deferral.`, `The group contains High-priority security packages: ${suggestedCohort.warningPackages.join(', ')}. Review before deferring.`)}</small> : null}
                {suggestedCohort.boundaryPackages.length ? <small>{text(`Граница группы: ${suggestedCohort.boundaryPackages.join(', ')}. Если blocker останется после deferral, DepLoom сможет расширить neighborhood уже на следующем подтверждённом кандидате.`, `Group boundary: ${suggestedCohort.boundaryPackages.join(', ')}. If the blocker remains after deferral, DepLoom can expand the neighborhood on the next confirmed candidate.`)}</small> : null}
                <button type="button" className="button primary" disabled={busy} onClick={deferSuggestedCohort}>{text(`Отложить группу (${suggestedCohort.packages.length}) и продолжить`, `Defer group (${suggestedCohort.packages.length}) and continue`)}</button>
                <details className="baseline-technical-details">
                  <summary>{text('Технические детали', 'Technical details')}</summary>
                  <small>confidence {Math.round(suggestedCohort.confidence * 100)}% · {suggestedCohort.authority}</small>
                  <code>{suggestedCohort.predicate || decision.repeatedPredicate || decision.predicate || '—'}</code>
                  <small>{suggestedCohort.reasons.join(' · ')}</small>
                </details>
              </div>
            ) : null}

            <div className="baseline-decision-summary">
              <strong>{decision.reason === 'policy-unsat'
                ? text('Для выбранной политики нет совместимого assignment', 'No compatible assignment exists under the selected policy')
                : suggestedCohort
                  ? text('Progressive search нашёл локальную точку сложности', 'Progressive search found a local hard region')
                  : text('Progressive search дошёл до границы автоматического поиска', 'Progressive search reached the automatic search boundary')}</strong>
              <span>{suggestedCohort
                ? text('Рекомендуемое действие выше позволит продолжить к первому verified accepted результату.', 'The recommended action above lets the search continue toward the first verified accepted result.')
                : decision.package
                  ? `${decision.package}${decision.currentVersion ? ` · current ${decision.currentVersion}` : ''}`
                  : decision.cohortResolution?.reason === 'PREDICATE_UNAVAILABLE'
                    ? text('Exact failing assignment сохранён, но устойчивого structural predicate недостаточно для безопасного автоматического cohort proposal. Можно изменить scope вручную или включить глубокий поиск.', 'The exact failing assignment is preserved, but there is not enough stable structural predicate evidence for a safe automatic cohort proposal. You can edit the scope manually or enable deep search.')
                    : decision.cohortResolution?.reason === 'NO_SAFE_DEFERRABLE_PACKAGE_OR_NEIGHBORHOOD'
                      ? text('Blocker распознан, но в связанном neighborhood сейчас нет нового безопасно deferrable direct package: пакеты уже отложены, Required/Critical или не имеют достаточной structural связи. Можно изменить scope вручную или включить глубокий поиск.', 'The blocker is recognized, but there is no new safely deferrable direct package in its neighborhood: packages are already deferred, Required/Critical, or lack sufficient structural linkage. You can edit the scope manually or enable deep search.')
                      : text('Уверенную группу вывести не удалось. Можно изменить scope вручную или включить глубокий поиск.', 'No confident group could be inferred. You can edit the scope manually or enable deep search.')}</span>
              {!suggestedCohort && (decision.predicate || decision.repeatedPredicate) ? <code>{decision.predicate || decision.repeatedPredicate}</code> : null}
              {decision.failedVersions?.length ? <small>{text('Подтверждённо не подошли', 'Confirmed incompatible')}: {decision.failedVersions.join(', ')}</small> : null}
              <small>{text(`Итерация ${decision.iteration ?? '—'} · learned constraints ${decision.learnedConstraints ?? '—'}`, `Iteration ${decision.iteration ?? '—'} · learned constraints ${decision.learnedConstraints ?? '—'}`)}</small>
            </div>
          </>
        ) : null}

        <div className="baseline-intent-toolbar">
          <label><Search size={14} /><input autoFocus spellCheck={false} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text('Найти зависимость', 'Find dependency')} /></label>
          <QuickSelect value={kind} options={kindOptions} onChange={(value) => setKind(value as typeof kind)} ariaLabel={text('Фильтр типа зависимости', 'Dependency type filter')} />
          <button type="button" className="button secondary" disabled={busy} onClick={() => { setPolicies({}); setDeferredCohorts([]) }}>{text('Все → AUTO', 'All → AUTO')}</button>
        </div>

        <div className="baseline-intent-stats">
          <span>AUTO <b>{counts.auto}</b></span>
          <span>{text('Отложено/current', 'Deferred/current')} <b>{counts['keep-current']}</b></span>
          <span>{text('Обязательно', 'Required')} <b>{counts.required}</b></span>
        </div>

        <div className="baseline-intent-list">
          <div className="baseline-intent-row baseline-intent-row-head"><span>Package</span><span>{text('Тип', 'Type')}</span><span>{text('Текущая', 'Current')}</span><span>{text('Правило', 'Policy')}</span></div>
          {visible.map((item) => {
            const policy = policies[item.name] ?? 'auto'
            const focused = item.name === decision?.package || suggestedCohort?.packages.includes(item.name)
            return (
              <div className={`baseline-intent-row${focused ? ' focused' : ''}`} key={item.name}>
                <div><strong>{item.name}</strong><small title={item.requestedSpec}>{item.requestedSpec}</small></div>
                <span>{item.kind}</span>
                <code>{item.currentVersion || '—'}</code>
                <div className="baseline-policy-toggle" role="group" aria-label={text(`Правило для ${item.name}`, `Policy for ${item.name}`)}>
                  <button type="button" className={policy === 'auto' ? 'active' : ''} aria-pressed={policy === 'auto'} disabled={busy} onClick={() => setPolicy(item.name, 'auto')}>AUTO</button>
                  <button type="button" className={policy === 'keep-current' ? 'active' : ''} aria-pressed={policy === 'keep-current'} disabled={busy} onClick={() => setPolicy(item.name, 'keep-current')}>{text('Пока current', 'Keep current')}</button>
                  <button type="button" className={policy === 'required' ? 'active required' : ''} aria-pressed={policy === 'required'} disabled={busy} onClick={() => setPolicy(item.name, 'required')}>{text('Обязательно', 'Required')}</button>
                </div>
              </div>
            )
          })}
        </div>

        <footer className="baseline-intent-actions">
          <span className="baseline-intent-apply-hint">{dirty ? text('Есть неприменённые изменения', 'There are unapplied changes') : text('Scope готов', 'Scope is ready')}</span>
          <button type="button" className="button secondary" disabled={busy} onClick={requestCancel}>{mode === 'decision' ? text('Оставить на паузе', 'Keep paused') : text('Отмена', 'Cancel')}</button>
          {mode === 'prepare' ? <button type="button" className="button secondary" disabled={busy} onClick={buildDraft} title={text('Без install/lifecycle/project checks. Compatibility останется UNKNOWN.', 'No install/lifecycle/project checks. Compatibility remains UNKNOWN.')}>{text('Сформировать Draft без проверки', 'Build unverified Draft')}</button> : null}
          {mode === 'prepare' ? <button type="button" className="button primary" disabled={busy} onClick={applyAndContinue}>{controlMode === 'AUTONOMOUS' ? text('Запустить автономно', 'Start autonomously') : text('Запустить Baseline', 'Start Baseline')}</button> : null}
          {mode === 'decision' && !suggestedCohort?.packages.length && decision?.package ? <button type="button" className="button primary" disabled={busy} onClick={keepFocusAndContinue}>{text(`Пока оставить ${decision.package} current`, `Keep ${decision.package} current for now`)}</button> : null}
          {mode === 'decision' && !suggestedCohort?.packages.length && !decision?.package ? <button type="button" className="button primary" disabled={busy} onClick={applyAndContinue}>{text('Применить scope и продолжить', 'Apply scope and continue')}</button> : null}
          {mode === 'decision' ? (
            <details className="baseline-advanced-actions">
              <summary>{text('Другие варианты', 'Other options')}</summary>
              <button type="button" className="button secondary" disabled={busy} onClick={applyAndContinue}>{text('Применить ручные изменения', 'Apply manual changes')}</button>
              <button type="button" className="button secondary" disabled={busy} onClick={buildDraft}>{text('Сформировать Draft для передачи агенту', 'Build Draft for agent handoff')}</button>
              <button type="button" className="button secondary" disabled={busy} onClick={continueExhaustive}>{text('Технически: продолжить EXHAUSTIVE автономно', 'Technical: continue EXHAUSTIVE autonomously')}</button>
            </details>
          ) : null}
        </footer>
      </section>
    </div>
  )
}
