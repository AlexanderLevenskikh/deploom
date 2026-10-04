import { Copy, TerminalSquare } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { copyTextToClipboard } from '../clipboard'
import { iterativeLogMessages } from '../data/iterativeLogMessages'
import { useLanguage } from '../i18n'
import type { ScenarioSignal } from '../../electron/iterative-scenario'

export function IterativeRunDiagnostics({ signal, logs = false, onReadLog, onOpenLog }: { signal: ScenarioSignal; logs?: boolean; onReadLog?: () => Promise<{ runLog?: string }>; onOpenLog?: () => Promise<void> }) {
  const { text } = useLanguage()
  const [raw, setRaw] = useState(false)
  const [copyStatus, setCopyStatus] = useState<string>()
  const logRef = useRef<HTMLDivElement>(null)
  const readLog = useRef(onReadLog)
  readLog.current = onReadLog
  const logScope = JSON.stringify([signal.workspaceId, signal.projectName, signal.attempt?.attemptId])
  const [runLog, setRunLog] = useState<{ scope: string; text?: string }>()
  useEffect(() => {
    if (!logs) return
    let alive = true; let pending = false
    const poll = async () => {
      if (pending) return
      pending = true
      try { const result = await readLog.current?.(); if (alive) setRunLog({ scope: logScope, text: result?.runLog }) } finally { pending = false }
    }
    void poll().catch(() => {})
    const timer = window.setInterval(() => { void poll().catch(() => {}) }, 2000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [logs, logScope])
  const visibleLog = (runLog?.scope === logScope ? runLog.text : undefined) ?? signal.attemptLog ?? ''
  const follow = useRef(true)
  const attempt = signal.attempt
  useEffect(() => {
    follow.current = true
    setCopyStatus(undefined)
  }, [attempt?.attemptId])
  useEffect(() => {
    if (follow.current && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [visibleLog, raw])
  const copy = async () => {
    const copied = await copyTextToClipboard(visibleLog)
    setCopyStatus(copied ? text('Скопировано', 'Copied') : text('Не удалось скопировать', 'Copy failed'))
  }
  return <section className="iterative-run-diagnostics" data-testid={logs ? 'current-run-logs' : 'current-run-details'}>
    <header><strong><TerminalSquare size={16} />{text('Текущая попытка', 'Current attempt')}</strong><span>{signal.running ? text('Выполняется', 'Running') : signal.description}</span></header>
    <p><strong>{signal.activity.title}</strong> · {signal.elapsed}{signal.activity.detail ? ` · ${signal.activity.detail}` : ''}</p>
    {signal.activity.percent !== undefined ? <progress max={100} value={signal.activity.percent} aria-label={text('Прогресс подбора версий', 'Version discovery progress')} /> : null}
    <dl>
      <div><dt>{text('Попытка', 'Attempt')}</dt><dd>{attempt?.attemptId ?? text('Ещё не начата', 'Not started yet')}</dd></div>
      <div><dt>{text('Статус журнала', 'Journal status')}</dt><dd>{attempt?.status ?? '—'}</dd></div>
      <div><dt>{text('Последний этап', 'Latest phase')}</dt><dd>{attempt?.phase ?? attempt?.stage ?? '—'}</dd></div>
      <div><dt>{text('Последнее событие', 'Latest event')}</dt><dd>{attempt?.lastHeartbeatAt ? new Date(attempt.lastHeartbeatAt).toLocaleTimeString() : '—'}</dd></div>
    </dl>
    {attempt?.status === 'failed' ? <p className="error-text">{attempt.lastError || attempt.reason}</p> : null}
    {logs ? <>
      <div className="iterative-log-toolbar"><button className="button secondary" aria-pressed={!raw} onClick={() => setRaw(false)}>{text('Ход работы', 'Activity')}</button><button className="button secondary" aria-pressed={raw} onClick={() => setRaw(true)}>Raw</button><button className="button secondary" disabled={!visibleLog} onClick={() => void copy()}><Copy size={14} />{copyStatus ?? text('Скопировать лог', 'Copy log')}</button></div>
      <div ref={logRef} className="attempt-log iterative-log-messages" tabIndex={0} onScroll={() => { const el = logRef.current; if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40 }}>{visibleLog ? (raw ? <pre>{visibleLog}</pre> : iterativeLogMessages(visibleLog).map((message, index) => <pre className="iterative-log-message" key={index}>{message}</pre>)) : signal.running ? text('Ожидаем первые сообщения…', 'Waiting for first messages…') : text('Нет сохранённых сообщений.', 'No saved messages.')}</div>
      <p>{text('Накопительный журнал запуска. На экране последние 256 КБ; полный журнал сохраняется в run.log.', 'Cumulative run journal. The screen shows the latest 256 KB; the full journal is saved in run.log.')} {onOpenLog ? <button className="linklike" onClick={() => void onOpenLog()}>{text('Открыть полный журнал', 'Open full log')}</button> : null}</p>
    </> : null}
  </section>
}
