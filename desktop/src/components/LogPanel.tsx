import { AlertCircle, AlertTriangle, Ban, Check, Copy, Download, MessageSquare, RotateCcw, Send, TerminalSquare, Wrench } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type UIEvent } from 'react'
import { latestJobId, mergeLogSources, presentLogs, summarizeTokenUsage, type PresentedLog } from '../data/logPresentation'
import { useLanguage } from '../i18n'
import type { EnvironmentInfo, JobOutput, JobOutputSource } from '../types'
import { RunMonitor } from './RunMonitor'

type Props = {
  logs: JobOutput[]
  knownSources?: JobOutputSource[]
  environment: EnvironmentInfo
  active: boolean
  activeJobId?: string
  onSendAgentNote: (note: string, branch?: string) => Promise<boolean>
  onCancel: () => void
  onClear: () => void
}

type LogView = 'activity' | 'raw'

function sourceKey(source?: JobOutputSource): string { return source ? `${source.kind}:${source.id}` : 'system' }
function sourceLabel(source?: JobOutputSource): string { return !source ? 'Система' : source.kind === 'planner' ? 'Planner' : `Группа: ${source.label}` }

function formatTokens(value: number): string {
  return new Intl.NumberFormat('ru-RU', { notation: value >= 10_000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(value)
}

function ActivityIcon({ kind }: { kind: PresentedLog['kind'] }) {
  if (kind === 'user' || kind === 'message') return <MessageSquare size={15} />
  if (kind === 'tool') return <Wrench size={15} />
  if (kind === 'warning') return <AlertTriangle size={15} />
  if (kind === 'error') return <AlertCircle size={15} />
  return <TerminalSquare size={15} />
}

export function LogPanel({ logs, knownSources = [], environment, active, activeJobId, onSendAgentNote, onCancel, onClear }: Props) {
  const { text } = useLanguage()
  const [view, setView] = useState<LogView>('activity')
  const [selectedSource, setSelectedSource] = useState('all')
  const [note, setNote] = useState('')
  const [noteState, setNoteState] = useState<'idle' | 'sending' | 'sent' | 'unavailable'>('idle')
  const [copied, setCopied] = useState(false)
  const activityRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<HTMLDivElement>(null)
  const followLogRef = useRef(true)
  const sourceOptions = useMemo(() => mergeLogSources(logs, knownSources), [logs, knownSources])
  const filteredLogs = useMemo(() => selectedSource === 'all' ? logs : selectedSource === 'system' ? logs.filter((entry) => !entry.source) : logs.filter((entry) => sourceKey(entry.source) === selectedSource), [logs, selectedSource])
  const selectedSourceInfo = sourceOptions.find(([key]) => key === selectedSource)?.[1]
  const addressedGroup = selectedSourceInfo?.kind === 'group' ? selectedSourceInfo : undefined
  const activity = useMemo(() => presentLogs(filteredLogs), [filteredLogs])
  // Scoped to the most recent job: the activity/raw views below keep the full
  // session history for review, but a running total across every past stage
  // (preflight, baseline, several agent resumes, generate, audit, ...) makes
  // an unrelated small step look like it cost the whole session's tokens.
  const tokens = useMemo(() => summarizeTokenUsage(filteredLogs.filter((entry) => entry.jobId === latestJobId(filteredLogs))), [filteredLogs])
  const tokenDetails = [
    `Input: ${tokens.input.toLocaleString('ru-RU')}`,
    tokens.cacheRead ? `cache-read: ${tokens.cacheRead.toLocaleString('ru-RU')} (переиспользованный контекст, дешевле обычного input)` : '',
    tokens.cacheWrite ? `cache-write: ${tokens.cacheWrite.toLocaleString('ru-RU')}` : '',
    `output: ${tokens.output.toLocaleString('ru-RU')}`,
    tokens.reasoning ? `reasoning: ${tokens.reasoning.toLocaleString('ru-RU')}` : '',
    tokens.cost ? `cost: ${tokens.cost.toFixed(4)}` : '',
  ].filter(Boolean).join('; ')
  const copyText = useMemo(() => view === 'raw'
    ? filteredLogs.map((entry) => `[${sourceLabel(entry.source)}] ${entry.line}`).join('\n')
    : activity.map((entry) => `[${sourceLabel(entry.source)}] ${entry.title ?? (entry.kind === 'system' ? 'Команда' : 'Вывод')}: ${entry.body}${entry.detail ? ` (${entry.detail})` : ''}`).join('\n'),
    [view, filteredLogs, activity])
  const copyLog = async () => {
    try {
      await navigator.clipboard.writeText(copyText)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable, ignore */ }
  }
  const sendNote = async () => {
    const message = note.trim()
    if (!message || !addressedGroup) return
    setNoteState('sending')
    const sent = await onSendAgentNote(message, addressedGroup.id)
    setNoteState(sent ? 'sent' : 'unavailable')
    if (sent) setNote('')
  }
  const rememberLogPosition = (event: UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget
    followLogRef.current = element.scrollHeight - element.scrollTop - element.clientHeight <= 48
  }
  useEffect(() => {
    followLogRef.current = true
    const frame = window.requestAnimationFrame(() => {
      const element = view === 'activity' ? activityRef.current : terminalRef.current
      if (element) element.scrollTop = element.scrollHeight
    })
    return () => window.cancelAnimationFrame(frame)
  }, [view, selectedSource])
  useEffect(() => {
    if (!followLogRef.current) return
    const frame = window.requestAnimationFrame(() => {
      const element = view === 'activity' ? activityRef.current : terminalRef.current
      if (element) element.scrollTop = element.scrollHeight
    })
    return () => window.cancelAnimationFrame(frame)
  }, [activity, filteredLogs, view])

  return (
    <aside className="log-panel">
      <div className="panel-heading">
        <div><TerminalSquare size={17} /><strong>{text('Лог выполнения', 'Execution log')}</strong></div>
        {tokens.total > 0 ? <span className="token-counter" title={tokenDetails}>{formatTokens(tokens.total)} {text('токенов · последний запуск', 'tokens · latest run')}</span> : null}
        <div className="heading-actions">
          {active ? <button className="icon-button danger-text" title={text('Остановить', 'Stop')} onClick={onCancel}><Ban size={16} /></button> : null}
          <button className="icon-button" title={copied ? text('Скопировано', 'Copied') : text('Скопировать лог', 'Copy log')} disabled={filteredLogs.length === 0} onClick={() => void copyLog()}>{copied ? <Check size={16} /> : <Copy size={16} />}</button>
          <button className="icon-button" title={text('Очистить лог', 'Clear log')} onClick={onClear}><RotateCcw size={16} /></button>
        </div>
      </div>
      <RunMonitor logs={logs} active={active} jobId={activeJobId} />
      <div className="log-controls"><div className="log-tabs" role="tablist" aria-label="Представление лога">
        <button role="tab" aria-selected={view === 'activity'} className={view === 'activity' ? 'active' : ''} onClick={() => setView('activity')}>{text('Ход', 'Activity')}</button>
        <button role="tab" aria-selected={view === 'raw'} className={view === 'raw' ? 'active' : ''} onClick={() => setView('raw')}>Raw</button>
      </div><label className="log-source-filter">{text('Сессия', 'Session')}<select value={selectedSource} onChange={(event) => { setSelectedSource(event.target.value); setNoteState('idle') }}><option value="all">{text('Все сообщения', 'All messages')}</option><option value="system">{text('Система / оркестратор', 'System / orchestrator')}</option>{sourceOptions.map(([key, source]) => <option key={key} value={key}>{sourceLabel(source)}</option>)}</select></label></div>
      <div className="agent-chat-composer"><div><strong>{addressedGroup ? `Сообщение · ${sourceLabel(addressedGroup)}` : 'Сообщение агенту'}</strong><span>{addressedGroup ? 'Будет отправлено только выбранной live-сессии.' : 'Выберите конкретную группу в списке сессий.'}</span></div><textarea rows={2} value={note} disabled={!active || !addressedGroup} onChange={(event) => { setNote(event.target.value); setNoteState('idle') }} placeholder={addressedGroup ? 'Дополнительный контекст для этой группы…' : 'Сначала выберите группу'} /><button className="button secondary" disabled={!active || !addressedGroup || !note.trim() || noteState === 'sending'} onClick={() => void sendNote()}><Send size={14} />{noteState === 'sending' ? 'Отправляю…' : noteState === 'sent' ? 'Отправлено' : 'Отправить'}</button>{noteState === 'unavailable' ? <small>Сессия уже завершилась или ещё не открылась.</small> : null}</div>
      {view === 'activity' ? (
        <div ref={activityRef} className="activity-log" aria-live="polite" onScroll={rememberLogPosition}>
          {activity.length === 0 ? <span className="activity-empty">{text('Здесь появятся только этапы, результаты и изменения статусов.', 'Only stages, results, and status changes will appear here.')}</span> : activity.map((entry, index) => (
            <div className={`activity-entry ${entry.kind}`} key={`${entry.kind}-${index}`}>
              <span className="activity-icon"><ActivityIcon kind={entry.kind} /></span>
              <div><div className="activity-title"><strong>{entry.title ?? (entry.kind === 'system' ? 'Команда' : 'Вывод')}</strong><span className="log-source-badge">{sourceLabel(entry.source)}</span></div><p>{entry.body}</p>{entry.detail ? <small>{entry.detail}</small> : null}</div>
            </div>
          ))}
        </div>
      ) : (
        <div ref={terminalRef} className="terminal" aria-live="polite" onScroll={rememberLogPosition}>
          {filteredLogs.length === 0 ? <span className="terminal-muted">{text('Для выбранной сессии сырого вывода пока нет.', 'No raw output for the selected session yet.')}</span> : filteredLogs.map((entry, index) => <div className="raw-log-entry" key={`${entry.jobId}-${index}`}><span className="log-source-badge">{sourceLabel(entry.source)}</span><pre className={entry.stream}>{entry.line}</pre></div>)}
        </div>
      )}
      <div className="context-heading"><strong>{text('Окружение', 'Environment')}</strong></div>
      <div className="environment-list">
        {Object.entries(environment).map(([name, info]) => (
          <div className="environment-row" key={name}><span className={`status-dot ${info.available ? 'success' : 'danger'}`} /><strong>{name}</strong><span>{info.available ? info.version : 'не найден'}</span></div>
        ))}
      </div>
      <div className="download-hint"><Download size={16} /><span>{text('Prompt и dashboard-state из встроенного отчёта сохраняются в workspace автоматически.', 'Prompt and dashboard-state from the embedded report are saved to the workspace automatically.')}</span></div>
    </aside>
  )
}
