import { Activity, CheckCircle2, CircleAlert, LoaderCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { deriveRunMonitor, type MonitorHealth, type RunMonitorPhase } from '../data/processMonitor'
import { latestJobId } from '../data/logPresentation'
import { useLanguage } from '../i18n'
import type { FlowAction, JobOutput, MigrationProgress } from '../types'

type Props = {
  logs: JobOutput[]
  active: boolean
  jobId?: string
  action?: FlowAction
  migrationProgress?: MigrationProgress
}

const PHASE_COPY: Record<RunMonitorPhase, { ru: string; en: string }> = {
  idle: { ru: '??? ???????? ????????', en: 'No active operation' },
  scan: { ru: '?????? ????????????', en: 'Dependency analysis' },
  planning: { ru: '?????????? ?????', en: 'Building plan' },
  solving: { ru: '??????? ???????????', en: 'Solving constraints' },
  verifying: { ru: '???????? ???????', en: 'Project verification' },
  localizing: { ru: '??????????? ?????????', en: 'Conflict localization' },
  confirming: { ru: '????????????? ?????????? ??????', en: 'Confirming localized set' },
  reproducing: { ru: '????????????? ???????', en: 'Reproducing culprit' },
  retrying: { ru: '????????? ???????', en: 'Retrying' },
  migrating: { ru: '????????', en: 'Migration' },
  repairing: { ru: '??????????? ??????', en: 'Repairing group' },
  merging: { ru: 'Merge', en: 'Merge' },
  running: { ru: '??????????', en: 'Running' },
  finished: { ru: '????????? ???????? ?????????', en: 'Last operation finished' },
}

const RUNTIME_COPY: Record<string, { ru: string; en: string }> = {
  planning: { ru: 'Supervisor ?????????', en: 'Supervisor planning' },
  queued: { ru: '? ???????', en: 'Queued' },
  starting: { ru: '??????????? agent-????', en: 'Opening agent slot' },
  running: { ru: '????? ????????', en: 'Agent working' },
  bootstrapping: { ru: '?????????? ?????????', en: 'Preparing environment' },
  verifying: { ru: '???????? ??????', en: 'Verifying group' },
  repairing: { ru: '??????????? ????? ????????', en: 'Repairing after verification' },
  failed: { ru: '??????? Supervisor', en: 'Waiting for Supervisor' },
  ready: { ru: '?????? ? merge', en: 'Ready to merge' },
  merging: { ru: '??????????? merge', en: 'Merging' },
  'integration-verifying': { ru: '???????? merged', en: 'Verifying merged branch' },
}

function formatDuration(seconds: number | undefined, language: 'ru' | 'en'): string | undefined {
  if (seconds === undefined) return undefined
  const total = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  if (hours) return language === 'ru' ? `${hours} ? ${minutes} ???` : `${hours}h ${minutes}m`
  if (minutes) return language === 'ru' ? `${minutes} ??? ${secs} ?` : `${minutes}m ${secs}s`
  return language === 'ru' ? `${secs} ?` : `${secs}s`
}

function healthCopy(health: MonitorHealth | undefined, age: string | undefined, language: 'ru' | 'en') {
  if (!health || !age) return undefined
  if (health === 'healthy') return language === 'ru' ? `???????? ? ?????? ${age} ?????` : `Working ? signal ${age} ago`
  if (health === 'quiet') return language === 'ru' ? `?????? ???????? ? ?????? ${age} ?????` : `Long operation ? signal ${age} ago`
  if (health === 'warning') return language === 'ru' ? `????? ??? ????? ????????? ? ${age}` : `No new messages for ${age}`
  return language === 'ru' ? `??? ????? ???????? ${age} ? watchdog ?????????? ????????` : `No signal for ${age} ? watchdog is still monitoring`
}

export function RunMonitor({ logs, active, jobId, action, migrationProgress }: Props) {
  const { language, text } = useLanguage()
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [active])

  const effectiveJobId = jobId ?? latestJobId(logs)
  const state = useMemo(
    () => deriveRunMonitor(logs, active, effectiveJobId, migrationProgress, action, now),
    [action, active, effectiveJobId, logs, migrationProgress, now],
  )
  const duration = formatDuration(state.elapsedSeconds, language)
  const stepDuration = formatDuration(state.stepElapsedSeconds, language)
  const signalAge = formatDuration(state.lastSignalAgeSeconds, language)
  const phase = PHASE_COPY[state.phase][language]
  const health = healthCopy(state.health, signalAge, language)

  let progress: { value: number; max: number; label: string } | undefined
  if (state.dependency) progress = {
    value: state.dependency.current,
    max: state.dependency.total,
    label: text(`?????? ${state.dependency.current} ?? ${state.dependency.total}`, `Packages ${state.dependency.current} of ${state.dependency.total}`),
  }
  if (state.projectCheck) progress = {
    value: state.projectCheck.current,
    max: state.projectCheck.total,
    label: text(`???????? ${state.projectCheck.current} ?? ${state.projectCheck.total}`, `Checks ${state.projectCheck.current} of ${state.projectCheck.total}`),
  }
  if (state.reproduction) progress = {
    value: state.reproduction.current,
    max: state.reproduction.total,
    label: text(`????????????? ${state.reproduction.current} ?? ${state.reproduction.total}`, `Reproduction ${state.reproduction.current} of ${state.reproduction.total}`),
  }
  if (state.retry) progress = {
    value: state.retry.current,
    max: state.retry.total,
    label: text(`??????? ${state.retry.current} ?? ${state.retry.total}`, `Attempt ${state.retry.current} of ${state.retry.total}`),
  }
  if (state.migration?.totalBranches) progress = {
    value: state.migration.completedBranches,
    max: state.migration.totalBranches,
    label: text(
      `? merged ${state.migration.completedBranches} ?? ${state.migration.totalBranches} ?????`,
      `Merged ${state.migration.completedBranches} of ${state.migration.totalBranches} groups`,
    ),
  }

  const percent = progress ? Math.min(100, Math.max(0, progress.value / Math.max(1, progress.max) * 100)) : undefined
  const runtimeLabel = state.migration?.runtimePhase ? RUNTIME_COPY[state.migration.runtimePhase]?.[language] : undefined

  const summary = state.localization
    ? state.phase === 'confirming'
      ? text(
          `???????????? ????????? ????? ???????????????. ???????: ${state.localization.shrinkHistory.join(' ? ')} ?????.`,
          `Confirming the localized set serially. Shrink: ${state.localization.shrinkHistory.join(' ? ')} groups.`,
        )
      : text(
          `???? ??????????? ????????. ??? ??????: ${state.localization.shrinkHistory.join(' ? ')} ?????.${state.localization.activeChecks ? ` ?????? ??????????? ??????????? ${state.localization.activeChecks} ????????.` : ''}`,
          `Finding the minimal conflict. Shrunk: ${state.localization.shrinkHistory.join(' ? ')} groups.${state.localization.activeChecks ? ` ${state.localization.activeChecks} candidates are running in parallel.` : ''}`,
        )
    : state.migration
      ? text(
          `????????: ${state.migration.completedBranches} ?? ${state.migration.totalBranches} ????? ??? ? merged.${state.migration.label ? ` ??????: ${state.migration.label}${runtimeLabel ? ` ? ${runtimeLabel}` : ''}.` : ''}`,
          `Migration: ${state.migration.completedBranches} of ${state.migration.totalBranches} groups are merged.${state.migration.label ? ` Current: ${state.migration.label}${runtimeLabel ? ` ? ${runtimeLabel}` : ''}.` : ''}`,
        )
      : state.currentOperation

  return (
    <section className={`run-monitor ${active ? 'active' : 'idle'}`} aria-live="polite">
      <div className="run-monitor-heading">
        <div>
          {active ? <LoaderCircle className="spin" size={15} /> : state.phase === 'finished' ? <CheckCircle2 size={15} /> : <Activity size={15} />}
          <strong>{text('??????? ??????????', 'Run monitor')}</strong>
        </div>
        <span className="run-monitor-phase">{phase}</span>
      </div>

      {active && health ? (
        <div className={`run-monitor-health ${state.health ?? 'healthy'}`}>
          {state.health === 'warning' || state.health === 'stale' ? <CircleAlert size={13} /> : <span className="run-monitor-health-dot" />}
          <strong>{health}</strong>
        </div>
      ) : null}

      {summary ? <div className="run-monitor-summary">{summary}</div> : null}

      {progress ? (
        <div className="run-monitor-progress">
          <div className="run-monitor-progress-copy"><span>{progress.label}</span><b>{Math.round(percent ?? 0)}%</b></div>
          <div className="run-monitor-track"><div style={{ width: `${percent}%` }} /></div>
        </div>
      ) : state.localization ? (
        <div className="run-monitor-localization">
          <div className="run-monitor-shrink">
            {state.localization.shrinkHistory.map((units, index) => <span key={`${units}-${index}`}>{index ? '? ' : ''}{units}</span>)}
          </div>
          <small>{text(
            `?????? ????????: ${state.localization.checksStarted}/${state.localization.maxChecks} ? ??? ????? ??????, ?? ??????? ??????????`,
            `Check budget: ${state.localization.checksStarted}/${state.localization.maxChecks} ? this is a search limit, not completion percent`,
          )}</small>
        </div>
      ) : active ? <div className="run-monitor-track indeterminate"><div /></div> : null}

      <div className="run-monitor-facts">
        {state.dependency ? <div><span>{text('??????', 'Current')}</span><strong title={state.dependency.name}>{state.dependency.name}</strong></div> : null}
        {state.solver?.componentsTotal ? <div><span>{text('??????????', 'Components')}</span><strong>{state.solver.componentsDone ?? 0}/{state.solver.componentsTotal}</strong></div> : null}
        {state.solver?.changed !== undefined ? <div><span>{text('?????????', 'Changes')}</span><strong>{state.solver.changed}</strong></div> : null}
        {state.projectCheck ? <div><span>{text('???????', 'Command')}</span><strong title={state.projectCheck.name}>{state.projectCheck.name}</strong></div> : null}
        {state.localization ? <div><span>{text('???????? ?????', 'Units left')}</span><strong>{state.localization.currentUnits}</strong></div> : null}
        {state.localization?.activeChecks !== undefined ? <div><span>{text('???????????', 'Parallel')}</span><strong>{state.localization.activeChecks}</strong></div> : null}
        {state.localization?.packages !== undefined ? <div><span>{text('??????? ? ??????', 'Packages in set')}</span><strong>{state.localization.packages}</strong></div> : null}
        {state.localization ? <div><span>{text('?????? checks', 'Check budget')}</span><strong>{state.localization.checksStarted}/{state.localization.maxChecks}</strong></div> : null}
        {state.reproduction?.literals !== undefined ? <div><span>{text('????????? literals', 'Testing literals')}</span><strong>{state.reproduction.literals}</strong></div> : null}
        {state.migration?.label ? <div><span>{text('??????? ??????', 'Current group')}</span><strong title={state.migration.branch}>{state.migration.label}</strong></div> : null}
        {state.migration?.branchPackages !== undefined ? <div><span>{text('???? ??????', 'Group targets')}</span><strong>{state.migration.metPackages ?? 0}/{state.migration.branchPackages}</strong></div> : null}
        {state.migration ? <div><span>{text('??????????? merged', 'Merged dependencies')}</span><strong>{state.migration.completedDependencies}/{state.migration.totalDependencies}</strong></div> : null}
        {state.migration?.readyBranches ? <div><span>{text('?????? ? merge', 'Ready to merge')}</span><strong>{state.migration.readyBranches}</strong></div> : null}
        {state.migration?.queuedBranches ? <div><span>{text('? ???????', 'Queued')}</span><strong>{state.migration.queuedBranches}</strong></div> : null}
        {state.migration?.failedBranches ? <div><span>{text('???? Supervisor', 'Waiting for Supervisor')}</span><strong>{state.migration.failedBranches}</strong></div> : null}
        {state.assignment ? <div><span>Assignment</span><strong title={state.assignment}>{state.assignment.slice(0, 12)}</strong></div> : null}
        {duration ? <div><span>{text('????? localization', 'Localization total')}</span><strong>{duration}</strong></div> : null}
        {stepDuration ? <div><span>{text('??????? ???', 'Current step')}</span><strong>{stepDuration}</strong></div> : null}
        {signalAge ? <div><span>{text('????????? ??????', 'Last signal')}</span><strong>{text(`${signalAge} ?????`, `${signalAge} ago`)}</strong></div> : null}
      </div>
    </section>
  )
}
