import { AlertTriangle, Search, ShieldCheck, X } from 'lucide-react'
import { useDeferredValue, useEffect, useMemo, useState } from 'react'
import { useLanguage } from '../i18n'
import type { BaselineDecision, BaselineExecutionMode, BaselineIntent, BaselineIntentPlan, BaselinePackagePolicy } from '../types'
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

function normalizedExecutionMode(value: BaselineExecutionMode | undefined): BaselineExecutionMode {
  return value === 'BACKGROUND' ? 'BACKGROUND' : 'FAST'
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
  const [executionMode, setExecutionMode] = useState<BaselineExecutionMode>(normalizedExecutionMode(plan.intent.executionMode))
  const [deferredCohorts, setDeferredCohorts] = useState<DeferredCohort[]>([...(plan.intent.deferredCohorts ?? [])])
  const [busy, setBusy] = useState(false)
  const deferredQuery = useDeferredValue(query)

  useEffect(() => {
    setPolicies({ ...plan.intent.policies })
    setExecutionMode(normalizedExecutionMode(plan.intent.executionMode))
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
      || executionMode !== normalizedExecutionMode(plan.intent.executionMode)
      || cohortFingerprint(deferredCohorts) !== cohortFingerprint(plan.intent.deferredCohorts ?? []),
    [deferredCohorts, executionMode, plan.intent.deferredCohorts, plan.intent.executionMode, plan.intent.policies, policies],
  )

  const buildIntent = ({
    extra = 0,
    grant = 0,
    nextPolicies = policies,
    searchMode = 'AUTO' as BaselineIntent['searchMode'],
    nextExecutionMode = executionMode,
    nextDeferredCohorts = deferredCohorts,
    cohortAction,
  }: {
    extra?: number
    grant?: number
    nextPolicies?: Record<string, BaselinePackagePolicy>
    searchMode?: BaselineIntent['searchMode']
    nextExecutionMode?: BaselineExecutionMode
    nextDeferredCohorts?: DeferredCohort[]
    cohortAction?: BaselineIntent['cohortAction']
  } = {}): BaselineIntent => ({
    schemaVersion: 1,
    policies: Object.fromEntries(Object.entries(nextPolicies).filter(([, value]) => value !== 'auto')),
    extraIterations: Math.max(0, Number(plan.intent.extraIterations ?? 0) + extra),
    decisionGrantIterations: grant,
    searchMode,
    executionMode: normalizedExecutionMode(nextExecutionMode),
    deferredCohorts: reconcileDeferredCohorts(nextDeferredCohorts, nextPolicies),
    ...(cohortAction ? { cohortAction } : {}),
  })

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
      nextExecutionMode: 'FAST',
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
      nextExecutionMode: 'FAST',
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
    nextExecutionMode: 'BACKGROUND',
  }))

  const applyAndContinue = () => void submit(buildIntent({
    extra: mode === 'decision' ? DECISION_TRANCHE : 0,
    grant: mode === 'decision' ? DECISION_TRANCHE : 0,
    searchMode: mode === 'decision' ? 'BOUNDED_IMPROVEMENT' : 'AUTO',
  }))

  const keepFocusAndContinue = () => {
    if (!decision?.package) return
    const next = { ...policies, [decision.package]: 'keep-current' as const }
    setPolicies(next)
    void submit(buildIntent({ extra: DECISION_TRANCHE, grant: DECISION_TRANCHE, nextPolicies: next, searchMode: 'BOUNDED_IMPROVEMENT', nextExecutionMode: 'FAST' }))
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
              <strong id="baseline-intent-title">{mode === 'decision' ? text('Нужно решение по Baseline', 'Baseline decision required') : text('Быстрый Baseline', 'Fast Baseline')}</strong>
              <span>{mode === 'decision'
                ? text('DepLoom сохранил подтверждённые ограничения. Основной Flow пытается быстро получить рабочий verified результат, откладывая только локально проблемную группу после вашего подтверждения.', 'DepLoom preserved confirmed constraints. The main flow aims for a working verified result quickly and defers only a locally problematic group after your confirmation.')
                : text('Сначала пробуем весь выбранный scope. Если совместимость локально блокирует поиск, DepLoom предложит временно отложить связанную группу и вернуться к ней следующим проходом.', 'We first try the full selected scope. If compatibility is locally blocked, DepLoom will suggest temporarily deferring the related group and revisiting it in a later pass.')}</span>
            </div>
          </div>
          <button type="button" className="icon-button" aria-label={text('Закрыть без применения', 'Close without applying')} onClick={requestCancel}><X size={17} /></button>
        </header>

        <div className="baseline-fast-flow">
          <div>
            <strong>{executionMode === 'BACKGROUND' ? text('☾ Глубокий поиск', '☾ Deep search') : text('⚡ Fast-first', '⚡ Fast-first')}</strong>
            <span>{executionMode === 'BACKGROUND'
              ? text('Дополнительный режим: DepLoom тратит больше времени на глобальные комбинации и глубокую диагностику.', 'Optional mode: DepLoom spends more time on global combinations and deeper diagnostics.')
              : text('Никакого массового DEV-exclude: беспроблемные dev-зависимости тоже обновляются. Отступаем только вокруг реально наблюдаемого compatibility-региона.', 'No blanket DEV exclusion: healthy dev dependencies are updated too. We only step back around an actually observed compatibility region.')}</span>
          </div>
          <details className="baseline-advanced-flow">
            <summary>{text('Дополнительно', 'Advanced')}</summary>
            {executionMode === 'BACKGROUND'
              ? <button type="button" className="button secondary" disabled={busy} onClick={() => setExecutionMode('FAST')}>{text('Вернуться к Fast-first', 'Return to Fast-first')}</button>
              : <button type="button" className="button secondary" disabled={busy} onClick={() => setExecutionMode('BACKGROUND')}>{text('Готов ждать: искать глубже в фоне', 'I can wait: search deeper in background')}</button>}
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
                {suggestedCohort.boundaryPackages.length ? <small>{text(`Граница группы: ${suggestedCohort.boundaryPackages.join(', ')}. При повторении того же failure DepLoom сможет расширить neighborhood.`, `Group boundary: ${suggestedCohort.boundaryPackages.join(', ')}. If the same failure repeats, DepLoom can expand the neighborhood.`)}</small> : null}
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
                  ? text('Fast-first нашёл локальную точку сложности', 'Fast-first found a local hard region')
                  : text('Fast-first дошёл до границы автоматического поиска', 'Fast-first reached the automatic search boundary')}</strong>
              <span>{suggestedCohort
                ? text('Рекомендуемое действие выше позволит продолжить к первому verified partial/Yellow результату.', 'The recommended action above lets the search continue toward the first verified partial/Yellow result.')
                : decision.package
                  ? `${decision.package}${decision.currentVersion ? ` · current ${decision.currentVersion}` : ''}`
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
          {mode === 'prepare' ? <button type="button" className="button primary" disabled={busy} onClick={applyAndContinue}>{executionMode === 'BACKGROUND' ? text('Запустить глубокий поиск', 'Start deep search') : text('Запустить Fast Baseline', 'Start Fast Baseline')}</button> : null}
          {mode === 'decision' && !suggestedCohort?.packages.length && decision?.package ? <button type="button" className="button primary" disabled={busy} onClick={keepFocusAndContinue}>{text(`Пока оставить ${decision.package} current`, `Keep ${decision.package} current for now`)}</button> : null}
          {mode === 'decision' && !suggestedCohort?.packages.length && !decision?.package ? <button type="button" className="button primary" disabled={busy} onClick={applyAndContinue}>{text('Применить scope и продолжить', 'Apply scope and continue')}</button> : null}
          {mode === 'decision' ? (
            <details className="baseline-advanced-actions">
              <summary>{text('Другие варианты', 'Other options')}</summary>
              <button type="button" className="button secondary" disabled={busy} onClick={applyAndContinue}>{text('Применить ручные изменения', 'Apply manual changes')}</button>
              <button type="button" className="button secondary" disabled={busy} onClick={continueExhaustive}>{text('Готов ждать: исчерпывающий поиск в фоне', 'I can wait: exhaustive search in background')}</button>
            </details>
          ) : null}
        </footer>
      </section>
    </div>
  )
}
