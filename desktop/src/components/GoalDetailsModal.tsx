import { AlertTriangle, Check, Copy, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { LagBlocker, TargetClosure } from '../types'

type Props = {
  closure: TargetClosure
  projectName: string
  onClose: () => void
}

const LEVEL_LABELS = { red: 'Красный', yellow: 'Жёлтый', green: 'Зелёный', unknown: 'не рассчитан' } as const

function blockerRows(blockers: LagBlocker[]): LagBlocker[] {
  return [...blockers].sort((a, b) => (a.group ?? 9) - (b.group ?? 9) || a.package.localeCompare(b.package))
}

export function GoalDetailsModal({ closure, projectName, onClose }: Props) {
  const [copied, setCopied] = useState(false)
  // The one distinction that changes what to do next: a package the plan still
  // has a target for is closed by running the agent again; one without a
  // target cannot be, and needs a scope decision or its own upgrade task.
  const fixable = useMemo(() => blockerRows(closure.lagBlockers.filter((blocker) => Boolean(blocker.plannedTarget))), [closure.lagBlockers])
  const stuck = useMemo(() => blockerRows(closure.lagBlockers.filter((blocker) => !blocker.plannedTarget)), [closure.lagBlockers])
  const targetLabel = closure.target === 'yellow' ? 'Жёлтый' : 'Зелёный'
  // Yellow is a percentage threshold, so "how many more" is a real number.
  // Green requires *every* dependency to comply, so the shortfall is simply
  // all of them -- reusing the yellow number there would understate the work.
  const needed = closure.target === 'green' ? closure.lagBlockers.length : (closure.neededForYellow ?? 0)

  const copyText = useMemo(() => {
    const line = (blocker: LagBlocker) => `- ${blocker.package} (группа ${blocker.group ?? '?'}): ${blocker.current ?? '?'} → нужно ≥ ${blocker.required ?? '?'}${blocker.plannedTarget ? `, план: ${blocker.plannedTarget}` : ', target отсутствует'}${blocker.note ? ` — ${blocker.note}` : ''}`
    return [
      `Проект: ${projectName}`,
      `Цель: ${targetLabel}. Текущий уровень: ${LEVEL_LABELS[closure.current]}${typeof closure.lagOkPct === 'number' ? ` · ${closure.lagOkPct.toFixed(1)}%` : ''}${typeof closure.lagOk === 'number' && typeof closure.total === 'number' ? ` (${closure.lagOk} из ${closure.total})` : ''}`,
      needed > 0 ? `Не хватает совместимых зависимостей: ${needed}` : 'Порог по проценту выполнен',
      '',
      `Закрывается текущим планом (${fixable.length}):`,
      ...fixable.map(line),
      '',
      `Не закрывается текущим планом (${stuck.length}):`,
      ...stuck.map(line),
    ].join('\n')
  }, [closure, fixable, stuck, needed, projectName, targetLabel])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyText)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable, ignore */ }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Почему цель не достигнута" onClick={onClose}>
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div><AlertTriangle size={18} className="warning-text" /><strong>Почему цель «{targetLabel}» не достигнута</strong></div>
          <button className="icon-button" title="Закрыть" onClick={onClose}><X size={16} /></button>
        </div>

        <div className="modal-summary">
          <div><span>Текущий уровень</span><strong>{LEVEL_LABELS[closure.current]}{typeof closure.lagOkPct === 'number' ? ` · ${closure.lagOkPct.toFixed(1)}%` : ''}</strong></div>
          <div><span>Соблюдают lag-policy</span><strong>{typeof closure.lagOk === 'number' && typeof closure.total === 'number' ? `${closure.lagOk} из ${closure.total}` : '—'}</strong></div>
          <div><span>Не хватает до порога</span><strong>{needed > 0 ? `${needed} ${needed === 1 ? 'зависимости' : 'зависимостей'}` : 'порог выполнен'}</strong></div>
          <div><span>Исключено из расчёта</span><strong>{closure.excluded ?? 0}</strong></div>
        </div>

        {closure.target === 'yellow' ? <p className="modal-note">Для жёлтого нужно ≥ 80% зависимостей в рамках их lag-policy и ноль Critical{typeof closure.critical === 'number' ? ` (сейчас Critical: ${closure.critical}${typeof closure.high === 'number' ? `, High: ${closure.high}` : ''})` : ''}.</p> : null}

        {closure.lagBlockers.length === 0
          ? <p className="modal-note">Детализация по пакетам недоступна: отчёт построен более старой версией. Перезапустите «Верификация», чтобы увидеть, какие именно зависимости не проходят lag-policy.</p>
          : <>
            <section className="modal-section">
              <h4>Закрывается текущим планом — {fixable.length}</h4>
              {fixable.length === 0
                ? <p className="modal-note">Ни одной. Повторный запуск агента по текущему плану не изменит процент — нужна работа со scope ниже.</p>
                : <><p className="modal-note">Выгрузите свежий prompt из Dashboard и прогоните миграцию — этого достаточно, чтобы закрыть их.</p>
                  <table className="modal-table"><thead><tr><th>Пакет</th><th>Гр.</th><th>Сейчас</th><th>Нужно ≥</th><th>План</th></tr></thead>
                    <tbody>{fixable.map((blocker) => <tr key={blocker.package}>
                      <td><code>{blocker.package}</code></td><td>{blocker.group ?? '—'}</td>
                      <td><code>{blocker.current ?? '—'}</code></td><td><code>{blocker.required ?? '—'}</code></td>
                      <td><code>{blocker.plannedTarget}</code></td>
                    </tr>)}</tbody></table></>}
            </section>

            <section className="modal-section">
              <h4>Не закрывается текущим планом — {stuck.length}</h4>
              {stuck.length === 0
                ? <p className="modal-note">Нет таких зависимостей.</p>
                : <><p className="modal-note">У этих пакетов нет target в текущем плане: планировщик счёл их слишком дорогими или объективно заблокированными. Их нельзя закрыть повторным запуском агента — либо заведите отдельную задачу на обновление, либо исключите из scope с причиной в Dashboard.</p>
                  <table className="modal-table"><thead><tr><th>Пакет</th><th>Гр.</th><th>Сейчас</th><th>Нужно ≥</th><th>Причина</th></tr></thead>
                    <tbody>{stuck.map((blocker) => <tr key={blocker.package}>
                      <td><code>{blocker.package}</code></td><td>{blocker.group ?? '—'}</td>
                      <td><code>{blocker.current ?? '—'}</code></td><td><code>{blocker.required ?? '—'}</code></td>
                      <td className="modal-reason">{blocker.note || '—'}</td>
                    </tr>)}</tbody></table></>}
            </section>
          </>}

        <div className="modal-actions">
          <button className="button secondary" onClick={() => void copy()}>{copied ? <Check size={16} /> : <Copy size={16} />} {copied ? 'Скопировано' : 'Скопировать детали'}</button>
          <button className="button primary" onClick={onClose}>Понятно</button>
        </div>
      </div>
    </div>
  )
}
