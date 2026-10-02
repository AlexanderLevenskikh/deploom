import { Copy, TerminalSquare } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { copyTextToClipboard } from '../clipboard'
import { useLanguage } from '../i18n'
import type { ScenarioSignal } from '../../electron/iterative-scenario'

function readableLog(log: string): string {
  return log.split('\n').map(line => {
    const match = line.match(/^ITERATIVE_MIGRATION_(?:STATUS|FAILURE)_V1 (\{.*\})$/)
    if (!match) return line
    try {
      const event = JSON.parse(match[1]) as { message?: string; summary?: string; event?: string; code?: string }
      return event.message ?? event.summary ?? event.event ?? event.code ?? line
    } catch { return line }
  }).join('\n')
}

export function IterativeRunDiagnostics({ signal, logs = false }: { signal: ScenarioSignal; logs?: boolean }) {
  const { text } = useLanguage()
  const [raw, setRaw] = useState(false)
  const [copyStatus, setCopyStatus] = useState<string>()
  const logRef = useRef<HTMLPreElement>(null)
  const follow = useRef(true)
  const attempt = signal.attempt
  useEffect(() => {
    follow.current = true
    setCopyStatus(undefined)
  }, [attempt?.attemptId])
  useEffect(() => {
    if (follow.current && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [signal.attemptLog, raw])
  const copy = async () => {
    const copied = await copyTextToClipboard(signal.attemptLog ?? '')
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
      <div className="iterative-log-toolbar"><button className="button secondary" aria-pressed={!raw} onClick={() => setRaw(false)}>{text('Ход работы', 'Activity')}</button><button className="button secondary" aria-pressed={raw} onClick={() => setRaw(true)}>Raw</button><button className="button secondary" disabled={!signal.attemptLog} onClick={() => void copy()}><Copy size={14} />{copyStatus ?? text('Скопировать лог', 'Copy log')}</button></div>
      <pre ref={logRef} className="attempt-log" tabIndex={0} onScroll={() => { const el = logRef.current; if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40 }}>{signal.attemptLog ? (raw ? signal.attemptLog : readableLog(signal.attemptLog)) : signal.running ? text('Ожидаем первые сообщения текущей попытки…', 'Waiting for the current attempt’s first messages…') : text('В этой попытке нет сохранённых сообщений.', 'No saved messages for this attempt.')}</pre>
    </> : null}
  </section>
}
