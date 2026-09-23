import { AlertTriangle, Search, ShieldCheck, X } from 'lucide-react'
import { useDeferredValue, useEffect, useMemo, useState } from 'react'
import { useLanguage } from '../i18n'
import { attemptsForMinutes, handleDialogBudget } from '../../electron/baseline-intent'
import type { BaselineControlMode, BaselineDecision, BaselineIntent, BaselineIntentPlan, BaselinePackagePolicy, BaselineProofMode, BaselineSearchMode } from '../types'
import { QuickSelect } from './QuickSelect'

type Props = {
  mode: 'prepare' | 'decision'
  resume?: 'auto' | 'continue' | 'restart'
  plan: BaselineIntentPlan
  decision?: BaselineDecision
  onCancel: () => void
  onSubmit: (intent: BaselineIntent, autoOpenPrompt?: boolean) => Promise<void>
}

type DeferredCohort = NonNullable<BaselineIntent['deferredCohorts']>[number]

const DECISION_TRANCHE = 8

function normalizedControlMode(intent: BaselineIntent): BaselineControlMode {
  if (intent.controlMode === 'AUTONOMOUS' || intent.controlMode === 'CONFIRM_SIGNIFICANT') return intent.controlMode
  if (intent.executionMode === 'FAST' || intent.executionMode === 'AUTOPILOT') return 'CONFIRM_SIGNIFICANT'
  return 'AUTONOMOUS'
}

function boundedInteger(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(min, Math.min(max, Math.round(parsed)))
}

