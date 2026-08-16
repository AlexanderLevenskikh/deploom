import { Activity, CheckCircle2, CircleAlert, LoaderCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { deriveRunMonitor, type MonitorHealth, type RunMonitorPhase } from '../data/processMonitor'
import { latestJobId } from '../data/logPresentation'
import { useLanguage, type TranslationKey } from '../i18n'
import type { FlowAction, JobOutput, MigrationProgress } from '../types'

type Props = {
  logs: JobOutput[]
  active: boolean
  jobId?: string
  action?: FlowAction
  runStartedAt?: number
  migrationProgress?: MigrationProgress
}

function formatDuration(seconds: number | undefined, units: { hour: string; minute: string; second: string }): string | undefined {
  if (seconds === undefined) return undefined
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hours) return `${hours}${units.hour} ${minutes}${units.minute}`
  if (minutes) return `${minutes}${units.minute} ${secs}${units.second}`
  return `${secs}${units.second}`
}

function phaseKey(phase: RunMonitorPhase): TranslationKey {
  return `monitor.phase.${phase}` as TranslationKey
}

function healthKey(health: MonitorHealth): TranslationKey {
  return `monitor.health.${health}` as TranslationKey
}

export function RunMonitor({ logs, active, jobId, action, runStartedAt, migrationProgress }: Props) {
  const { t } = useLanguage()
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [active])

  const effectiveJobId = jobId ?? latestJobId(logs)
  const state = useMemo(
    () => deriveRunMonitor(logs, active, effectiveJobId, migrationProgress, action, now, runStartedAt),
    [action, active, effectiveJobId, logs, migrationProgress, now, runStartedAt],
  )

  const durationUnits = { hour: t('duration.hourShort'), minute: t('duration.minuteShort'), second: t('duration.secondShort') }
  const totalDuration = formatDuration(state.runElapsedSeconds, durationUnits)
  const attemptDuration = formatDuration(state.attemptElapsedSeconds, durationUnits)
  const stepDuration = formatDuration(state.stepElapsedSeconds, durationUnits)
  const signalAge = formatDuration(state.lastSignalAgeSeconds, durationUnits)
  const phase = t(phaseKey(state.phase))
  const health = state.health && signalAge ? t(healthKey(state.health), { age: signalAge }) : undefined

  const activitySummary = (() => {
    if (state.activity === 'scan' && state.dependency) return t('monitor.activity.scan', state.dependency)
    if (state.activity === 'planning') return t('monitor.activity.planning')
    if (state.activity === 'solving' || state.activity === 'searching-next') {
      const base = t(state.activity === 'searching-next' ? 'monitor.activity.searchingNext' : 'monitor.activity.solving')
      const limit = state.baseline?.allowedIterations ?? state.baseline?.maxIterations
      return state.baseline?.iteration && limit
        ? `${base} · ${t('monitor.iteration')} ${state.baseline.iteration}/${limit}`
        : base
    }
    if (state.activity === 'verifying-assignment') return t('monitor.activity.verifying')
    if (state.activity === 'confirming-exact') return t('monitor.activity.confirmingExact')
    if (state.activity === 'certifying-conflict') return t('monitor.activity.certifying')
    if (state.activity === 'minimizing-conflict') {
      const history = state.minimization?.shrinkHistory
      return history?.length
        ? t('monitor.activity.minimizingWithHistory', { history: history.join(' → ') })
        : t('monitor.activity.minimizing')
    }
    if (state.activity === 'project-check') return t('monitor.activity.projectCheck')
    if (state.activity === 'localizing' && state.localization) {
      return t('monitor.localizationSummary', { history: state.localization.shrinkHistory.join(' → ') })
    }
    if (state.activity === 'confirming-localized' && state.localization) {
      return t('monitor.confirmationSummary', { history: state.localization.shrinkHistory.join(' → ') })
    }
    if (state.activity === 'reproducing') return t('monitor.activity.reproducing')
    if (state.activity === 'retrying') return t('monitor.activity.retrying')
    if (state.activity === 'migrating') return t('monitor.activity.migrating')
    if (state.activity === 'repairing') return t('monitor.activity.repairing')
    if (state.activity === 'merging') return t('monitor.activity.merging')
    return state.currentOperation || (active ? t('monitor.activity.running') : undefined)
  })()

  // The retry counter is metadata, not the progress of the work performed by
  // that attempt. Live dependency/check/migration progress must win over 2/3.
  let progress: { value: number; max: number; label: string } | undefined
  if (state.dependency) {
    progress = {
      value: state.dependency.current,
      max: state.dependency.total,
      label: t('monitor.packages', { current: state.dependency.current, total: state.dependency.total }),
    }
  } else if (state.projectCheck) {
    progress = {
      value: state.projectCheck.current,
      max: state.projectCheck.total,
      label: t('monitor.checks', { current: state.projectCheck.current, total: state.projectCheck.total }),
    }
  } else if (state.reproduction) {
    progress = {
      value: state.reproduction.current,
      max: state.reproduction.total,
      label: t('monitor.reproduction', { current: state.reproduction.current, total: state.reproduction.total }),
    }
  } else if (state.migration?.totalBranches) {
    progress = {
      value: state.migration.completedBranches,
      max: state.migration.totalBranches,
      label: t('monitor.mergedGroups', { current: state.migration.completedBranches, total: state.migration.totalBranches }),
    }
  } else if (state.retry) {
    progress = {
      value: state.retry.current,
      max: state.retry.total,
      label: t('monitor.attempt', { current: state.retry.current, total: state.retry.total }),
    }
  }

  const percent = progress ? Math.min(100, Math.max(0, progress.value / Math.max(1, progress.max) * 100)) : undefined

  return (
    <section className={`run-monitor ${active ? 'active' : 'idle'}`} aria-live="polite">
      <div className="run-monitor-heading">
        <div>
          {active ? <LoaderCircle className="spin" size={15} /> : state.phase === 'finished' ? <CheckCircle2 size={15} /> : <Activity size={15} />}
          <strong>{t('monitor.title')}</strong>
        </div>
        <span className="run-monitor-phase">{phase}</span>
      </div>

      {active && health ? (
        <div className={`run-monitor-health ${state.health ?? 'healthy'}`}>
          {state.health === 'warning' || state.health === 'stale' ? <CircleAlert size={13} /> : <span className="run-monitor-health-dot" />}
          <strong>{health}</strong>
        </div>
      ) : null}

      {activitySummary ? <div className="run-monitor-summary">{activitySummary}</div> : null}

      {progress ? (
        <div className="run-monitor-progress">
          <div className="run-monitor-progress-copy"><span>{progress.label}</span><b>{Math.round(percent ?? 0)}%</b></div>
          <div className="run-monitor-track"><div style={{ width: `${percent}%` }} /></div>
        </div>
      ) : state.localization ? (
        <div className="run-monitor-localization">
          <div className="run-monitor-shrink">
            {state.localization.shrinkHistory.map((units, index) => <span key={`${units}-${index}`}>{index ? '→ ' : ''}{units}</span>)}
          </div>
          <small>{t('monitor.checkBudgetHint', { started: state.localization.checksStarted, max: state.localization.maxChecks })}</small>
        </div>
      ) : active ? <div className="run-monitor-track indeterminate"><div /></div> : null}

      <div className="run-monitor-facts">
        {state.retry ? <div><span>{t('monitor.attemptTime')}</span><strong>{state.retry.current}/{state.retry.total}{attemptDuration ? ` · ${attemptDuration}` : ''}</strong></div> : null}
        {state.baseline?.iteration ? <div><span>{t('monitor.iteration')}</span><strong>{state.baseline.iteration}{(state.baseline.allowedIterations ?? state.baseline.maxIterations) ? `/${state.baseline.allowedIterations ?? state.baseline.maxIterations}` : ''}</strong></div> : null}
        {state.baseline?.mode ? <div><span>{t('monitor.mode')}</span><strong>{state.baseline.mode}</strong></div> : null}
        {state.baseline?.hardIterations !== undefined ? <div><span>{t('monitor.hardBudget')}</span><strong>{state.baseline.hardIterations}</strong></div> : null}
        {state.baseline?.learnedConstraints !== undefined ? <div><span>{t('monitor.learnedConstraints')}</span><strong>{state.baseline.learnedConstraints}</strong></div> : null}
        {state.baseline?.certifiedExtensions !== undefined ? <div><span>{t('monitor.certifiedExtensions')}</span><strong>{state.baseline.certifiedExtensions}</strong></div> : null}
        {state.baseline?.exactExclusions !== undefined ? <div><span>{t('monitor.exactExclusions')}</span><strong>{state.baseline.exactExclusions}</strong></div> : null}
        {state.baseline?.solverManagedInputs !== undefined ? <div><span>{t('monitor.solverManagedInputs')}</span><strong>{state.baseline.solverManagedInputs}</strong></div> : null}
        {state.baseline?.fixedInputs !== undefined ? <div><span>{t('monitor.fixedInputs')}</span><strong>{state.baseline.fixedInputs}</strong></div> : null}
        {state.conflict?.candidate ? <div><span>{t('monitor.conflictCandidate')}</span><strong title={state.conflict.candidate}>{state.conflict.candidate}</strong></div> : null}
        {state.conflict?.literals !== undefined ? <div><span>{t('monitor.literals')}</span><strong>{state.conflict.literals}</strong></div> : null}
        {state.conflict?.literalBudget !== undefined ? <div><span>{t('monitor.literalBudget')}</span><strong>{state.conflict.literalBudget}</strong></div> : null}
        {state.conflict?.boundedSlice !== undefined ? <div><span>{t('monitor.boundedSlice')}</span><strong>{state.conflict.boundedSlice ? 'true' : 'false'}</strong></div> : null}
        {state.conflict?.seedSource ? <div><span>{t('monitor.seedSource')}</span><strong>{state.conflict.seedSource}</strong></div> : null}
        {state.minimization?.originalLiterals !== undefined ? <div><span>{t('monitor.minimization')}</span><strong>{state.minimization.originalLiterals}{state.minimization.minimizedLiterals !== undefined ? `→${state.minimization.minimizedLiterals}` : ''}</strong></div> : null}
        {state.minimization?.checks !== undefined ? <div><span>{t('monitor.minimizationChecks')}</span><strong>{state.minimization.checks}</strong></div> : null}
        {state.minimization?.acceptedShrinks !== undefined ? <div><span>{t('monitor.acceptedShrinks')}</span><strong>{state.minimization.acceptedShrinks}</strong></div> : null}
        {state.dependency ? <div><span>{t('common.current')}</span><strong title={state.dependency.name}>{state.dependency.name}</strong></div> : null}
        {state.solver?.componentsTotal ? <div><span>{t('monitor.components')}</span><strong>{state.solver.componentsDone ?? 0}/{state.solver.componentsTotal}</strong></div> : null}
        {state.solver?.changed !== undefined ? <div><span>{t('monitor.changes')}</span><strong>{state.solver.changed}</strong></div> : null}
        {state.projectCheck ? <div><span>{t('monitor.command')}</span><strong title={state.projectCheck.name}>{state.projectCheck.name}</strong></div> : null}
        {state.localization ? <div><span>{t('monitor.unitsLeft')}</span><strong>{state.localization.currentUnits}</strong></div> : null}
        {state.localization?.activeChecks !== undefined ? <div><span>{t('monitor.parallel')}</span><strong>{state.localization.activeChecks}</strong></div> : null}
        {state.localization?.packages !== undefined ? <div><span>{t('monitor.packagesInSet')}</span><strong>{state.localization.packages}</strong></div> : null}
        {state.localization ? <div><span>{t('monitor.checkBudget')}</span><strong>{state.localization.checksStarted}/{state.localization.maxChecks}</strong></div> : null}
        {state.migration?.label ? <div><span>{t('monitor.group')}</span><strong title={state.migration.branch}>{state.migration.label}</strong></div> : null}
        {state.migration?.branchPackages !== undefined ? <div><span>{t('monitor.groupTargets')}</span><strong>{state.migration.metPackages ?? 0}/{state.migration.branchPackages}</strong></div> : null}
        {state.migration ? <div><span>{t('monitor.mergedDependencies')}</span><strong>{state.migration.completedDependencies}/{state.migration.totalDependencies}</strong></div> : null}
        {state.migration?.readyBranches ? <div><span>{t('monitor.readyToMerge')}</span><strong>{state.migration.readyBranches}</strong></div> : null}
        {state.migration?.queuedBranches ? <div><span>{t('monitor.queued')}</span><strong>{state.migration.queuedBranches}</strong></div> : null}
        {state.migration?.failedBranches ? <div><span>{t('monitor.waitSupervisor')}</span><strong>{state.migration.failedBranches}</strong></div> : null}
        {totalDuration ? <div><span>{t('monitor.totalTime')}</span><strong>{totalDuration}</strong></div> : null}
        {stepDuration ? <div><span>{t('monitor.currentPhaseTime')}</span><strong>{stepDuration}</strong></div> : null}
        {signalAge ? <div><span>{t('monitor.lastSignal')}</span><strong>{t('monitor.ago', { value: signalAge })}</strong></div> : null}
      </div>
    </section>
  )
}
