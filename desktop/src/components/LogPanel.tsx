import { AlertCircle, AlertTriangle, Ban, Check, Copy, Download, MessageSquare, RotateCcw, Send, TerminalSquare, Wrench } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type UIEvent } from 'react'
import { latestJobId, mergeLogSources, presentLogs, summarizeTokenUsage, type PresentedLog } from '../data/logPresentation'
import { useLanguage, type Language, type TranslationKey } from '../i18n'
import type { EnvironmentInfo, FlowAction, JobOutput, JobOutputSource, MigrationProgress } from '../types'
import { RunMonitor } from './RunMonitor'
import { QuickSelect } from './QuickSelect'

type Props = {
  logs: JobOutput[]
  knownSources?: JobOutputSource[]
  environment: EnvironmentInfo
  active: boolean
  activeJobId?: string
  activeAction?: FlowAction
  runStartedAt?: number
  migrationProgress?: MigrationProgress
  onSendAgentNote: (note: string, branch?: string) => Promise<boolean>
  onCancel: () => void
  onClear: () => void
  showRunMonitor?: boolean
  showEnvironment?: boolean
}

type LogView = 'activity' | 'raw'

function sourceKey(source?: JobOutputSource): string { return source ? `${source.kind}:${source.id}` : 'system' }
function isMachineTelemetry(entry: JobOutput): boolean {
  return entry.line.includes('DEPLOOM_PROGRESS_V2 ')
}

function formatTokens(value: number, language: Language): string {
  return new Intl.NumberFormat(language === 'ru' ? 'ru-RU' : 'en-US', {
    notation: value >= 10_000 ? 'compact' : 'standard',
    maximumFractionDigits: 1,
  }).format(value)
}

function ActivityIcon({ kind }: { kind: PresentedLog['kind'] }) {
  if (kind === 'user' || kind === 'message') return <MessageSquare size={15} />
  if (kind === 'tool') return <Wrench size={15} />
  if (kind === 'warning') return <AlertTriangle size={15} />
  if (kind === 'error') return <AlertCircle size={15} />
  return <TerminalSquare size={15} />
}

const GENERATED_TITLES: Record<string, TranslationKey> = {
  'Предупреждение': 'log.generated.warning',
  'Ошибка': 'log.generated.error',
  'Готово': 'log.generated.done',
  'Ход выполнения': 'log.generated.progress',
  'Агент': 'log.generated.agent',
  'Результат инструмента': 'log.generated.toolResult',
  'Ошибка агента': 'log.generated.agentError',
  'Подсказка': 'log.generated.hint',
  'Выполняю команду': 'log.tool.bash',
  'Изменяю файл': 'log.tool.edit',
  'Ищу файлы': 'log.tool.glob',
  'Ищу по коду': 'log.tool.grep',
  'Просматриваю каталог': 'log.tool.list',
  'Читаю файл': 'log.tool.read',
  'Запускаю подзадачу': 'log.tool.task',
  'Записываю файл': 'log.tool.write',
}