function boundedLagMonths(value: unknown): number {
  const parsed = Number(value)
  return parsed === 3 || parsed === 6 || parsed === 9 || parsed === 12 ? parsed : 12
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

export function BaselineIntentDialog({ mode, resume, plan, decision, onCancel, onSubmit }: Props) {
  const { text } = useLanguage()
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<'all' | 'runtime' | 'dev' | 'peer'>('all')
  const [policies, setPolicies] = useState<Record<string, BaselinePackagePolicy>>({ ...plan.intent.policies })
  const [controlMode, setControlMode] = useState<BaselineControlMode>(normalizedControlMode(plan.intent))
  const [budgetMinutes, setBudgetMinutes] = useState(boundedInteger(plan.intent.budgetMinutes, 30, 5, 240))
  // N1: an untouched budget field is NOT an explicit user choice. Only editing
  // the minutes (or reopening an explicitly saved intent) carries the override.
  const [budgetEdited, setBudgetEdited] = useState(false)
  const [maxKnownHigh, setMaxKnownHigh] = useState(boundedInteger(plan.intent.acceptancePolicy?.maxKnownHigh, 1, 0, 99))
  const [searchDepth, setSearchDepth] = useState<BaselineSearchMode>(normalizedSearchMode(plan.intent.searchMode))
  const [deferredCohorts, setDeferredCohorts] = useState<DeferredCohort[]>([...(plan.intent.deferredCohorts ?? [])])
  const [targetLevel, setTargetLevel] = useState<'yellow' | 'green'>(plan.intent.targetLevel === 'green' ? 'green' : 'yellow')
  const [minLagOkPct, setMinLagOkPct] = useState(boundedInteger(plan.intent.minLagOkPct, 80, 0, 100))
  const [lagPolicyMonths, setLagPolicyMonths] = useState<number>(
    boundedLagMonths(plan.intent.lagPolicyMonths ?? plan.intent.acceptancePolicy?.lagPolicyMonths ?? 12),
  )
  const [busy, setBusy] = useState(false)
  // G5: product search strategy for verified runs. FAST stops on the first
  // verified assignment that actually satisfies the acceptance policy; DEEP
  // keeps improving and preserves the best verified incumbent (legacy default).
  const [productMode, setProductMode] = useState<'fast' | 'deep'>(plan.intent.productMode ?? 'deep')
  const deferredQuery = useDeferredValue(query)

  useEffect(() => {
    setPolicies({ ...plan.intent.policies })
    setControlMode(normalizedControlMode(plan.intent))
    setBudgetMinutes(boundedInteger(plan.intent.budgetMinutes, 30, 5, 240))
    setMaxKnownHigh(boundedInteger(plan.intent.acceptancePolicy?.maxKnownHigh, 1, 0, 99))
    setSearchDepth(normalizedSearchMode(plan.intent.searchMode))
    setDeferredCohorts([...(plan.intent.deferredCohorts ?? [])])
    setTargetLevel(plan.intent.targetLevel === 'green' ? 'green' : 'yellow')
    setMinLagOkPct(boundedInteger(plan.intent.minLagOkPct, 80, 0, 100))
    setLagPolicyMonths(boundedLagMonths(plan.intent.lagPolicyMonths ?? plan.intent.acceptancePolicy?.lagPolicyMonths ?? 12))
    setProductMode(plan.intent.productMode ?? 'deep')
    setBudgetEdited(false)
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
      || budgetEdited !== Boolean(plan.intent.budgetMinutesExplicit)
      || budgetMinutes !== boundedInteger(plan.intent.budgetMinutes, 30, 5, 240)
      || maxKnownHigh !== boundedInteger(plan.intent.acceptancePolicy?.maxKnownHigh, 1, 0, 99)
      || searchDepth !== normalizedSearchMode(plan.intent.searchMode)
      || cohortFingerprint(deferredCohorts) !== cohortFingerprint(plan.intent.deferredCohorts ?? [])
      || targetLevel !== (plan.intent.targetLevel === 'green' ? 'green' : 'yellow')
      || minLagOkPct !== boundedInteger(plan.intent.minLagOkPct, 80, 0, 100)
      || lagPolicyMonths !== boundedLagMonths(plan.intent.lagPolicyMonths ?? plan.intent.acceptancePolicy?.lagPolicyMonths ?? 12)
      || productMode !== (plan.intent.productMode ?? 'deep'),
    [budgetMinutes, budgetEdited, controlMode, deferredCohorts, lagPolicyMonths, maxKnownHigh, minLagOkPct, plan.intent, policies, productMode, searchDepth, targetLevel],
  )

  const buildIntent = ({
    extra = 0,
    grant = 0,
    nextPolicies = policies,
    searchMode = searchDepth as BaselineIntent['searchMode'],
    nextControlMode = controlMode,
    proofMode = 'VERIFIED' as BaselineProofMode,
    nextDeferredCohorts = deferredCohorts,
    nextTargetLevel = targetLevel,
    nextMinLagOkPct = minLagOkPct,
    nextLagPolicyMonths = lagPolicyMonths,
    nextProductMode = productMode,
    cohortAction,
  }: {
    extra?: number
    grant?: number
    nextPolicies?: Record<string, BaselinePackagePolicy>
    searchMode?: BaselineIntent['searchMode']
    nextControlMode?: BaselineControlMode
    proofMode?: BaselineProofMode
    nextDeferredCohorts?: DeferredCohort[]
    nextTargetLevel?: 'yellow' | 'green'
    nextMinLagOkPct?: number
    nextLagPolicyMonths?: number
    nextProductMode?: 'fast' | 'deep'
    cohortAction?: BaselineIntent['cohortAction']
  } = {}): BaselineIntent => {
    // F1: the green preset forces the whole numeric goal -- 100% lag
    // compliance, High=0 and Moderate/Low=0 -- and overrides whatever the
    // sliders currently show, so "green" can never be saved with H=1 or an
    // 80% freshness caveat the release gate must then silently reinterpret.
    const effectiveTarget = nextTargetLevel === 'green' ? 'green' : 'yellow'
    const effectiveLagPct = nextTargetLevel === 'green' ? 100 : boundedInteger(nextMinLagOkPct, 80, 0, 100)
    const effectiveHigh = nextTargetLevel === 'green' ? 0 : boundedInteger(maxKnownHigh, 1, 0, 99)
    const effectiveLagMonths = boundedLagMonths(nextLagPolicyMonths)
    return {
      schemaVersion: 2,
      policies: Object.fromEntries(Object.entries(nextPolicies).filter(([, value]) => value !== 'auto')),
      controlMode: nextControlMode,
      // N1: only a genuinely chosen budget travels as an explicit override.
      // An untouched field (or a default that was never saved as a user
      // choice) carries NOTHING, so the Desktop/engine keep the mode budgets.
      ...handleDialogBudget({
        edited: budgetEdited,
        planExplicit: Boolean(plan.intent.budgetMinutesExplicit),
        shownMinutes: budgetMinutes,
        planBudgetMinutes: plan.intent.budgetMinutes,
      }),
      // The acceptancePolicy is the single canonical home of the TargetPolicy:
      // readAcceptanceVerdict reads only this nested object, so the goal must
      // be part of it (not just a top-level mirror).
      acceptancePolicy: {
        maxKnownCritical: 0,
        maxKnownHigh: effectiveHigh,
        targetLevel: effectiveTarget,
        minLagOkPct: effectiveLagPct,
        lagPolicyMonths: effectiveLagMonths,
        ...(effectiveTarget === 'green' ? { maxKnownModerate: 0, maxKnownLow: 0 } : {}),
      },
      extraIterations: Math.max(0, Number(plan.intent.extraIterations ?? 0) + extra),
      decisionGrantIterations: grant,
      // Transport-only compatibility hints for the current Python engine.
      searchMode,
      executionMode: nextControlMode === 'AUTONOMOUS' ? 'BACKGROUND' : 'FAST',
      proofMode,
      deferredCohorts: reconcileDeferredCohorts(nextDeferredCohorts, nextPolicies),
      // Top-level mirrors of the canonical policy for legacy readers and the
      // R9 policy-hash identity; only these objects participate in the hash.
      targetLevel: effectiveTarget,
      minLagOkPct: effectiveLagPct,
      lagPolicyMonths: effectiveLagMonths,
      // G5: the chosen product search strategy for VERIFIED runs travels with
      // the intent so the persistence/transport layers can re-apply it.
      ...(proofMode !== 'DRAFT' ? { productMode: nextProductMode === 'fast' ? 'fast' : 'deep' } : {}),
      ...(cohortAction ? { cohortAction } : {}),
    }
  }

  const submit = async (intent: BaselineIntent, autoOpenPrompt?: boolean) => {
    setBusy(true)
    try { await onSubmit(intent, autoOpenPrompt) } finally { setBusy(false) }
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

  const buildDraft = (autoOpenPrompt = false) => void submit(buildIntent({
    proofMode: 'DRAFT',
    searchMode: 'AUTO',
    nextControlMode: controlMode,
  }), autoOpenPrompt)

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
              <strong id="baseline-intent-title">{mode === 'decision' ? text('Нужно решение по Baseline', 'Baseline decision required') : text('Состав Baseline', 'Baseline scope')}</strong>
              <span>{mode === 'decision'
                ? text('DepLoom сохранил подтверждённые ограничения. Основной Flow пытается быстро получить рабочий verified результат, откладывая только локально проблемную группу после вашего подтверждения.', 'DepLoom preserved confirmed constraints. The main flow aims for a working verified result quickly and defers only a locally problematic group after your confirmation.')
                : text('Выберите зависимости для этого запуска. Затем можно либо получить быстрый planning-only Draft с промптом, либо запустить physically verified Baseline.', 'Choose dependencies for this run. Then either build a quick planning-only Draft with an agent prompt or start the physically verified Baseline.')}</span>
            </div>
          </div>
          <button type="button" className="icon-button" aria-label={text('Закрыть без применения', 'Close without applying')} onClick={requestCancel}><X size={17} /></button>
        </header>

        {resume === 'restart' ? <div className="resume-notice baseline-restart-notice"><strong>{text('Baseline будет начат заново', 'Baseline will be restarted')}</strong><span>{text('После запуска оркестрационный checkpoint будет сброшен; exact proof/artifact cache с совпадающей identity останется доступен. Здесь можно применить состав Baseline или сразу запустить.', 'After you start, the orchestration checkpoint will be reset, while the exact proof/artifact cache with matching identity stays reusable. Apply the Baseline scope here or start right away.')}</span></div> : null}

        <div className="baseline-intent-scroll">
        <div className="baseline-fast-flow">
          <div>
            <strong>{text('Как работать', 'How to run')}</strong>
            <span>{text('DepLoom ищет первый physically verified полезный результат, затем улучшает его, пока есть смысл. Acceptance и freshness теперь независимы.', 'DepLoom seeks the first physically verified useful result, then improves it while worthwhile. Acceptance and freshness are now independent.')}</span>
          </div>
          <div className="baseline-run-controls">
            <div className="baseline-run-control">
              <span className="baseline-run-control-label">{text('Режим поиска', 'Search strategy')}</span>
              <div className="baseline-mode-toggle" role="group" aria-label={text('Стратегия поиска Baseline', 'Baseline search strategy')}>
                <button type="button" className={productMode === 'fast' ? 'active' : ''} aria-pressed={productMode === 'fast'} disabled={busy} onClick={() => setProductMode('fast')}>{text('Fast', 'Fast')}</button>
                <button type="button" className={productMode === 'deep' ? 'active' : ''} aria-pressed={productMode === 'deep'} disabled={busy} onClick={() => setProductMode('deep')}>{text('Deep', 'Deep')}</button>
              </div>
              <small>{productMode === 'fast'
                ? text('Fast останавливается на первом physically verified кандидате, который реально удовлетворяет Acceptance policy (все известные Critical/High закрыты, lag-цель достигнута). Дедлайн и бюджет строго ограничены.', 'FAST stops on the first physically verified candidate that actually satisfies the acceptance policy (every known Critical/High covered, lag goal met). Deadline and budget are tightly bounded.')
                : text('Deep продолжает улучшать результат после первого удовлетворяющего кандидата и сохраняет лучший verified incumbent при ошибке/таймауте. Используется по умолчанию.', 'DEEP keeps improving after the first satisfying candidate and preserves the best verified incumbent on error/timeout. This is the default.')}</small>
              {budgetEdited || Boolean(plan.intent.budgetMinutesExplicit) ? (
                <small className="baseline-budget-applied">
                  {text(`Применяемый лимит: ${boundedInteger(budgetMinutes, 30, 5, 240)} мин (${boundedInteger(budgetMinutes, 30, 5, 240) * 60} с · ${attemptsForMinutes(budgetMinutes)} дорогих попыток)`, `Applied budget: ${boundedInteger(budgetMinutes, 30, 5, 240)} min (${boundedInteger(budgetMinutes, 30, 5, 240) * 60}s · ${attemptsForMinutes(budgetMinutes)} expensive attempts)`)}
                </small>
              ) : (
                <small className="baseline-budget-applied">
                  {text('Без выбора бюджета применяются режимные лимиты: Fast 300 с / 2 попытки, Deep 3600 с / 12.', 'Without a chosen budget the mode limits apply: Fast 300s/2 attempts, Deep 3600s/12.')}
                </small>
              )}
            </div>
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
              <label>{text('Минуты', 'Minutes')}<input type="number" min={5} max={240} step={5} value={budgetMinutes} disabled={busy} onChange={(event) => { setBudgetEdited(true); setBudgetMinutes(boundedInteger(event.target.value, 30, 5, 240)) }} /></label>
              <small>{text('Ограничивает дорогой поиск и physical verification на этапе Baseline. Это не таймер всего FLOW.', 'Bounds expensive search and physical verification during Baseline. It is not a timer for the whole FLOW.')}</small>
            </div>
            <div className="baseline-run-control">
              <span className="baseline-run-control-label">{text('Acceptance policy', 'Acceptance policy')}</span>
              <div className="baseline-critical-fixed"><span>Critical</span><strong>0</strong><small>{text('обязательно', 'required')}</small></div>
              <label>High ≤ <input type="number" min={0} max={99} value={targetLevel === 'green' ? 0 : maxKnownHigh} disabled={busy || targetLevel === 'green'} onChange={(event) => setMaxKnownHigh(boundedInteger(event.target.value, 1, 0, 99))} /></label>
              <small>{targetLevel === 'green'
                ? text('Цель Green всегда требует High=0 (и Moderate/Low=0): зелёный пресет задаёт все лимиты численно, их нельзя ослабить. Critical всегда должен быть 0.', 'The Green goal always requires High=0 (and Moderate/Low=0): the green preset sets every limit numerically and they cannot be relaxed. Critical must always be 0.')
                : text('Critical всегда должен быть 0. Допуск High можно настроить. Неполный или устаревший аудит никогда не считается безопасным.', 'Critical must always be 0. High tolerance is configurable. Incomplete or stale audit evidence is never accepted.')}</small>
            </div>
            <div className="baseline-run-control">
              <span className="baseline-run-control-label">{text('Цель запуска', 'Run goal')}</span>
              <div className="baseline-mode-toggle" role="group" aria-label={text('Целевой уровень Result', 'Result target level')}>
                <button type="button" className={targetLevel === 'yellow' ? 'active' : ''} aria-pressed={targetLevel === 'yellow'} disabled={busy} onClick={() => setTargetLevel('yellow')}>{text('Жёлтый', 'Yellow')}</button>
                <button type="button" className={targetLevel === 'green' ? 'active' : ''} aria-pressed={targetLevel === 'green'} disabled={busy} onClick={() => { setTargetLevel('green'); setMinLagOkPct(100) }}>{text('Зелёный', 'Green')}</button>
              </div>
              <label>{text('Минимум актуальности', 'Minimum lag compliance')} <input type="range" min={0} max={100} step={5} value={targetLevel === 'green' ? 100 : minLagOkPct} disabled={busy || targetLevel === 'green'} onChange={(event) => setMinLagOkPct(boundedInteger(event.target.value, 80, 0, 100))} /> <strong>{targetLevel === 'green' ? 100 : minLagOkPct}%</strong></label>
              <div className="baseline-mode-toggle" role="group" aria-label={text('Lag-порог', 'Lag threshold')}>
                {[3, 6, 9, 12].map((months) => (
                  <button key={months} type="button" className={lagPolicyMonths === months ? 'active' : ''} aria-pressed={lagPolicyMonths === months} disabled={busy} onClick={() => setLagPolicyMonths(boundedLagMonths(String(months)))}>{months} {text('мес', 'mo')}</button>
                ))}
              </div>
              <small>{targetLevel === 'green'
                ? text('Цель Green: 100% библиотек без нарушений lag-политики за выбранный период, C=0/H=0/M=0/L=0, известных и неизвестных вместе. Входит в policy hash: изменение сдвигает реальные пороги плана и приёмки.', 'Green goal: 100% of libraries satisfying the lag policy within the chosen window, C=0/H=0/M=0/L=0, known and unknown together. Part of the policy hash: changing it moves the real plan and acceptance thresholds.')
                : text('Сколько библиотек должны соблюдать lag-policy, чтобы цель считалась достигнутой. Входит в policy hash: изменение сдвигает реальные пороги плана и приёмки.', 'Share of libraries that must satisfy the lag policy for the goal to count as met. It is part of the policy hash: changing it moves the real plan and acceptance thresholds.')}</small>
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

        {mode === 'prepare' ? <div className="baseline-fast-flow">
          <div>
            <strong>{text('Что обновлять в этом запуске', 'What to update in this run')}</strong>
            <span>{text('AUTO — DepLoom решает сам. «Не обновлять сейчас» оставляет текущую версию и исключает зависимость из upgrade scope этого запуска. «Обязательно» принудительно оставляет её в scope.', 'AUTO lets DepLoom decide. “Do not update now” keeps the current version and excludes the dependency from this run upgrade scope. “Required” forces it to remain in scope.')}</span>
          </div>
        </div> : null}

        <div className="baseline-intent-toolbar">
          <label><Search size={14} /><input autoFocus spellCheck={false} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text('Найти зависимость', 'Find dependency')} /></label>
          <QuickSelect value={kind} options={kindOptions} onChange={(value) => setKind(value as typeof kind)} ariaLabel={text('Фильтр типа зависимости', 'Dependency type filter')} />
          <button type="button" className="button secondary" disabled={busy} onClick={() => { setPolicies({}); setDeferredCohorts([]) }}>{text('Все → AUTO', 'All → AUTO')}</button>
        </div>

        <div className="baseline-intent-stats">
          <span>AUTO <b>{counts.auto}</b></span>
          <span>{text('Не обновлять сейчас', 'Do not update now')} <b>{counts['keep-current']}</b></span>
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
                  <button type="button" className={policy === 'keep-current' ? 'active' : ''} aria-pressed={policy === 'keep-current'} disabled={busy} onClick={() => setPolicy(item.name, 'keep-current')}>{text('Не обновлять сейчас', 'Do not update now')}</button>
                  <button type="button" className={policy === 'required' ? 'active required' : ''} aria-pressed={policy === 'required'} disabled={busy} onClick={() => setPolicy(item.name, 'required')}>{text('Обязательно', 'Required')}</button>
                </div>
              </div>
            )
          })}
        </div>
        </div>

        <footer className="baseline-intent-actions">
          <span className="baseline-intent-apply-hint">{dirty ? text('Есть неприменённые изменения', 'There are unapplied changes') : text('Scope готов', 'Scope is ready')}</span>
          <button type="button" className="button secondary" disabled={busy} onClick={requestCancel}>{mode === 'decision' ? text('Оставить на паузе', 'Keep paused') : text('Отмена', 'Cancel')}</button>
          {mode === 'prepare' ? <button type="button" className="button secondary" disabled={busy} onClick={() => buildDraft(true)} title={text('Без install/lifecycle/project checks. После завершения сразу откроется готовый prompt; Dashboard не требуется.', 'No install/lifecycle/project checks. The ready prompt opens immediately after completion; Dashboard is not required.')}>{text('Создать Draft и показать промпт', 'Create Draft and show prompt')}</button> : null}
          {mode === 'prepare' ? <button type="button" className="button primary" disabled={busy} onClick={applyAndContinue}>{resume === 'restart' ? text('Начать заново и запустить', 'Restart and start') : controlMode === 'AUTONOMOUS' ? text('Запустить автономно', 'Start autonomously') : text('Запустить Baseline', 'Start Baseline')}</button> : null}
          {mode === 'decision' && !suggestedCohort?.packages.length && decision?.package ? <button type="button" className="button primary" disabled={busy} onClick={keepFocusAndContinue}>{text(`Пока оставить ${decision.package} current`, `Keep ${decision.package} current for now`)}</button> : null}
          {mode === 'decision' && !suggestedCohort?.packages.length && !decision?.package ? <button type="button" className="button primary" disabled={busy} onClick={applyAndContinue}>{text('Применить scope и продолжить', 'Apply scope and continue')}</button> : null}
          {mode === 'decision' ? (
            <details className="baseline-advanced-actions">
              <summary>{text('Другие варианты', 'Other options')}</summary>
              <button type="button" className="button secondary" disabled={busy} onClick={applyAndContinue}>{text('Применить ручные изменения', 'Apply manual changes')}</button>
              <button type="button" className="button secondary" disabled={busy} onClick={() => buildDraft()}>{text('Сформировать Draft для передачи агенту', 'Build Draft for agent handoff')}</button>
              <button type="button" className="button secondary" disabled={busy} onClick={continueExhaustive}>{text('Технически: продолжить EXHAUSTIVE автономно', 'Technical: continue EXHAUSTIVE autonomously')}</button>
            </details>
          ) : null}
        </footer>
      </section>
    </div>
  )
}
