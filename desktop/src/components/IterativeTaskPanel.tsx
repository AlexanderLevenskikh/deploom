import { Check, Clipboard, ExternalLink, FileText, Forward, RefreshCw, Save, Wrench, X, Rocket } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useLanguage } from '../i18n'
import type { IterativeAgentOutcome, IterativeBeginOutcome, IterativeDriveOutcome, IterativeStatusOutcome, IterativeTaskActionOutcome, IterativeTaskSnapshot } from '../types'

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
  onBegin: (projectName: string) => Promise<IterativeBeginOutcome>
  onAgent: (projectName: string) => Promise<IterativeAgentOutcome>
  onOpenPath: (path?: string) => Promise<void>
}

type PanelState =
  | { phase: 'loading' }
  | { phase: 'ready'; snapshot: IterativeTaskSnapshot }
  | { phase: 'error'; message: string }

export function IterativeTaskPanel({ projectName, refreshKey, onGet, onExport, onCopy, onSave, onStatus, onDrive, onBegin, onExportLegacy, onAgent, onOpenPath }: Props) {
  const { text, language } = useLanguage()
  const [state, setState] = useState<PanelState>({ phase: 'loading' })
  const [busy, setBusy] = useState<string>()
  const [copied, setCopied] = useState(false)
  const [note, setNote] = useState<string>()
  const [showDialog, setShowDialog] = useState(false)
  const [runner, setRunner] = useState<IterativeStatusOutcome>()
  const [stepBusy, setStepBusy] = useState(false)
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

  const refreshRunner = useCallback(async () => {
    try {
      const status = await onStatus(projectName)
      setRunner(status)
      return status
    } catch {
      setRunner(undefined)
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

  const beginNow = async () => {
    setStepBusy(true)
    setNote(undefined)
    try {
      const outcome = await onBegin(projectName)
      if (outcome.ok) {
        setNote(
          text(
            `Начат прогон: C0${outcome.targetsCount !== undefined ? `, целей из roadmap: ${outcome.targetsCount}` : ''}`,
            `Run started: C0${outcome.targetsCount !== undefined ? `, roadmap targets: ${outcome.targetsCount}` : ''}`,
          ),
        )
        // Continuous migration: keep driving until the first gate/finish.
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

  const snapshot = state.phase === 'ready' ? state.snapshot : undefined
  const task = snapshot?.task
  const insufficient = Boolean(snapshot && !snapshot.present && snapshot.missing.length > 0)

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
        <button type="button" className="icon-button" aria-label={text('Обновить', 'Reload')} onClick={() => void load()} disabled={busy === 'load'}><RefreshCw size={16} /></button>
      </header>

      {state.phase === 'loading' ? (
        <div className="resume-notice"><span>{text('Загружаю задание…', 'Loading the assignment…')}</span></div>
      ) : state.phase === 'error' ? (
        <div className="resume-notice danger">
          <strong>{text('Не удалось прочитать задание', 'Could not read the assignment')}</strong>
          <span>{state.message}</span>
        </div>
      ) : !task ? (
        <>
          {!runner?.present ? (
            <div className="resume-notice">
              <strong>{text('Прогон ещё не начат', 'No run state yet')}</strong>
              <span>{text('Зафиксируйте C0: базовый снимок и контрольную проверку текущих зависимостей. Цели берутся из последнего roadmap (dashboard-state).', 'Capture C0: the baseline snapshot and control verification of current dependencies. Targets come from the last roadmap (dashboard-state).')}</span>
              <footer className="baseline-intent-actions">
                <button type="button" className="button primary" disabled={stepBusy} onClick={() => void beginNow()}>
                  <Rocket size={16} />{text('Начать миграцию (C0)', 'Start migration (C0)')}
                </button>
                <button type="button" className="button secondary" disabled={stepBusy} onClick={() => void exportLegacyNow()}>
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
            <button type="button" className="button primary" disabled={busy !== undefined || stepBusy} onClick={() => void driveNow()}>
              <Forward size={16} />{text('Продолжить', 'Continue')}
            </button>
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
