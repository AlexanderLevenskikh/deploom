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

const DECISION_TRANCHE = 8

function policyFingerprint(policies: Record<string, BaselinePackagePolicy>): string {
  return Object.entries(policies)
    .filter(([, policy]) => policy !== 'auto')
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, policy]) => `${name}\u0000${policy}`)
    .join('\u0001')
}

export function BaselineIntentDialog({ mode, plan, decision, onCancel, onSubmit }: Props) {
  const { text } = useLanguage()
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<'all' | 'runtime' | 'dev' | 'peer'>('all')
  const [policies, setPolicies] = useState<Record<string, BaselinePackagePolicy>>({ ...plan.intent.policies })
  const [executionMode, setExecutionMode] = useState<BaselineExecutionMode>(plan.intent.executionMode === 'BACKGROUND' ? 'BACKGROUND' : 'FAST')
  const [busy, setBusy] = useState(false)
  const deferredQuery = useDeferredValue(query)

  useEffect(() => {
    setPolicies({ ...plan.intent.policies })
    setExecutionMode(plan.intent.executionMode === 'BACKGROUND' ? 'BACKGROUND' : 'FAST')
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
      || executionMode !== (plan.intent.executionMode === 'BACKGROUND' ? 'BACKGROUND' : 'FAST'),
    [executionMode, plan.intent.executionMode, plan.intent.policies, policies],
  )

  const buildIntent = (extra = 0, grant = 0, nextPolicies = policies, searchMode: BaselineIntent['searchMode'] = 'AUTO', nextExecutionMode: BaselineExecutionMode = executionMode): BaselineIntent => ({
    schemaVersion: 1,
    policies: Object.fromEntries(Object.entries(nextPolicies).filter(([, value]) => value !== 'auto')),
    extraIterations: Math.max(0, Number(plan.intent.extraIterations ?? 0) + extra),
    decisionGrantIterations: grant,
    searchMode,
    executionMode: nextExecutionMode,
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
  }

  const chooseExecutionMode = (nextMode: BaselineExecutionMode) => {
    setExecutionMode(nextMode === 'BACKGROUND' ? 'BACKGROUND' : 'FAST')
  }

  const suggestedCohort = decision?.suggestedCohort
  const deferSuggestedCohort = () => {
    if (!suggestedCohort?.packages?.length) return
    const next = { ...policies }
    for (const name of suggestedCohort.packages) {
      if (next[name] !== 'required') next[name] = 'keep-current'
    }
    setPolicies(next)
    void submit(buildIntent(DECISION_TRANCHE, DECISION_TRANCHE, next, 'BOUNDED_IMPROVEMENT', 'FAST'))
  }

  const continueExhaustive = () => void submit(buildIntent(DECISION_TRANCHE * 8, DECISION_TRANCHE * 8, policies, 'EXHAUSTIVE', 'BACKGROUND'))
  const applyAndContinue = () => void submit(buildIntent(mode === 'decision' ? DECISION_TRANCHE : 0, mode === 'decision' ? DECISION_TRANCHE : 0, policies, mode === 'decision' ? 'BOUNDED_IMPROVEMENT' : 'AUTO'))
  const keepFocusAndContinue = () => {
    if (!decision?.package) return
    const next = { ...policies, [decision.package]: 'keep-current' as const }
    setPolicies(next)
    void submit(buildIntent(DECISION_TRANCHE, DECISION_TRANCHE, next, 'BOUNDED_IMPROVEMENT'))
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
              <strong id="baseline-intent-title">{mode === 'decision' ? text('Нужно решение по Baseline', 'Baseline decision required') : text('Что включить в Baseline', 'Choose Baseline scope')}</strong>
              <span>{mode === 'decision'
                ? text('Автоматический поиск сохранил все подтверждённые constraints и ждёт вашего решения. Совместимость всё равно будет проверена package manager и project checks.', 'Automatic search preserved all confirmed constraints and is waiting for your decision. Compatibility will still be verified by the package manager and project checks.')
                : text('AUTO — обычная политика DepLoom. «Исключить» оставляет пакет на текущей версии и убирает его из цели этого Baseline. «Обязательно» требует обновления.', 'AUTO uses normal DepLoom policy. Exclude keeps the package at its current version and removes it from this Baseline target. Required forces an update.')}</span>
            </div>
          </div>
          <button type="button" className="icon-button" aria-label={text('Закрыть без применения', 'Close without applying')} onClick={requestCancel}><X size={17} /></button>
        </header>

        <div className="baseline-fast-flow">
          <div>
            <strong>{executionMode === 'BACKGROUND' ? text('☾ Глубокий поиск', '☾ Deep search') : text('⚡ Быстрый Baseline', '⚡ Fast Baseline')}</strong>
            <span>{executionMode === 'BACKGROUND'
              ? text('DepLoom будет дольше исследовать проблемные комбинации. Это дополнительный режим для случаев, когда вы готовы ждать.', 'DepLoom will spend longer exploring difficult combinations. This is an optional mode for when you are willing to wait.')
              : text('Основной Flow: сначала получить рабочий verified результат. Если совместимость локально мешает, DepLoom предложит временно отложить только связанную когорту — без массового исключения DEV-пакетов.', 'Main flow: get a working verified result first. If compatibility is locally blocked, DepLoom will suggest deferring only the related cohort instead of excluding all dev packages.')}</span>
          </div>
          <details className="baseline-advanced-flow">
            <summary>{text('Дополнительно', 'Advanced')}</summary>
            {executionMode === 'BACKGROUND'
              ? <button type="button" className="button secondary" disabled={busy} onClick={() => chooseExecutionMode('FAST')}>{text('Вернуться к быстрому Flow', 'Return to Fast flow')}</button>
              : <button type="button" className="button secondary" disabled={busy} onClick={() => chooseExecutionMode('BACKGROUND')}>{text('Готов ждать: искать глубже в фоне', 'I can wait: search deeper in background')}</button>}
          </details>
        </div>

        {mode === 'decision' && decision ? (
          <>
            {suggestedCohort?.packages?.length ? (
              <div className="baseline-cohort-suggestion">
                <strong>{text(`Рекомендуем временно отложить ${suggestedCohort.label}`, `Temporarily defer ${suggestedCohort.label}`)}</strong>
                <span>{text(
                  'Конфликт выглядит локальным для связанной compatibility-когорты. Быстрее получить рабочий Baseline для остальных зависимостей, а к этой группе вернуться отдельным проходом. Это эвристика навигации, не proof.',
                  'The conflict looks local to a related compatibility cohort. It is faster to get a working Baseline for the remaining dependencies and revisit this group in a separate pass. This is a navigation heuristic, not proof.',
                )}</span>
                <div className="baseline-cohort-packages">
                  {suggestedCohort.packages.map((name) => <code key={name}>{name}</code>)}
                </div>
                {suggestedCohort.requiredPackages?.length ? (
                  <small>{text(
                    `Не будут автоматически исключены обязательные: ${suggestedCohort.requiredPackages.join(', ')}`,
                    `Required packages will not be automatically deferred: ${suggestedCohort.requiredPackages.join(', ')}`,
                  )}</small>
                ) : null}
                <button type="button" className="button primary" disabled={busy} onClick={deferSuggestedCohort}>
                  {text(`Отложить когорту (${suggestedCohort.packages.length}) и продолжить`, `Defer cohort (${suggestedCohort.packages.length}) and continue`)}
                </button>
                <details className="baseline-technical-details">
                  <summary>{text('Почему DepLoom это предлагает', 'Why DepLoom suggests this')}</summary>
                  <small>{text('Confidence', 'Confidence')}: {Math.round(suggestedCohort.confidence * 100)}% · {suggestedCohort.authority}</small>
                  <code>{suggestedCohort.predicate}</code>
                </details>
              </div>
            ) : null}

            <div className="baseline-decision-summary">
              <strong>{decision.reason === 'budget-exhausted' || decision.reason === 'AUTOMATIC_BUDGET_EXHAUSTED' || decision.reason === 'BASE_ITERATION_LIMIT'
                ? text('Быстрый поиск дошёл до границы бюджета', 'Fast search reached its budget boundary')
                : decision.reason === 'policy-unsat'
                  ? text('Для выбранной политики нет совместимого assignment', 'No compatible assignment exists under the selected policy')
                  : text('Повторяется одна и та же compatibility-проблема', 'The same compatibility problem keeps repeating')}</strong>
              <span>{suggestedCohort
                ? text('Основной рекомендуемый шаг показан выше. Подтверждённые constraints сохранены.', 'The recommended next step is shown above. Confirmed constraints are preserved.')
                : decision.package
                  ? `${decision.package}${decision.currentVersion ? ` · current ${decision.currentVersion}` : ''}`
                  : text('DepLoom не смог уверенно вывести когорту. Можно изменить состав вручную или выбрать глубокий поиск.', 'DepLoom could not infer a confident cohort. You can edit the scope manually or choose deep search.')}</span>
              {!suggestedCohort && (decision.predicate || decision.repeatedPredicate) ? <code>{decision.predicate || decision.repeatedPredicate}</code> : null}
              {decision.failedVersions?.length ? <small>{text('Подтверждённо не подошли', 'Confirmed incompatible')}: {decision.failedVersions.join(', ')}</small> : null}
              <small>{text(`Итерация ${decision.iteration ?? '—'} · learned constraints ${decision.learnedConstraints ?? '—'}`, `Iteration ${decision.iteration ?? '—'} · learned constraints ${decision.learnedConstraints ?? '—'}`)}</small>
            </div>
          </>
        ) : null}

        <div className="baseline-intent-toolbar">
          <label><Search size={14} /><input autoFocus spellCheck={false} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text('Найти зависимость', 'Find dependency')} /></label>
          <QuickSelect value={kind} options={kindOptions} onChange={(value) => setKind(value as typeof kind)} ariaLabel={text('Фильтр типа зависимости', 'Dependency type filter')} />
          <button type="button" className="button secondary" disabled={busy} onClick={() => setPolicies({})}>{text('Все → AUTO', 'All → AUTO')}</button>
        </div>

        <div className="baseline-intent-stats">
          <span>AUTO <b>{counts.auto}</b></span>
          <span>{text('Исключено', 'Excluded')} <b>{counts['keep-current']}</b></span>
          <span>{text('Обязательно', 'Required')} <b>{counts.required}</b></span>
        </div>

        <div className="baseline-intent-scope-note">
          {text(
            'Важно: исключённая зависимость не обновляется и не входит в health-цель этого Baseline, но может появляться в фазе «Анализ зависимостей»: DepLoom всё равно читает manifest/lockfile и metadata, потому что пакет остаётся частью реального package-manager графа.',
            'Important: an excluded dependency is not updated and is outside this Baseline health target, but it may still appear during dependency analysis because it remains part of the real manifest/lockfile and package-manager graph.',
          )}
        </div>

        <div className="baseline-intent-list">
          <div className="baseline-intent-row baseline-intent-row-head"><span>Package</span><span>{text('Тип', 'Type')}</span><span>{text('Текущая', 'Current')}</span><span>{text('Правило', 'Policy')}</span></div>
          {visible.map((item) => {
            const policy = policies[item.name] ?? 'auto'
            const focused = item.name === decision?.package
            return (
              <div className={`baseline-intent-row${focused ? ' focused' : ''}`} key={item.name}>
                <div><strong>{item.name}</strong><small title={item.requestedSpec}>{item.requestedSpec}</small></div>
                <span>{item.kind}</span>
                <code>{item.currentVersion || '—'}</code>
                <div className="baseline-policy-toggle" role="group" aria-label={text(`Правило для ${item.name}`, `Policy for ${item.name}`)}>
                  <button type="button" className={policy === 'auto' ? 'active' : ''} aria-pressed={policy === 'auto'} disabled={busy} onClick={() => setPolicy(item.name, 'auto')}>AUTO</button>
                  <button type="button" className={policy === 'keep-current' ? 'active' : ''} aria-pressed={policy === 'keep-current'} disabled={busy} onClick={() => setPolicy(item.name, 'keep-current')}>{text('Исключить', 'Exclude')}</button>
                  <button type="button" className={policy === 'required' ? 'active required' : ''} aria-pressed={policy === 'required'} disabled={busy} onClick={() => setPolicy(item.name, 'required')}>{text('Обязательно', 'Required')}</button>
                </div>
              </div>
            )
          })}
        </div>

        <footer className="baseline-intent-actions">
          <span className="baseline-intent-apply-hint">{dirty ? text('Есть неприменённые изменения', 'There are unapplied changes') : text('Состав готов', 'Scope is ready')}</span>
          <button type="button" className="button secondary" disabled={busy} onClick={requestCancel}>{mode === 'decision' ? text('Оставить на паузе', 'Keep paused') : text('Отмена', 'Cancel')}</button>

          {mode === 'prepare' ? (
            <button type="button" className="button primary" disabled={busy} onClick={applyAndContinue}>
              {executionMode === 'BACKGROUND' ? text('Запустить глубокий поиск', 'Start deep search') : text('Запустить быстрый Baseline', 'Start Fast Baseline')}
            </button>
          ) : null}

          {mode === 'decision' && !suggestedCohort?.packages?.length && decision?.package ? (
            <button type="button" className="button primary" disabled={busy} onClick={keepFocusAndContinue}>
              {text(`Пока оставить ${decision.package} текущим`, `Keep ${decision.package} current for now`)}
            </button>
          ) : null}

          {mode === 'decision' && !suggestedCohort?.packages?.length && !decision?.package ? (
            <button type="button" className="button primary" disabled={busy} onClick={applyAndContinue}>{text('Применить состав и продолжить', 'Apply scope and continue')}</button>
          ) : null}

          {mode === 'decision' ? (
            <details className="baseline-advanced-actions">
              <summary>{text('Другие варианты', 'Other options')}</summary>
              <button type="button" className="button secondary" disabled={busy} onClick={applyAndContinue}>{text('Применить ручные изменения', 'Apply manual scope changes')}</button>
              <button type="button" className="button secondary" disabled={busy} onClick={continueExhaustive}>{text('Готов ждать: исчерпывающий поиск в фоне', 'I can wait: exhaustive search in background')}</button>
            </details>
          ) : null}
        </footer>
      </section>
    </div>
  )
}
