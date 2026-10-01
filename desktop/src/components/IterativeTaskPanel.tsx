import { Check, Clipboard, ExternalLink, FileText, Forward, RefreshCw, Save, Wrench, X, Rocket } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useLanguage } from '../i18n'
import type { IterativeAgentOutcome, IterativeAttemptView, IterativeBeginOutcome, IterativeDriveOutcome, IterativeStatusOutcome, IterativeTaskActionOutcome, IterativeTaskSnapshot } from '../types'

type Props = {
  workspaceId?: string
  projectName: string
  refreshKey?: string
  onGet: (projectName: string) => Promise<IterativeTaskSnapshot>
  onExport: (projectName: string) => Promise<IterativeTaskActionOutcome>
  onExportLegacy: (projectName: string) => Promise<IterativeTaskActionOutcome>
  onCopy: (projectName: string, language?: string) => Promise<IterativeTaskActionOutcome>
  onSave: (projectName: string, language?: string) => Promise<IterativeTaskActionOutcome>
  onStatus: (projectName: string) => Promise<IterativeStatusOutcome>
  onDrive: (projectName: string) => Promise<IterativeDriveOutcome>
  onBegin: (projectName: string, discovery?: { mode: 'auto' | 'none'; timeoutSeconds?: number; parallelism?: number; maxPackages?: number }) => Promise<IterativeBeginOutcome>
  onAgent: (projectName: string) => Promise<IterativeAgentOutcome>
  onAttempt: (projectName: string) => Promise<{ ok: boolean; present: boolean; attempt?: IterativeAttemptView; attemptLog?: string; error?: string }>
  onCancel: (projectName: string) => Promise<{ ok: boolean }>
  liveAttempt?: IterativeAttemptView
  onConfigureScope?: () => void
  onOpenPath: (path?: string) => Promise<void>
}

type PanelState =
  | { phase: 'loading' }
  | { phase: 'ready'; snapshot: IterativeTaskSnapshot }
  | { phase: 'error'; message: string }

const stageLabel = (stage?: string, text?: (ru: string, en: string) => string) => {
  const t = text ?? ((ru: string) => ru)
  switch (stage) {
    case 'preflight': return t('предварительная проверка', 'preflight')
    case 'begin': return t('C0-старт', 'begin')
    case 'drive': return t('супервизор', 'supervisor')
    case 'agent': return t('агент', 'agent')
    case 'none': return t('—', '—')
    default: return stage
  }
}

