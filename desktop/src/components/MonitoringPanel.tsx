import { AlertTriangle, ClipboardCopy, Cpu, Monitor, Pause, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { deriveRunMonitor } from '../data/processMonitor'
import { presentRunError } from '../data/errorPresentation'
import { latestJobId } from '../data/logPresentation'
import { useLanguage } from '../i18n'
import type { BaselineRecoveryInfo, EnvironmentInfo, FlowAction, HardwareSnapshot, JobOutput, JobOutputSource, MigrationProgress } from '../types'
import { LogPanel } from './LogPanel'
import { QuickSelect } from './QuickSelect'
import { RunMonitor } from './RunMonitor'

type View = 'overview' | 'run' | 'hardware' | 'logs' | 'errors' | 'environment'

type Props = {
  logs: JobOutput[]
  knownSources?: JobOutputSource[]
  environment: EnvironmentInfo
  active: boolean
  activeJobId?: string
  activeAction?: FlowAction
  runStartedAt?: number
  migrationProgress?: MigrationProgress
  baselineRecovery?: BaselineRecoveryInfo
  error?: string
  onDismissError: () => void
  onSendAgentNote: (note: string, branch?: string) => Promise<boolean>
  onPauseBaseline?: () => Promise<boolean>
  onCancel: () => void
  onClear: () => void
  getHardwareSnapshot: () => Promise<HardwareSnapshot>
}

function formatBytes(value: number | undefined): string {
  if (value === undefined) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let next = Math.max(0, value)
  let index = 0
  while (next >= 1024 && index < units.length - 1) {
    next /= 1024
    index += 1
  }
  return `${next.toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

function estimateProgress(state: ReturnType<typeof deriveRunMonitor>, active: boolean): number | undefined {
  if (!active && state.phase === 'finished') return 100
  if (!active) return undefined
  if (state.dependency) return Math.min(18, state.dependency.current / Math.max(1, state.dependency.total) * 18)
  if (state.phase === 'planning') return 22
  if (state.phase === 'solving') return 28
  if (state.phase === 'verifying') return state.projectCheck ? 42 + state.projectCheck.current / Math.max(1, state.projectCheck.total) * 18 : 38
  if (state.phase === 'localizing') return 60 + Math.min(25, (state.localization?.checksStarted ?? 0) / Math.max(1, state.localization?.maxChecks ?? 1) * 25)
  if (state.phase === 'confirming') return 88
  if (state.phase === 'migrating') return state.migration ? 40 + state.migration.completedBranches / Math.max(1, state.migration.totalBranches) * 55 : 45
  if (state.phase === 'merging') return 88
  if (state.phase === 'repairing') return 74
  return 32
}

function readableActivity(state: ReturnType<typeof deriveRunMonitor>, active: boolean, text: (ru: string, en: string) => string): string {
  if (!active && state.phase === 'finished') return text('Последний процесс завершён', 'Last run finished')
  if (!active) return text('Нет активного процесса', 'No active run')
  if (state.dependency) return text(`Анализирую зависимости: ${state.dependency.current}/${state.dependency.total}`, `Analyzing dependencies: ${state.dependency.current}/${state.dependency.total}`)
  if (state.activity === 'solving' || state.activity === 'searching-next') return text('Подбираю совместимое множество версий', 'Choosing a compatible version assignment')
  if (state.activity === 'verifying-assignment') return text('Проверяю assignment настоящим package manager', 'Verifying the assignment with the real package manager')
  if (state.activity === 'project-check') return text(`Запускаю проектную проверку${state.projectCheck?.name ? `: ${state.projectCheck.name}` : ''}`, `Running project check${state.projectCheck?.name ? `: ${state.projectCheck.name}` : ''}`)
  if (state.activity === 'localizing') return text('Сужаю конфликт до минимальной группы зависимостей', 'Narrowing the conflict to a minimal dependency set')
  if (state.activity === 'confirming-exact') return text('Подтверждаю результат точной проверкой', 'Confirming with an exact check')
  if (state.activity === 'migrating') return text('Выполняю migration plan', 'Executing the migration plan')
  if (state.activity === 'repairing') return text('Восстанавливаю recoverable-сбой', 'Recovering from a recoverable failure')
  return state.currentOperation || text('Процесс выполняется', 'Run is in progress')
}

export function MonitoringPanel({ logs, knownSources = [], environment, active, activeJobId, activeAction, runStartedAt, migrationProgress, baselineRecovery, error, onDismissError, onSendAgentNote, onPauseBaseline, onCancel, onClear, getHardwareSnapshot }: Props) {
  const { t, text } = useLanguage()
  const [view, setView] = useState<View>('overview')
  const [hardware, setHardware] = useState<HardwareSnapshot>()
  const [hardwareError, setHardwareError] = useState<string>()
  const lastAutoOpenedError = useRef<string | undefined>(undefined)
  const effectiveJobId = activeJobId ?? latestJobId(logs)
  const monitorState = useMemo(() => deriveRunMonitor(logs, active, effectiveJobId, migrationProgress, activeAction, Date.now(), runStartedAt), [active, activeAction, effectiveJobId, logs, migrationProgress, runStartedAt])
  const progress = estimateProgress(monitorState, active)
  const progressLabel = progress === undefined ? '—' : `${Math.round(progress)}%`
  const visibleError = useMemo(() => error ? presentRunError(error, text) : undefined, [error, text])
  useEffect(() => {
    if (!error) { lastAutoOpenedError.current = undefined; return }
    if (lastAutoOpenedError.current === error) return
    lastAutoOpenedError.current = error
    setView('errors')
  }, [error])

  useEffect(() => {
    if (view !== 'hardware') return
    let cancelled = false
    const load = async () => {
      try {
        const snapshot = await getHardwareSnapshot()
        if (!cancelled) {
          setHardware(snapshot)
          setHardwareError(undefined)
        }
      } catch (error) {
        if (!cancelled) setHardwareError(error instanceof Error ? error.message : String(error))
      }
    }
    void load()
    const timer = window.setInterval(load, 2_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [getHardwareSnapshot, view])

  return (
    <aside className="monitoring-panel">
      <div className="monitoring-heading">
        <div className="monitoring-heading-title"><Monitor size={17} /><strong>{text('Мониторинг', 'Monitoring')}</strong></div>
        <QuickSelect value={view} onChange={(value) => setView(value as View)} ariaLabel={text('Мониторинг', 'Monitoring')} options={[
          { value: 'overview', label: 'Default' },
          { value: 'run', label: text('Монитор выполнения', 'Run monitor') },
          { value: 'hardware', label: text('Железо', 'Hardware') },
          { value: 'logs', label: text('Логи', 'Logs') },
          { value: 'errors', label: error ? `⚠ ${text('Ошибки', 'Errors')} · 1` : text('Ошибки', 'Errors') },
          { value: 'environment', label: text('Окружение', 'Environment') },
        ]} />
      </div>

      <div className="monitoring-body">
        {view === 'overview' ? (
          <section className="monitoring-overview">
            <div className="monitoring-progress-card">
              <div className={`monitoring-ring ${active ? 'running' : ''}`} style={{ '--progress': progress ?? 0 } as CSSProperties}>
                <div><strong>{progressLabel}</strong><span>{text('Оценка прогресса', 'Estimated progress')}</span></div>
              </div>
              <div className="monitoring-summary">
                <strong>{readableActivity(monitorState, active, text)}</strong>
                <span>{progress === undefined ? text('Собираю оценку…', 'Calculating estimate…') : text('Оценочно, может меняться по мере поиска', 'Estimated, may change while search expands')}</span>
              </div>
              {active && activeAction === 'baseline' && onPauseBaseline ? <button className="button secondary" onClick={() => void onPauseBaseline()}><Pause size={15} /> {text('Пауза', 'Pause')}</button> : null}
            </div>
            <div className="monitoring-facts">
              {baselineRecovery?.available ? <div className="monitoring-fact"><span>{text('Последняя безопасная точка', 'Last safe checkpoint')}</span><strong>{text(`итерация ${baselineRecovery.iteration ?? 0} · ${baselineRecovery.status ?? 'unknown'}`, `iteration ${baselineRecovery.iteration ?? 0} · ${baselineRecovery.status ?? 'unknown'}`)}</strong></div> : null}
              <div className="monitoring-fact"><span>{text('Текущий этап', 'Current stage')}</span><strong>{activeAction ?? text('ожидание', 'idle')}</strong></div>
              <div className="monitoring-fact"><span>{text('Последний сигнал', 'Last signal')}</span><strong>{monitorState.lastSignalAgeSeconds !== undefined ? `${Math.floor(monitorState.lastSignalAgeSeconds)}s` : '—'}</strong></div>
            </div>
          </section>
        ) : null}

        {view === 'run' ? <RunMonitor logs={logs} active={active} jobId={activeJobId} action={activeAction} runStartedAt={runStartedAt} migrationProgress={migrationProgress} /> : null}
        {view === 'logs' ? <LogPanel logs={logs} knownSources={knownSources} environment={environment} active={active} activeJobId={activeJobId} activeAction={activeAction} runStartedAt={runStartedAt} migrationProgress={migrationProgress} onSendAgentNote={onSendAgentNote} onCancel={onCancel} onClear={onClear} showRunMonitor={false} showEnvironment={false} /> : null}
        {view === 'errors' ? (
          <section className="monitoring-errors">
            <div className="monitoring-errors-header"><div><AlertTriangle size={16} /><strong>{text('Ошибки выполнения', 'Run errors')}</strong></div>{error ? <span className="monitoring-error-count">1</span> : null}</div>
            {error ? <><div className="monitoring-error-card" role="alert"><pre>{visibleError}</pre></div><div className="monitoring-error-actions"><button className="button secondary" onClick={() => visibleError && void navigator.clipboard.writeText(visibleError)}><ClipboardCopy size={14} /> {text('Копировать', 'Copy')}</button><button className="button secondary" onClick={() => setView('logs')}>{text('Открыть логи', 'Open logs')}</button><button className="button secondary" onClick={onDismissError}><X size={14} /> {text('Закрыть ошибку', 'Dismiss error')}</button></div></> : <div className="monitoring-errors-empty">{text('Ошибок выполнения нет', 'No run errors')}</div>}
          </section>
        ) : null}
        {view === 'environment' ? <div className="environment-only">{Object.entries(environment).map(([name, info]) => <div className="environment-row" key={name}><span className={`status-dot ${info.available ? 'success' : 'danger'}`} /><strong>{name}</strong><span>{info.available ? info.version : t('log.notFound')}</span></div>)}</div> : null}
        {view === 'hardware' ? (
          <section className="hardware-grid">
            <div className="monitoring-fact"><span>{text('Опрос включён только на этой вкладке', 'Sampling runs only on this tab')}</span><strong>{hardware?.capturedAt ? new Date(hardware.capturedAt).toLocaleTimeString() : hardwareError ?? '—'}</strong></div>
            <div className="hardware-card"><span>CPU</span><strong><Cpu size={14} /> {hardware?.cpu.loadPct !== undefined ? `${hardware.cpu.loadPct.toFixed(0)}%` : '—'} · {hardware?.cpu.logicalCores ?? '—'} cores</strong></div>
            <div className="hardware-card"><span>{text('Память', 'Memory')}</span><strong>{hardware ? `${formatBytes(hardware.memory.usedBytes)} / ${formatBytes(hardware.memory.totalBytes)} · ${hardware.memory.usedPct.toFixed(0)}%` : '—'}</strong></div>
            <div className="hardware-card"><span>DepLoom</span><strong>{hardware ? `${formatBytes(hardware.process.memoryBytes)}${hardware.process.cpuPct !== undefined ? ` · ${hardware.process.cpuPct.toFixed(1)}% CPU` : ''}` : '—'}</strong></div>
          </section>
        ) : null}
      </div>
    </aside>
  )
}