export function LogPanel({ logs, knownSources = [], environment, active, activeJobId, activeAction, runStartedAt, migrationProgress, onSendAgentNote, onCancel, onClear, showRunMonitor = true, showEnvironment = true }: Props) {
  const { language, t } = useLanguage()
  const [view, setView] = useState<LogView>('activity')
  const [selectedSource, setSelectedSource] = useState('all')
  const [note, setNote] = useState('')
  const [noteState, setNoteState] = useState<'idle' | 'sending' | 'sent' | 'unavailable'>('idle')
  const [copied, setCopied] = useState(false)
  const activityRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<HTMLDivElement>(null)
  const followLogRef = useRef(true)

  const sourceLabel = (source?: JobOutputSource): string => !source
    ? t('log.source.system')
    : source.kind === 'planner'
      ? t('log.source.planner')
      : t('log.source.group', { label: source.label })

  const localizeActivityTitle = (title: string | undefined, kind: PresentedLog['kind']): string => {
    if (!title) return kind === 'system' ? t('log.command') : t('log.output')
    const direct = GENERATED_TITLES[title]
    if (direct) return t(direct)
    const tool = /^Инструмент:\s*(.+)$/i.exec(title)
    return tool ? t('log.tool.generic', { name: tool[1] }) : title
  }

  const visibleLogs = useMemo(() => logs.filter((entry) => !isMachineTelemetry(entry)), [logs])
  const sourceOptions = useMemo(() => mergeLogSources(visibleLogs, knownSources), [visibleLogs, knownSources])
  const filteredLogs = useMemo(() => selectedSource === 'all' ? visibleLogs : selectedSource === 'system' ? visibleLogs.filter((entry) => !entry.source) : visibleLogs.filter((entry) => sourceKey(entry.source) === selectedSource), [selectedSource, visibleLogs])
  const selectedSourceInfo = sourceOptions.find(([key]) => key === selectedSource)?.[1]
  const addressedGroup = selectedSourceInfo?.kind === 'group' ? selectedSourceInfo : undefined
  const activity = useMemo(() => presentLogs(filteredLogs), [filteredLogs])
  const tokens = useMemo(() => summarizeTokenUsage(filteredLogs.filter((entry) => entry.jobId === latestJobId(filteredLogs))), [filteredLogs])

  const numberLocale = language === 'ru' ? 'ru-RU' : 'en-US'
  const tokenDetails = [
    `Input: ${tokens.input.toLocaleString(numberLocale)}`,
    tokens.cacheRead ? `cache-read: ${tokens.cacheRead.toLocaleString(numberLocale)} (${t('log.cacheReadHint')})` : '',
    tokens.cacheWrite ? `cache-write: ${tokens.cacheWrite.toLocaleString(numberLocale)}` : '',
    `output: ${tokens.output.toLocaleString(numberLocale)}`,
    tokens.reasoning ? `reasoning: ${tokens.reasoning.toLocaleString(numberLocale)}` : '',
    tokens.cost ? `cost: ${tokens.cost.toFixed(4)}` : '',
  ].filter(Boolean).join('; ')

  const copyText = view === 'raw'
    ? filteredLogs.map((entry) => `[${sourceLabel(entry.source)}] ${entry.line}`).join('\n')
    : activity.map((entry) => `[${sourceLabel(entry.source)}] ${localizeActivityTitle(entry.title, entry.kind)}: ${entry.body}${entry.detail ? ` (${entry.detail})` : ''}`).join('\n')

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
        <div><TerminalSquare size={17} /><strong>{t('log.title')}</strong></div>
        {tokens.total > 0 ? <span className="token-counter" title={tokenDetails}>{formatTokens(tokens.total, language)} {t('log.tokensLatest')}</span> : null}
        <div className="heading-actions">
          {active ? <button className="icon-button danger-text" title={t('log.stop')} onClick={onCancel}><Ban size={16} /></button> : null}
          <button className="icon-button" title={copied ? t('common.copied') : t('log.copy')} disabled={filteredLogs.length === 0} onClick={() => void copyLog()}>{copied ? <Check size={16} /> : <Copy size={16} />}</button>
          <button className="icon-button" title={t('log.clear')} onClick={onClear}><RotateCcw size={16} /></button>
        </div>
      </div>

      {showRunMonitor ? <RunMonitor logs={logs} active={active} jobId={activeJobId} action={activeAction} runStartedAt={runStartedAt} migrationProgress={migrationProgress} /> : null}

      <div className="log-controls"><div className="log-tabs" role="tablist" aria-label={t('log.viewAria')}>
        <button role="tab" aria-selected={view === 'activity'} className={view === 'activity' ? 'active' : ''} onClick={() => setView('activity')}>{t('log.activity')}</button>
        <button role="tab" aria-selected={view === 'raw'} className={view === 'raw' ? 'active' : ''} onClick={() => setView('raw')}>Raw</button>
      </div><div className="log-source-filter"><span>{t('log.session')}</span><QuickSelect value={selectedSource} onChange={(value) => { setSelectedSource(value); setNoteState('idle') }} ariaLabel={t('log.session')} options={[{ value: 'all', label: t('log.allMessages') }, { value: 'system', label: t('log.systemOrchestrator') }, ...sourceOptions.map(([key, source]) => ({ value: key, label: sourceLabel(source) }))]} /></div></div>

      <div className="agent-chat-composer">
        <div><strong>{addressedGroup ? t('log.messageSelected', { source: sourceLabel(addressedGroup) }) : t('log.messageAgent')}</strong><span>{addressedGroup ? t('log.selectedLiveOnly') : t('log.selectGroup')}</span></div>
        <textarea rows={2} value={note} disabled={!active || !addressedGroup} onChange={(event) => { setNote(event.target.value); setNoteState('idle') }} placeholder={addressedGroup ? t('log.groupContextPlaceholder') : t('log.placeholderSelect')} />
        <button className="button secondary" disabled={!active || !addressedGroup || !note.trim() || noteState === 'sending'} onClick={() => void sendNote()}><Send size={14} />{noteState === 'sending' ? t('log.sending') : noteState === 'sent' ? t('log.sent') : t('log.send')}</button>
        {noteState === 'unavailable' ? <small>{t('log.sessionUnavailable')}</small> : null}
      </div>

      {view === 'activity' ? (
        <div ref={activityRef} className="activity-log" aria-live="polite" onScroll={rememberLogPosition}>
          {activity.length === 0 ? <span className="activity-empty">{t('log.activityEmpty')}</span> : activity.map((entry, index) => (
            <div className={`activity-entry ${entry.kind}`} key={`${entry.kind}-${index}`}>
              <span className="activity-icon"><ActivityIcon kind={entry.kind} /></span>
              <div><div className="activity-title"><strong>{localizeActivityTitle(entry.title, entry.kind)}</strong><span className="log-source-badge">{sourceLabel(entry.source)}</span></div><p>{entry.body}</p>{entry.detail ? <small>{entry.detail}</small> : null}</div>
            </div>
          ))}
        </div>
      ) : (
        <div ref={terminalRef} className="terminal" aria-live="polite" onScroll={rememberLogPosition}>
          {filteredLogs.length === 0 ? <span className="terminal-muted">{t('log.rawEmpty')}</span> : filteredLogs.map((entry, index) => <div className="raw-log-entry" key={`${entry.jobId}-${index}`}><span className="log-source-badge">{sourceLabel(entry.source)}</span><pre className={entry.stream}>{entry.line}</pre></div>)}
        </div>
      )}

      {showEnvironment ? <>
        <div className="context-heading"><strong>{t('log.environment')}</strong></div>
        <div className="environment-list">
          {Object.entries(environment).map(([name, info]) => (
            <div className="environment-row" key={name}><span className={`status-dot ${info.available ? 'success' : 'danger'}`} /><strong>{name}</strong><span>{info.available ? info.version : t('log.notFound')}</span></div>
          ))}
        </div>
        <div className="download-hint"><Download size={16} /><span>{t('log.downloadHint')}</span></div>
      </> : null}
    </aside>
  )
}
