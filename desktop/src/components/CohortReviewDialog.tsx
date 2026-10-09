import { useState } from 'react'
import './CohortReviewDialog.css'
import { useLanguage } from '../i18n'
import type { IterativeStatusOutcome } from '../types'

type Cohort = NonNullable<IterativeStatusOutcome['cohortReview']>
export function CohortReviewDialog({ cohort, onSubmit, onClose }: { cohort: Cohort; onSubmit: (selected: string[]) => Promise<void>; onClose: () => void }) {
  const { text } = useLanguage()
  const [selected, setSelected] = useState(() => new Set(cohort.packages.map(p => p.name)))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const submit = async (names: string[]) => {
    setBusy(true); setError(undefined)
    try { await onSubmit(names) } catch (e) { setError(String(e)); setBusy(false) }
  }
  const toggle = (name: string, checked: boolean) => {
    const names = new Set([name])
    let changed = true
    while (changed) { changed = false; for (const group of cohort.atomicGroups ?? []) if (group.some(n => names.has(n))) for (const n of group) if (!names.has(n)) { names.add(n); changed = true } }
    setSelected(previous => { const next = new Set(previous); for (const n of names) if (cohort.packages.some(p => p.name === n)) { if (checked) next.add(n); else next.delete(n) }; return next })
  }
  return <div className="dialog-backdrop" onKeyDown={event => { if (event.key === 'Escape' && !busy) { event.stopPropagation(); onClose() } }}><section className="dialog cohort-review-dialog" role="dialog" aria-modal="true" aria-labelledby="cohort-review-title" data-testid="cohort-review-dialog">
    <header><h2 id="cohort-review-title">{text('Согласовать группу зависимостей', 'Review dependency cohort')}</h2></header>
    <p>{text('Подбор объединяет пакеты по связям peer-зависимостей и ограничениям совместимости. Связанные атомарные группы выбираются целиком. Изменения ещё не применены.', 'Planning groups packages by peer dependencies and compatibility constraints. Atomic groups are selected together. Changes have not been applied yet.')}</p>
    <p>{text('После исключения пакетов пересчитаем состав от проверенного результата. Отложенные цели останутся видны; новый состав снова потребует согласования.', 'Exclusions trigger re-planning from the verified checkpoint. Deferred goals remain visible; the new proposal needs another approval.')}</p>
    <div style={{ maxHeight: '45vh', overflow: 'auto' }}>{cohort.packages.map(p => <label key={p.name} style={{ display: 'flex', gap: 12, padding: 8 }}><input autoFocus={p === cohort.packages[0]} type="checkbox" checked={selected.has(p.name)} disabled={busy} onChange={e => toggle(p.name, e.target.checked)} /><span><strong>{p.name}</strong><br />{p.current || '?'} → {p.target}</span></label>)}</div>
    {error ? <p role="alert">{error}</p> : null}
    <footer className="dialog-actions"><button className="button secondary" disabled={busy} onClick={onClose}>{text('Решить позже', 'Decide later')}</button><button className="button secondary" disabled={busy} onClick={() => void submit([])}>{text('Отклонить группу и подобрать следующую', 'Defer cohort and plan the next')}</button><button className="button primary" disabled={busy || selected.size === 0} onClick={() => void submit([...selected])}>{busy ? text('Сохраняем…', 'Saving…') : selected.size === cohort.packages.length ? text('Одобрить и продолжить', 'Approve and continue') : text('Исключить и пересчитать', 'Exclude and re-plan')}</button></footer>
  </section></div>
}
