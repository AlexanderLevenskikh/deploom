import { AlertTriangle, Check, Copy, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useLanguage } from '../i18n'
import type { LagBlocker, TargetClosure } from '../types'

type Props = {
  closure: TargetClosure
  projectName: string
  onClose: () => void
}

function blockerRows(blockers: LagBlocker[]): LagBlocker[] {
  return [...blockers].sort((a, b) => (a.group ?? 9) - (b.group ?? 9) || a.package.localeCompare(b.package))
}

export function GoalDetailsModal({ closure, projectName, onClose }: Props) {
  const { t, text } = useLanguage()
  const [copied, setCopied] = useState(false)
  const fixable = useMemo(() => blockerRows(closure.lagBlockers.filter((blocker) => Boolean(blocker.plannedTarget))), [closure.lagBlockers])
  const stuck = useMemo(() => blockerRows(closure.lagBlockers.filter((blocker) => !blocker.plannedTarget)), [closure.lagBlockers])
  const targetLabel = closure.target === 'yellow' ? t('levels.yellow') : t('levels.green')
  const currentLabel = closure.current === 'red' ? t('levels.red') : closure.current === 'yellow' ? t('levels.yellow') : closure.current === 'green' ? t('levels.green') : t('levels.unknown')
  const needed = closure.target === 'green' ? closure.lagBlockers.length : (closure.neededForYellow ?? 0)

  const copyText = useMemo(() => {
    const line = (blocker: LagBlocker) => text(
      `- ${blocker.package} (группа ${blocker.group ?? '?'}): ${blocker.current ?? '?'} → нужно ≥ ${blocker.required ?? '?'}${blocker.plannedTarget ? `, план: ${blocker.plannedTarget}` : ', target отсутствует'}${blocker.note ? ` — ${blocker.note}` : ''}`,
      `- ${blocker.package} (group ${blocker.group ?? '?'}): ${blocker.current ?? '?'} → required ≥ ${blocker.required ?? '?'}${blocker.plannedTarget ? `, plan: ${blocker.plannedTarget}` : ', target missing'}${blocker.note ? ` — ${blocker.note}` : ''}`,
    )
    return [
      text(`Проект: ${projectName}`, `Project: ${projectName}`),
      text(
        `Цель: ${targetLabel}. Текущий уровень: ${currentLabel}${typeof closure.lagOkPct === 'number' ? ` · ${closure.lagOkPct.toFixed(1)}%` : ''}${typeof closure.lagOk === 'number' && typeof closure.total === 'number' ? ` (${closure.lagOk} из ${closure.total})` : ''}`,
        `Target: ${targetLabel}. Current level: ${currentLabel}${typeof closure.lagOkPct === 'number' ? ` · ${closure.lagOkPct.toFixed(1)}%` : ''}${typeof closure.lagOk === 'number' && typeof closure.total === 'number' ? ` (${closure.lagOk} of ${closure.total})` : ''}`,
      ),
      needed > 0 ? text(`Не хватает совместимых зависимостей: ${needed}`, `Compatible dependencies still needed: ${needed}`) : t('goal.thresholdMet'),
      '',
      t('goal.fixable', { count: fixable.length }),
      ...fixable.map(line),
      '',
      t('goal.stuck', { count: stuck.length }),
      ...stuck.map(line),
    ].join('\n')
  }, [closure, currentLabel, fixable, needed, projectName, stuck, targetLabel, t, text])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyText)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable, ignore */ }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={t('goal.title', { target: targetLabel })} onClick={onClose}>
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div><AlertTriangle size={18} className="warning-text" /><strong>{t('goal.title', { target: targetLabel })}</strong></div>
          <button className="icon-button" title={t('common.close')} onClick={onClose}><X size={16} /></button>
        </div>

        <div className="modal-summary">
          <div><span>{t('goal.currentLevel')}</span><strong>{currentLabel}{typeof closure.lagOkPct === 'number' ? ` · ${closure.lagOkPct.toFixed(1)}%` : ''}</strong></div>
          <div><span>{t('goal.lagCompliant')}</span><strong>{typeof closure.lagOk === 'number' && typeof closure.total === 'number' ? `${closure.lagOk}/${closure.total}` : '—'}</strong></div>
          <div><span>{t('goal.shortfall')}</span><strong>{needed > 0 ? String(needed) : t('goal.thresholdMet')}</strong></div>
          <div><span>{t('goal.excluded')}</span><strong>{closure.excluded ?? 0}</strong></div>
        </div>

        {closure.target === 'yellow' ? <p className="modal-note">{t('goal.yellowRule')}{typeof closure.critical === 'number' ? ` Critical: ${closure.critical}${typeof closure.high === 'number' ? `, High: ${closure.high}` : ''}.` : ''}</p> : null}

        {closure.lagBlockers.length === 0
          ? <p className="modal-note">{t('goal.oldReport')}</p>
          : <>
            <section className="modal-section">
              <h4>{t('goal.fixable', { count: fixable.length })}</h4>
              {fixable.length === 0
                ? <p className="modal-note">{t('goal.fixableNone')}</p>
                : <><p className="modal-note">{t('goal.fixableHint')}</p>
                  <table className="modal-table"><thead><tr><th>{t('goal.package')}</th><th>{t('goal.groupShort')}</th><th>{t('goal.now')}</th><th>{t('goal.required')}</th><th>{t('goal.plan')}</th></tr></thead>
                    <tbody>{fixable.map((blocker) => <tr key={blocker.package}>
                      <td><code>{blocker.package}</code></td><td>{blocker.group ?? '—'}</td>
                      <td><code>{blocker.current ?? '—'}</code></td><td><code>{blocker.required ?? '—'}</code></td>
                      <td><code>{blocker.plannedTarget}</code></td>
                    </tr>)}</tbody></table></>}
            </section>

            <section className="modal-section">
              <h4>{t('goal.stuck', { count: stuck.length })}</h4>
              {stuck.length === 0
                ? <p className="modal-note">{t('goal.stuckNone')}</p>
                : <><p className="modal-note">{t('goal.stuckHint')}</p>
                  <table className="modal-table"><thead><tr><th>{t('goal.package')}</th><th>{t('goal.groupShort')}</th><th>{t('goal.now')}</th><th>{t('goal.required')}</th><th>{t('goal.reason')}</th></tr></thead>
                    <tbody>{stuck.map((blocker) => <tr key={blocker.package}>
                      <td><code>{blocker.package}</code></td><td>{blocker.group ?? '—'}</td>
                      <td><code>{blocker.current ?? '—'}</code></td><td><code>{blocker.required ?? '—'}</code></td>
                      <td className="modal-reason">{blocker.note || '—'}</td>
                    </tr>)}</tbody></table></>}
            </section>
          </>}

        <div className="modal-actions">
          <button className="button secondary" onClick={() => void copy()}>{copied ? <Check size={16} /> : <Copy size={16} />} {copied ? t('common.copied') : t('goal.copyDetails')}</button>
          <button className="button primary" onClick={onClose}>{t('common.understood')}</button>
        </div>
      </div>
    </div>
  )
}