const fmtElapsed = (ms: number): string => {
  const total = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

export function IterativeTaskPanel({ projectName, refreshKey, onGet, onExport, onCopy, onSave, onStatus, onDrive, onBegin, onExportLegacy, onAgent, onAttempt, onCancel, liveAttempt, onConfigureScope, onOpenPath }: Props) {
  const { text, language } = useLanguage()
  const [state, setState] = useState<PanelState>({ phase: 'loading' })
  const [busy, setBusy] = useState<string>()
  const [copied, setCopied] = useState(false)
  const [note, setNote] = useState<string>()
  const [showDialog, setShowDialog] = useState(false)
  const [runner, setRunner] = useState<IterativeStatusOutcome>()
  const [runnerError, setRunnerError] = useState<string>()
  const [stepBusy, setStepBusy] = useState(false)
  // L1: durable attempt journal — seeded from the IPC read, then merged with the
  // live event stream from main.ts so the first click is observable without a poll.
  const [attempt, setAttempt] = useState<IterativeAttemptView>()
  const [attemptLog, setAttemptLog] = useState<string>()
  const [showLog, setShowLog] = useState(false)
  const [noTargetsStep, setNoTargetsStep] = useState(false)
  const [nowTick, setNowTick] = useState(Date.now())
  const loadSeq = useRef(0)

  // D2.4: display-only view of the durable runtime contract — requested vs
  // effective + the CI evidence source (installed runtime that committed).
  const runtimeLine = (runner?: IterativeStatusOutcome) => {
    const rt = runner?.runtime
    if (!runner?.present || !rt?.effectiveVersion) return null
    const manager = [rt.packageManager, rt.packageManagerVersion].filter(Boolean).join(' ')
    const platform = [rt.platform, rt.arch].filter(Boolean).join('/')
    const evidence = [rt.source, manager, platform].filter(Boolean).join(' · ')
    return (
      <span>
        {text('Node/CI', 'Node/CI')}: {rt.requested || text('не задан', 'unset')} → {rt.effectiveVersion}
        {evidence ? ` · ${evidence}` : ''}
      </span>
    )
  }

  // L4: never swallow a status error. Keep the last good runner AND remember the
  // failure reason so the UI shows it next to the action buttons.
  const refreshRunner = useCallback(async () => {
    try {
      const status = await onStatus(projectName)
      setRunner(status)
      setRunnerError(status.ok || !status.error ? undefined : status.error)
      return status
    } catch (error) {
      setRunnerError(error instanceof Error ? error.message : String(error))
      return undefined
    }
  }, [onStatus, projectName])

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    setState({ phase: 'loading' })
    try {
      const snapshot = await onGet(projectName)
      if (loadSeq.current !== seq) return
      setState({ phase: 'ready', snapshot })
    } catch (error) {
      if (loadSeq.current !== seq) return
      setState({ phase: 'error', message: error instanceof Error ? error.message : String(error) })
    }
  }, [onGet, projectName])

  useEffect(() => {
    void load()
    void refreshRunner()
  }, [load, refreshRunner, refreshKey])

  // L1: the journal survives a Desktop restart — re-seed from disk on mount.
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const result = await onAttempt(projectName)
        if (!alive) return
        if (result.attempt) {
          setAttempt(result.attempt)
          if (result.attemptLog) setAttemptLog(result.attemptLog)
        }
        if (result.attempt?.lastStep === 'no-targets' || result.attempt?.reason?.startsWith('no roadmap targets')) setNoTargetsStep(true)
      } catch {
        // journal read is best-effort; the live event stream still updates us
      }
    })()
    return () => { alive = false }
  }, [onAttempt, projectName, refreshKey])

  // L1: live event stream carries the newest journal snapshot for THIS project.
  useEffect(() => {
    if (!liveAttempt || liveAttempt.projectName !== projectName) return
    setAttempt(liveAttempt)
    if (liveAttempt.reason?.startsWith('no roadmap targets') || liveAttempt.lastStep === 'no-targets') setNoTargetsStep(true)
  }, [liveAttempt, projectName])

  // Elapsed counter while an attempt is alive.
  const attemptAlive = Boolean(attempt && (attempt.status === 'starting' || attempt.status === 'running'))
  useEffect(() => {
    if (!attemptAlive) return
    const timer = window.setInterval(() => setNowTick(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [attemptAlive, attempt?.status, attempt?.attemptId])

  // Review re-check P1#4: a durable 'running' attempt restored after a restart
  // has NO live process behind it — main.ts re-spawns the child only on the
  // NEXT drive. Cancel can only reach a child created by an IN-FLIGHT
  // begin/drive call of THIS panel, so it is gated on `stepBusy` (honest
  // liveness), never on the journal status alone.
  const childAlive = stepBusy

  // Review re-check P2#5: while the output log is open and a run is actively
  // emitting, re-read the journal tail periodically so the user sees live
  // progress even though the child's stdout only lands in the journal.
  useEffect(() => {
    if (!showLog || !(attemptAlive || stepBusy)) return
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const result = await onAttempt(projectName)
          if (result.attemptLog) setAttemptLog(result.attemptLog)
          if (result.attempt) setAttempt(result.attempt)
        } catch { /* diagnostics best-effort */ }
      })()
    }, 2000)
    return () => window.clearInterval(timer)
  }, [showLog, attemptAlive, stepBusy, onAttempt, projectName])

  const run = async (kind: string, action: () => Promise<IterativeTaskActionOutcome>) => {
    setBusy(kind)
    setNote(undefined)
    try {
      const outcome = await action()
      if (outcome.ok) {
        if (kind === 'copy') {
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1600)
        }
        setNote(outcome.path ?? (outcome.artifactId ? `${outcome.artifactId}` : undefined))
        if (kind === 'export') void load()
      } else {
        setNote(outcome.error ?? 'Ошибка')
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(undefined)
    }
  }

  // #1: the durable supervisor drives plan-next -> materialize -> precheck ->
  // verify-exact -> next cohort WITHOUT manual internal steps, stopping ONLY
  // at an agent gate, finish, error or budget. Restart-safe: every iteration
  // re-reads the durable Python state.
  const driveNow = async () => {
    setStepBusy(true)
    setNote(undefined)
    try {
      const outcome = await onDrive(projectName)
      if (!outcome.ok) {
        setNote(outcome.error ?? 'Ошибка')
      } else if (outcome.stopped === 'agent-gate') {
        setNote(
          text(
            `Пауза на агенте: ${outcome.reason ?? 'нужен ремонт'}${outcome.steps.length ? ` · выполнено шагов: ${outcome.steps.length}` : ''}`,
            `Agent gate: ${outcome.reason ?? 'repair needed'}${outcome.steps.length ? ` · steps done: ${outcome.steps.length}` : ''}`,
          ),
        )
      } else if (outcome.stopped === 'canceled') {
        setNote(text(`Остановлено пользователем. ${outcome.reason ?? 'verified checkpoint сохранён; можно продолжить позже.'}`, `Canceled by user. ${outcome.reason ?? 'verified checkpoint kept; you can continue later.'}`))
      } else if (outcome.stopped === 'finished') {
        setNote(text('Миграция завершена: финальные отчёты сформированы.', 'Migration finished: final reports written.'))
      } else {
        setNote(
          text(
            `Супервизор остановился: ${outcome.reason ?? ''}`,
            `Supervisor stopped: ${outcome.reason ?? ''}`,
          ),
        )
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setStepBusy(false)
      void refreshRunner()
      void load()
    }
  }

  // L2: begin with the EXPLICIT path. Default is roadmap-only ('none') so an
  // empty target map is reported honestly (noTargets) instead of running the
  // long sequential registry discovery inside a blocking IPC.
  const beginNow = async (discovery?: { mode: 'auto' | 'none'; timeoutSeconds?: number; parallelism?: number; maxPackages?: number }) => {
    setStepBusy(true)
    setNote(undefined)
    try {
      const outcome = await onBegin(projectName, discovery)
      if (outcome.ok) {
        if (outcome.noTargets) {
          setNoTargetsStep(true)
          setNote(
            text(
              'Сохранённых целей roadmap нет — прогон не создан. Выберите: настроить состав (Draft) или ограниченный авто-поиск.',
              'No saved roadmap targets — no run created. Choose: configure scope (Draft) or bounded auto-discovery.',
            ),
          )
        } else {
          setNoTargetsStep(false)
          setNote(
            text(
              `Начат прогон: C0${outcome.targetsCount !== undefined ? `, целей из roadmap: ${outcome.targetsCount}` : ''}`,
              `Run started: C0${outcome.targetsCount !== undefined ? `, roadmap targets: ${outcome.targetsCount}` : ''}`,
            ),
          )
          // Continuous migration: keep driving until the first gate/finish.
          await driveNow()
        }
      } else if ((outcome.error ?? '').startsWith('RUN_ALREADY_EXISTS')) {
        setNote(text('Прогон уже существует — продолжите с его состояния.', 'A run already exists — continue from its state.'))
        void refreshRunner()
      } else {
        setNote(outcome.error ?? 'Ошибка')
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setStepBusy(false)
      void refreshRunner()
      void load()
    }
  }

  // L1: user cancel is cooperative — main.ts polls cancelRequested every 500ms
  // and stops the current spawn; the durable run + verified checkpoint survive.
  const cancelNow = async () => {
    setNote(undefined)
    try {
      await onCancel(projectName)
      setNote(text('Отправлен запрос остановки…', 'Cancel requested…'))
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    }
  }

  // R9: import the user's OLD saved Baseline result (dashboard-state JSON)
  // into the shared task contract with the CURRENT builder — no rerun, no
  // invented proof (verification stays "legacy-exported-not-reverified").
  const exportLegacyNow = async () => {
    setStepBusy(true)
    setNote(undefined)
    try {
      const outcome = await onExportLegacy(projectName)
      if (outcome.ok) {
        setNote(
          text(
            `Импортировано из сохранённого результата: ${outcome.artifactId ?? ''}`,
            `Imported from the saved result: ${outcome.artifactId ?? ''}`,
          ),
        )
      } else {
        setNote(outcome.error ?? 'Ошибка')
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setStepBusy(false)
      void refreshRunner()
      void load()
    }
  }

  const agentNow = async () => {
    setStepBusy(true)
    setNote(undefined)
    try {
      const outcome = await onAgent(projectName)
      if (outcome.ok) {
        const changed = (outcome.changedFiles ?? []).length
        setNote(
          text(
            `Агент исправил ${changed} файлов${outcome.next ? ` · далее: ${outcome.next.step ?? '—'}` : ''}`,
            `Agent repaired ${changed} file(s)${outcome.next ? ` · next: ${outcome.next.step ?? '—'}` : ''}`,
          ),
        )
        // Continuous migration: after the repair is accepted, the supervisor
        // verifies the exact candidate and moves to the next cohort on its own.
        await driveNow()
      } else {
        setNote(outcome.error ?? 'Ошибка')
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setStepBusy(false)
      void refreshRunner()
      void load()
    }
  }

  const toggleLog = async () => {
    // Re-read the journal tail on every OPEN so the freshly appended lines of a
    // running child are shown immediately; while open, the interval effect keeps
    // it current.
    if (showLog || attemptLog === undefined) {
      try {
        const result = await onAttempt(projectName)
        if (result.attemptLog) setAttemptLog(result.attemptLog)
      } catch {
        // diagnostics are best-effort
      }
    }
    setShowLog((current) => !current)
  }

  const snapshot = state.phase === 'ready' ? state.snapshot : undefined
  const task = snapshot?.task
  const insufficient = Boolean(snapshot && !snapshot.present && snapshot.missing.length > 0)

  const attemptFailure = attempt && (attempt.status === 'failed' || attempt.status === 'canceled')
    ? (attempt.lastError || attempt.reason || undefined)
    : undefined
  const elapsedMs = attempt?.startedAt && nowTick > attempt.startedAt ? nowTick - attempt.startedAt : 0
  const progress = attempt?.packageProgress
  const showLogTail = showLog && attemptLog

  return (
    <section className="roadmap-card" data-testid="iterative-task-panel">
      <header className="roadmap-card-header">
        <div>
          <strong>{text('План обновления (ТЗ для миграции)', 'Update assignment (migration task)')}</strong>
          <span>
            {text(
              'Точные версии принятых обновлений и явные отложенные пакеты. Формируется из verified checkpoint, без запуска Baseline.',
              'Exact accepted update versions and explicit deferred packages. Built from the verified checkpoint, no Baseline run.',
            )}
          </span>
        </div>
        <button type="button" className="icon-button" aria-label={text('Обновить', 'Reload')} onClick={() => { void load(); void refreshRunner() }} disabled={busy === 'load'}><RefreshCw size={16} /></button>
      </header>

      {/* L1: the durable attempt strip — visible from the very first click, after
          a restart, and during a long preflight/discovery/cohort. */}
      {attempt ? (
        <div className={`attempt-strip ${attempt.status}`} data-testid="attempt-strip">
          <div className="attempt-strip-row">
            <strong className={`attempt-status ${attempt.status}`}>{text('Попытка', 'Attempt')}: {attempt.status}</strong>
            <span className="attempt-meta">
              {stageLabel(attempt.stage, text)} · {text('этап', 'stage')} {attempt.stage === 'begin' || attempt.stage === 'drive' ? attempt.phase ?? '—' : ''}
              · {text('прошло', 'elapsed')} {fmtElapsed(elapsedMs)}
              {attempt.targetSource === 'roadmap'
                ? ` · ${text('цели: roadmap', 'targets: roadmap')}`
                : attempt.targetSource === 'discovery'
                  ? ` · ${text('цели: авто-поиск', 'targets: auto-discovery')}`
                  : ` · ${text('цели: нет', 'no targets')}`}
            </span>
            {attempt && childAlive ? (
              <button type="button" className="button danger" data-testid="iterative-cancel" onClick={() => void cancelNow()}>
                <X size={15} />{text('Остановить', 'Cancel')}
              </button>
            ) : null}
          </div>
          <div className="attempt-strip-row">
            {attempt.discovery ? (
              <span className="attempt-meta">
                {text('бюджет поиска', 'discovery budget')}: {attempt.discovery.parallelism} {text('потоков', 'threads')} · {attempt.discovery.timeoutSeconds}с · {text('макс', 'max')} {attempt.discovery.maxPackages} {text('пакетов', 'packages')}
              </span>
            ) : null}
            {progress && progress.total > 0 ? (
              <span className="attempt-meta">
                {text('прогресс', 'progress')}: {progress.processed}/{progress.total}
                <span className="attempt-progress"><span style={{ width: `${Math.min(100, Math.round((progress.processed / progress.total) * 100))}%` }} /></span>
              </span>
            ) : null}
            {attempt.stepsDone.length > 0 ? (
              <span className="attempt-meta">{text('шаги', 'steps')}: {attempt.stepsDone.join(' → ')}</span>
            ) : null}
            {attempt.reason ? (
              <span className="attempt-meta attempt-reason">{attempt.reason}</span>
            ) : null}
            {attempt.status === 'running' && !childAlive ? (
              <span className="attempt-meta attempt-reason">
                {text('Попытка восстановлена после перезапуска; супервизор не запущен — нажмите «Продолжить».', 'Attempt restored after restart; the supervisor is not running — press «Continue».')}
              </span>
            ) : null}
            {attempt ? (
              <button type="button" className="linklike" onClick={() => void toggleLog()}>
                {showLog ? text('скрыть вывод', 'hide output') : text('показать вывод', 'show output')}
              </button>
            ) : null}
          </div>
          {showLogTail ? (
            <pre className="attempt-log">{attemptLog}</pre>
          ) : null}
        </div>
      ) : null}

      {state.phase === 'loading' ? (
        <div className="resume-notice"><span>{text('Загружаю задание…', 'Loading the assignment…')}</span></div>
      ) : state.phase === 'error' ? (
        <div className="resume-notice danger">
          <strong>{text('Не удалось прочитать задание', 'Could not read the assignment')}</strong>
          <span>{state.message}</span>
        </div>
      ) : !task ? (
        <>
          {/* L4: begin/drive/agent failures are visible in the pre-run branch too.
              RUN_ALREADY_EXISTS pulls the user toward «Продолжить» below. */}
          {attemptFailure ? (
            <div className="resume-notice danger">
              <strong>{text(attempt?.status === 'canceled' ? 'Попытка остановлена' : 'Попытка завершилась ошибкой', attempt?.status === 'canceled' ? 'Attempt canceled' : 'Attempt failed')}</strong>
              <span>{attemptFailure}</span>
              {attempt?.status === 'canceled' ? <span>{text('Verified checkpoint не тронут — можно продолжить позже.', 'The verified checkpoint was kept — you can continue later.')}</span> : null}
            </div>
          ) : null}
          {runnerError ? (
            <div className="resume-notice danger">
              <strong>{text('Не удалось прочитать статус прогона', 'Could not read run status')}</strong>
              <span>{runnerError}</span>
            </div>
          ) : null}
          {note ? <div className="resume-notice"><span>{note}</span></div> : null}

          {!runner?.present ? (
            <div className="resume-notice">
              <strong>{text('Прогон ещё не начат', 'No run state yet')}</strong>
              {noTargetsStep ? (
                <span>
                  {text('Сохранённых целей roadmap нет. Дальше — на ваш выбор:', 'No saved roadmap targets. Your move:')}
                </span>
              ) : (
                <span>{text('Зафиксируйте C0: базовый снимок и контрольную проверку текущих зависимостей. Цели берутся из последнего roadmap (dashboard-state).', 'Capture C0: the baseline snapshot and control verification of current dependencies. Targets come from the last roadmap (dashboard-state).')}</span>
              )}
              <footer className="baseline-intent-actions">
                <button type="button" className="button primary" disabled={stepBusy} onClick={() => void beginNow({ mode: 'none' })}>
                  <Rocket size={16} />{text('Начать миграцию (C0)', 'Start migration (C0)')}
                </button>
                {noTargetsStep ? (
                  <>
                    <button type="button" className="button secondary" disabled={stepBusy} onClick={() => void beginNow({ mode: 'auto' })}>
                      <RefreshCw size={16} />{text('Ограниченный авто-поиск целей', 'Bounded auto-discovery of targets')}
                    </button>
                    {onConfigureScope ? (
                      <button type="button" className="button secondary" disabled={stepBusy} onClick={() => onConfigureScope()}>
                        <Wrench size={16} />{text('Настроить состав / Draft', 'Configure scope / Draft')}
                      </button>
                    ) : null}
                  </>
                ) : null}
                <button type="button" className="button secondary" disabled={stepBusy || noTargetsStep} onClick={() => void exportLegacyNow()}>
                  <FileText size={16} />{text('Импортировать ТЗ из сохранённого результата', 'Import task from the saved result')}
                </button>
              </footer>
            </div>
          ) : null}
          <div className="resume-notice">
            <strong>{text('Задание ещё не сформировано', 'The assignment is not ready yet')}</strong>
            {insufficient ? (
              <span>
                {text('Недостаточно данных durable-состояния:', 'Durable state lacks:')} {snapshot?.missing.join(', ')}. {text('Сначала выполните миграцию до verified checkpoint.', 'Run the migration to a verified checkpoint first.')}
              </span>
            ) : (
              <span>{text('Сначала выполните миграцию до verified checkpoint — задание появится здесь.', 'Run the migration to a verified checkpoint — the assignment appears here.')}</span>
            )}
          </div>
          {runner && runner.present ? (
            <div className="resume-notice">
              <strong>{text('Состояние прогона', 'Run state')}: {runner.phase ?? '—'}</strong>
              {runner.decision ? (
                <span>{text('Следующий шаг', 'Next step')}: {runner.decision.step ?? '—'} · {runner.decision.reason}</span>
              ) : null}
              {runtimeLine(runner)}
              <footer className="baseline-intent-actions">
                {/* Review re-check P1#4: a durable run whose ТЗ does not exist yet
                    is CONTINUABLE — the supervisor drives plan-next/…/gate and the
                    gate rebuilds the task artifact itself, so the coordinator
                    must be reachable here, not only «Сформировать задание». */}
                <button type="button" className="button primary" disabled={busy !== undefined || stepBusy} onClick={() => void driveNow()}>
                  <Forward size={16} />{text('Продолжить', 'Continue')}
                </button>
                <button type="button" className="button secondary" disabled={busy !== undefined || stepBusy} onClick={() => void run('export', () => onExport(projectName))}>
                  <RefreshCw size={16} />{text('Сформировать задание', 'Build the assignment')}
                </button>
              </footer>
            </div>
          ) : null}
          {runner?.decision?.step === 'agent' ? (
            <footer className="baseline-intent-actions">
              <button type="button" className="button primary" disabled={busy !== undefined || stepBusy} onClick={() => void agentNow()}>
                <Wrench size={16} />{text('Исправить агентом', 'Repair with agent')}
              </button>
            </footer>
          ) : null}
        </>
      ) : (
        <>
          <div className="roadmap-summary-grid">
            <div>
              <strong>{task.completeness.remaining}</strong>
              <span>{text('осталось', 'remaining')}</span>
            </div>
            <div>
              <strong>{task.actions.length}</strong>
              <span>{text('принято обновлений', 'accepted updates')}</span>
            </div>
            <div>
              <strong>{task.deferred.length}</strong>
              <span>{text('отложено', 'deferred')}</span>
            </div>
          </div>

          {snapshot?.stale ? (
            <div className="resume-notice warning">
              <strong>{text('ТЗ устарело', 'The assignment is stale')}</strong>
              <span>{snapshot.staleReason} {text('Перед диспатчем агента и на агент-гейте ТЗ пересобирается из текущего durable-состояния автоматически — ручная переэкспорт не обязателен.', 'Before dispatching the agent and at the agent gate the assignment is rebuilt from the current durable state automatically — manual re-export is not required.')}</span>
            </div>
          ) : null}

          {task.deferred.length > 0 ? (
            <ul className="plain-list">
              {task.deferred.map((item) => (
                <li key={item.package} title={item.reason}>
                  <strong>{item.package}</strong>
                  <span>
                    {item.current} → {item.lagPolicyTarget} · {text('отложено:', 'deferred:')} {item.reason}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}

          {/* L4: the failure note is inside the task branch AND next to the action
              buttons, so a failed step is never hidden behind the ТЗ. */}
          {attemptFailure ? (
            <div className="resume-notice danger">
              <strong>{text(attempt?.status === 'canceled' ? 'Попытка остановлена' : 'Попытка завершилась ошибкой', attempt?.status === 'canceled' ? 'Attempt canceled' : 'Attempt failed')}</strong>
              <span>{attemptFailure}</span>
              {attempt?.status === 'canceled' ? <span>{text('Verified checkpoint не тронут — продолжите нажатием «Продолжить».', 'The verified checkpoint was kept — continue via «Continue».')}</span> : null}
            </div>
          ) : null}
          {runnerError ? (
            <div className="resume-notice danger">
              <strong>{text('Не удалось прочитать статус прогона', 'Could not read run status')}</strong>
              <span>{runnerError}</span>
            </div>
          ) : null}

          {note ? <div className="resume-notice"><span>{note}</span></div> : null}

          {runner && runner.present ? (
            <div className="resume-notice">
              <strong>{text('Состояние прогона', 'Run state')}: {runner.phase ?? '—'}</strong>
              {runner.decision ? (
                <span>{text('Следующий шаг', 'Next step')}: {runner.decision.step ?? '—'} · {runner.decision.reason}</span>
              ) : null}
              {runtimeLine(runner)}
            </div>
          ) : null}

          <footer className="baseline-intent-actions">
            {childAlive ? (
              <button type="button" className="button danger" disabled={stepBusy} onClick={() => void cancelNow()}>
                <X size={16} />{text('Остановить', 'Cancel')}
              </button>
            ) : (
              <button type="button" className="button primary" disabled={busy !== undefined || stepBusy} onClick={() => void driveNow()}>
                <Forward size={16} />{text('Продолжить', 'Continue')}
              </button>
            )}
            {runner?.decision?.step === 'agent' ? (
              <button type="button" className="button primary" disabled={busy !== undefined || stepBusy} onClick={() => void agentNow()}>
                <Wrench size={16} />{text('Исправить агентом', 'Repair with agent')}
              </button>
            ) : null}
            <button type="button" className="button secondary" disabled={busy !== undefined} onClick={() => setShowDialog(true)}>
              <FileText size={16} />{text('Посмотреть', 'View')}
            </button>
            <button type="button" className="button secondary" disabled={busy !== undefined} onClick={() => void run('copy', () => onCopy(projectName, language))}>
              {copied ? <Check size={16} /> : <Clipboard size={16} />}{copied ? text('Скопировано', 'Copied') : text('Скопировать', 'Copy')}
            </button>
            <button type="button" className="button secondary" disabled={busy !== undefined} onClick={() => void run('save', () => onSave(projectName, language))}>
              <Save size={16} />{text('Сохранить', 'Save…')}
            </button>
            <button type="button" className="button secondary" disabled={busy !== undefined || stepBusy} onClick={() => void run('export', () => onExport(projectName))}>
              <RefreshCw size={16} />{text('Переэкспортировать', 'Re-export')}
            </button>
          </footer>
        </>
      )}

      {showDialog && task ? (
        <div className="baseline-intent-backdrop" role="presentation" onClick={() => setShowDialog(false)}>
          <section className="baseline-intent-dialog" role="dialog" aria-modal="true" aria-labelledby="iterative-task-title" onClick={(event) => event.stopPropagation()}>
            <header className="baseline-intent-header">
              <div>
                <FileText size={18} />
                <div>
                  <strong id="iterative-task-title">{text('Задание на адаптацию зависимостей', 'Dependency adaptation assignment')}</strong>
                  <span>{task.runId} · {task.targetCheckpointId} · {task.artifactId}</span>
                </div>
              </div>
              <button type="button" className="icon-button" aria-label={text('Закрыть', 'Close')} onClick={() => setShowDialog(false)}><X size={17} /></button>
            </header>
            <pre data-testid="iterative-task-preview" style={{ margin: 0, maxHeight: '56vh', overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', padding: '16px', borderRadius: '10px', border: '1px solid var(--border, rgba(127, 127, 127, .3))', background: 'var(--panel, rgba(0, 0, 0, .08))', fontSize: '12px', lineHeight: 1.55 }}>{task.content}</pre>
            <footer className="baseline-intent-actions">
              {snapshot.runDir ? (
                <button type="button" className="button secondary" onClick={() => void onOpenPath(snapshot.runDir)}><ExternalLink size={16} />{text('Открыть папку задания', 'Open task folder')}</button>
              ) : null}
              <button type="button" className="button primary" disabled={busy !== undefined} onClick={() => void run('copy', () => onCopy(projectName, language))}>
                {copied ? <Check size={16} /> : <Clipboard size={16} />}{copied ? text('Скопировано', 'Copied') : text('Скопировать задание', 'Copy assignment')}
              </button>
            </footer>
          </section>
        </div>
      ) : null}
    </section>
  )
}
