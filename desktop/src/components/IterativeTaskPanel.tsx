import { Check, Clipboard, ExternalLink, FileText, Play, RefreshCw, Save, Wrench, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useLanguage } from '../i18n'
import type { IterativeAgentOutcome, IterativeStatusOutcome, IterativeStepOutcome, IterativeTaskActionOutcome, IterativeTaskSnapshot } from '../types'

type Props = {
  workspaceId?: string
  projectName: string
  refreshKey?: string
  onGet: (projectName: string) => Promise<IterativeTaskSnapshot>
  onExport: (projectName: string) => Promise<IterativeTaskActionOutcome>
  onCopy: (projectName: string, language?: string) => Promise<IterativeTaskActionOutcome>
  onSave: (projectName: string, language?: string) => Promise<IterativeTaskActionOutcome>
  onStatus: (projectName: string) => Promise<IterativeStatusOutcome>
  onStep: (projectName: string) => Promise<IterativeStepOutcome>
  onAgent: (projectName: string) => Promise<IterativeAgentOutcome>
  onOpenPath: (path?: string) => Promise<void>
}

type PanelState =
  | { phase: 'loading' }
  | { phase: 'ready'; snapshot: IterativeTaskSnapshot }
  | { phase: 'error'; message: string }

export function IterativeTaskPanel({ projectName, refreshKey, onGet, onExport, onCopy, onSave, onStatus, onStep, onAgent, onOpenPath }: Props) {
  const { text, language } = useLanguage()
  const [state, setState] = useState<PanelState>({ phase: 'loading' })
  const [busy, setBusy] = useState<string>()
  const [copied, setCopied] = useState(false)
  const [note, setNote] = useState<string>()
  const [showDialog, setShowDialog] = useState(false)
  const [runner, setRunner] = useState<IterativeStatusOutcome>()
  const [stepBusy, setStepBusy] = useState(false)
  const loadSeq = useRef(0)

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

  const stepNow = async () => {
    setStepBusy(true)
    setNote(undefined)
    try {
      const outcome = await onStep(projectName)
      if (outcome.ok && outcome.gated === 'agent') {
        setNote(
          text(
            `Нужен агент: ${outcome.reason ?? ''}`,
            `Agent required: ${outcome.reason ?? ''}`,
          ),
        )
      } else if (outcome.ok) {
        setNote(
          text(
            `Шаг «${outcome.step ?? ''}» выполнен${outcome.next ? ` · далее: ${outcome.next.step ?? '—'}` : ''}`,
            `Step "${outcome.step ?? ''}" done${outcome.next ? ` · next: ${outcome.next.step ?? '—'}` : ''}`,
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
              <strong>{text('Задание устарело', 'The assignment is stale')}</strong>
              <span>{snapshot.staleReason} {text('Переэкспортируйте перед отправкой.', 'Re-export before sending.')}</span>
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

          {runner && runner.present && !runner.stale ? (
            <div className="resume-notice">
              <strong>{text('Состояние прогона', 'Run state')}: {runner.phase ?? '—'}</strong>
              {runner.decision ? (
                <span>{text('Следующий шаг', 'Next step')}: {runner.decision.step ?? '—'} · {runner.decision.reason}</span>
              ) : null}
            </div>
          ) : null}

          <footer className="baseline-intent-actions">
            {runner?.decision?.step === 'agent' ? (
              <button type="button" className="button primary" disabled={busy !== undefined || stepBusy} onClick={() => void agentNow()}>
                <Wrench size={16} />{text('Исправить агентом', 'Repair with agent')}
              </button>
            ) : null}
            <button type="button" className="button secondary" disabled={busy !== undefined || stepBusy} onClick={() => void stepNow()}>
              <Play size={16} />{text('Выполнить следующий шаг', 'Run next step')}
            </button>
            <button type="button" className="button secondary" disabled={busy !== undefined} onClick={() => setShowDialog(true)}>
              <FileText size={16} />{text('Посмотреть', 'View')}
            </button>
            <button type="button" className="button secondary" disabled={busy !== undefined} onClick={() => void run('copy', () => onCopy(projectName, language))}>
              {copied ? <Check size={16} /> : <Clipboard size={16} />}{copied ? text('Скопировано', 'Copied') : text('Скопировать', 'Copy')}
            </button>
            <button type="button" className="button secondary" disabled={busy !== undefined} onClick={() => void run('save', () => onSave(projectName, language))}>
              <Save size={16} />{text('Сохранить', 'Save…')}
            </button>
            <button type="button" className="button secondary" disabled={busy !== undefined || snapshot?.stale} onClick={() => void run('export', () => onExport(projectName))}>
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
