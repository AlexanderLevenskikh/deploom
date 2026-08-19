import { AlertTriangle, Search, ShieldCheck, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useLanguage } from '../i18n'
import type { BaselineDecision, BaselineIntent, BaselineIntentPlan, BaselinePackagePolicy } from '../types'

type Props = {
  mode: 'prepare' | 'decision'
  plan: BaselineIntentPlan
  decision?: BaselineDecision
  onCancel: () => void
  onSubmit: (intent: BaselineIntent) => Promise<void>
}

const DECISION_TRANCHE = 8

export function BaselineIntentDialog({ mode, plan, decision, onCancel, onSubmit }: Props) {
  const { text } = useLanguage()
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<'all' | 'runtime' | 'dev' | 'peer'>('all')
  const [policies, setPolicies] = useState<Record<string, BaselinePackagePolicy>>({ ...plan.intent.policies })
  const [busy, setBusy] = useState(false)

  useEffect(() => setPolicies({ ...plan.intent.policies }), [plan])

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return plan.candidates.filter((item) => {
      if (kind !== 'all' && item.kind !== kind) return false
      return !needle || item.name.toLowerCase().includes(needle)
    })
  }, [kind, plan.candidates, query])

  const counts = useMemo(() => {
    const result = { auto: 0, 'keep-current': 0, required: 0 }
    for (const item of plan.candidates) result[policies[item.name] ?? 'auto'] += 1
    return result
  }, [plan.candidates, policies])

  const buildIntent = (extra = 0, grant = 0, nextPolicies = policies): BaselineIntent => ({
    schemaVersion: 1,
    policies: Object.fromEntries(Object.entries(nextPolicies).filter(([, value]) => value !== 'auto')),
    extraIterations: Math.max(0, Number(plan.intent.extraIterations ?? 0) + extra),
    decisionGrantIterations: grant,
  })

  const submit = async (intent: BaselineIntent) => {
    setBusy(true)
    try { await onSubmit(intent) } finally { setBusy(false) }
  }

  const continueSearch = () => void submit(buildIntent(DECISION_TRANCHE, DECISION_TRANCHE))
  const applyAndContinue = () => void submit(buildIntent(mode === 'decision' ? DECISION_TRANCHE : 0, mode === 'decision' ? DECISION_TRANCHE : 0))
  const keepFocusAndContinue = () => {
    if (!decision?.package) return
    const next = { ...policies, [decision.package]: 'keep-current' as const }
    setPolicies(next)
    void submit(buildIntent(DECISION_TRANCHE, DECISION_TRANCHE, next))
  }

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
                : text('Можно оставить часть зависимостей текущими или потребовать их обновления именно в этом расчёте. AUTO сохраняет обычную политику DepLoom.', 'You may keep some dependencies current or require their update in this run. AUTO keeps the normal DepLoom policy.')}</span>
            </div>
          </div>
          <button className="icon-button" aria-label={text('Закрыть', 'Close')} onClick={onCancel}><X size={17} /></button>
        </header>

        {mode === 'decision' && decision ? (
          <div className="baseline-decision-summary">
            <strong>{decision.reason === 'budget-exhausted'
              ? text('Исчерпан автоматический search budget', 'Automatic search budget exhausted')
              : decision.reason === 'policy-unsat'
                ? text('Для выбранной политики нет совместимого assignment', 'No compatible assignment exists under the selected policy')
                : text('Повторяющийся конфликт', 'Repeated compatibility conflict')}</strong>
            <span>{decision.package ? `${decision.package}${decision.currentVersion ? ` · current ${decision.currentVersion}` : ''}` : text('Можно изменить состав Baseline или продолжить поиск.', 'You can change Baseline scope or continue searching.')}</span>
            {decision.predicate ? <code>{decision.predicate}</code> : null}
            {decision.failedVersions?.length ? <small>{text('Подтверждённо не подошли', 'Confirmed incompatible')}: {decision.failedVersions.join(', ')}</small> : null}
            <small>{text(`Итерация ${decision.iteration ?? '—'} · learned constraints ${decision.learnedConstraints ?? '—'}`, `Iteration ${decision.iteration ?? '—'} · learned constraints ${decision.learnedConstraints ?? '—'}`)}</small>
          </div>
        ) : null}

        <div className="baseline-intent-toolbar">
          <label><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text('Найти зависимость', 'Find dependency')} /></label>
          <select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}>
            <option value="all">{text('Все типы', 'All types')}</option>
            <option value="runtime">runtime</option>
            <option value="dev">dev</option>
            <option value="peer">peer</option>
          </select>
          <button className="button secondary" onClick={() => setPolicies({})}>{text('Все → AUTO', 'All → AUTO')}</button>
          <button className="button secondary" onClick={() => setPolicies((current) => ({ ...current, ...Object.fromEntries(plan.candidates.filter((item) => item.kind === 'dev').map((item) => [item.name, 'keep-current' as const] as const)) }))}>{text('DEV → оставить', 'DEV → keep')}</button>
        </div>

        <div className="baseline-intent-stats">
          <span>AUTO <b>{counts.auto}</b></span>
          <span>{text('Оставить', 'Keep')} <b>{counts['keep-current']}</b></span>
          <span>{text('Обязательно', 'Required')} <b>{counts.required}</b></span>
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
                <select value={policy} onChange={(event) => setPolicies((current) => ({ ...current, [item.name]: event.target.value as BaselinePackagePolicy }))}>
                  <option value="auto">AUTO</option>
                  <option value="keep-current">{text('Оставить текущую', 'Keep current')}</option>
                  <option value="required">{text('Обязательно обновить', 'Required update')}</option>
                </select>
              </div>
            )
          })}
        </div>

        <footer className="baseline-intent-actions">
          <button className="button secondary" disabled={busy} onClick={onCancel}>{mode === 'decision' ? text('Оставить на паузе', 'Keep paused') : text('Отмена', 'Cancel')}</button>
          {mode === 'decision' ? <button className="button secondary" disabled={busy} onClick={continueSearch}>{text(`Продолжить поиск (+${DECISION_TRANCHE})`, `Continue search (+${DECISION_TRANCHE})`)}</button> : null}
          {mode === 'decision' && decision?.package ? <button className="button secondary" disabled={busy} onClick={keepFocusAndContinue}>{text(`Оставить ${decision.package} текущей`, `Keep ${decision.package} current`)}</button> : null}
          <button className="button primary" disabled={busy} onClick={applyAndContinue}>{mode === 'decision' ? text('Применить и продолжить', 'Apply and continue') : text('Запустить Baseline', 'Start Baseline')}</button>
        </footer>
      </section>
    </div>
  )
}
