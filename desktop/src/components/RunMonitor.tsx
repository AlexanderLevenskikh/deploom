import { Activity, CheckCircle2, LoaderCircle } from 'lucide-react'
import { useMemo } from 'react'
import { deriveRunMonitor, type RunMonitorPhase } from '../data/processMonitor'
import { latestJobId } from '../data/logPresentation'
import { useLanguage } from '../i18n'
import type { JobOutput } from '../types'

type Props = {
  logs: JobOutput[]
  active: boolean
  jobId?: string
}

const PHASE_COPY: Record<RunMonitorPhase, { ru: string; en: string }> = {
  idle: { ru: 'Нет активной операции', en: 'No active operation' },
  scan: { ru: 'Анализ зависимостей', en: 'Dependency analysis' },
  planning: { ru: 'Построение плана', en: 'Building plan' },
  solving: { ru: 'Решение ограничений', en: 'Solving constraints' },
  verifying: { ru: 'Проверка проекта', en: 'Project verification' },
  localizing: { ru: 'Локализация конфликта', en: 'Conflict localization' },
  reproducing: { ru: 'Подтверждение причины', en: 'Reproducing culprit' },
  retrying: { ru: 'Повторная попытка', en: 'Retrying' },
  running: { ru: 'Выполнение', en: 'Running' },
  finished: { ru: 'Последняя операция завершена', en: 'Last operation finished' },
}

function formatDuration(seconds: number | undefined, language: 'ru' | 'en'): string | undefined {
  if (seconds === undefined) return undefined
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hours) return language === 'ru' ? `${hours} ч ${minutes} мин` : `${hours}h ${minutes}m`
  if (minutes) return language === 'ru' ? `${minutes} мин ${secs} с` : `${minutes}m ${secs}s`
  return language === 'ru' ? `${secs} с` : `${secs}s`
}

export function RunMonitor({ logs, active, jobId }: Props) {
  const { language, text } = useLanguage()
  const effectiveJobId = jobId ?? latestJobId(logs)
  const state = useMemo(() => deriveRunMonitor(logs, active, effectiveJobId), [active, effectiveJobId, logs])
  const duration = formatDuration(state.elapsedSeconds, language)
  const phase = PHASE_COPY[state.phase][language]

  let progress: { value: number; max: number; label: string } | undefined
  if (state.dependency) progress = {
    value: state.dependency.current,
    max: state.dependency.total,
    label: text(`Пакеты ${state.dependency.current} из ${state.dependency.total}`, `Packages ${state.dependency.current} of ${state.dependency.total}`),
  }
  if (state.projectCheck) progress = {
    value: state.projectCheck.current,
    max: state.projectCheck.total,
    label: text(`Проверки ${state.projectCheck.current} из ${state.projectCheck.total}`, `Checks ${state.projectCheck.current} of ${state.projectCheck.total}`),
  }
  if (state.localization) progress = {
    value: state.localization.checksStarted,
    max: state.localization.maxChecks,
    label: text(`Бюджет проверок ${state.localization.checksStarted} из ${state.localization.maxChecks}`, `Check budget ${state.localization.checksStarted} of ${state.localization.maxChecks}`),
  }
  if (state.reproduction) progress = {
    value: state.reproduction.current,
    max: state.reproduction.total,
    label: text(`Подтверждение ${state.reproduction.current} из ${state.reproduction.total}`, `Reproduction ${state.reproduction.current} of ${state.reproduction.total}`),
  }
  if (state.retry) progress = {
    value: state.retry.current,
    max: state.retry.total,
    label: text(`Попытка ${state.retry.current} из ${state.retry.total}`, `Attempt ${state.retry.current} of ${state.retry.total}`),
  }

  const percent = progress ? Math.min(100, Math.max(0, progress.value / Math.max(1, progress.max) * 100)) : undefined

  return (
    <section className={`run-monitor ${active ? 'active' : 'idle'}`} aria-live="polite">
      <div className="run-monitor-heading">
        <div>
          {active ? <LoaderCircle className="spin" size={15} /> : state.phase === 'finished' ? <CheckCircle2 size={15} /> : <Activity size={15} />}
          <strong>{text('Монитор выполнения', 'Run monitor')}</strong>
        </div>
        <span className="run-monitor-phase">{phase}</span>
      </div>

      {progress ? (
        <div className="run-monitor-progress">
          <div className="run-monitor-progress-copy"><span>{progress.label}</span><b>{Math.round(percent ?? 0)}%</b></div>
          <div className="run-monitor-track"><div style={{ width: `${percent}%` }} /></div>
        </div>
      ) : active ? <div className="run-monitor-track indeterminate"><div /></div> : null}

      <div className="run-monitor-facts">
        {state.dependency ? <div><span>{text('Сейчас', 'Current')}</span><strong title={state.dependency.name}>{state.dependency.name}</strong></div> : null}
        {state.solver?.componentsTotal ? <div><span>{text('Компоненты', 'Components')}</span><strong>{state.solver.componentsDone ?? 0}/{state.solver.componentsTotal}</strong></div> : null}
        {state.solver?.changed !== undefined ? <div><span>{text('Изменений', 'Changes')}</span><strong>{state.solver.changed}</strong></div> : null}
        {state.projectCheck ? <div><span>{text('Команда', 'Command')}</span><strong title={state.projectCheck.name}>{state.projectCheck.name}</strong></div> : null}
        {state.localization ? <div><span>{text('Сужение', 'Shrink')}</span><strong>{state.localization.shrinkHistory.join(' → ')}</strong></div> : null}
        {state.localization?.activeChecks !== undefined ? <div><span>{text('Активно', 'Active')}</span><strong>{state.localization.activeChecks}</strong></div> : null}
        {state.localization?.packages !== undefined ? <div><span>{text('Пакетов в наборе', 'Packages in set')}</span><strong>{state.localization.packages}</strong></div> : null}
        {state.reproduction?.literals !== undefined ? <div><span>{text('Проверяем literals', 'Testing literals')}</span><strong>{state.reproduction.literals}</strong></div> : null}
        {state.assignment ? <div><span>Assignment</span><strong title={state.assignment}>{state.assignment.slice(0, 12)}</strong></div> : null}
        {state.currentOperation ? <div><span>{text('Операция', 'Operation')}</span><strong title={state.currentOperation}>{state.currentOperation}</strong></div> : null}
        {duration ? <div><span>{text('Время операции', 'Operation time')}</span><strong>{duration}</strong></div> : null}
      </div>
    </section>
  )
}