import { AlertCircle, AlertTriangle, Ban, Check, Copy, Download, MessageSquare, RotateCcw, Send, TerminalSquare, Wrench } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type UIEvent } from 'react'
import { latestJobId, mergeLogSources, migrationLogError, presentLogs, summarizeTokenUsage, type PresentedLog } from '../data/logPresentation'
import { useLanguage, type Language, type TranslationKey } from '../i18n'
import type { EnvironmentInfo, FlowAction, JobOutput, JobOutputSource, MigrationProgress } from '../types'
import { copyTextToClipboard } from '../clipboard'
import { useVirtualLogRows } from '../hooks/useVirtualLogRows'
import { RunMonitor } from './RunMonitor'
import { rawLogOffsets, rawLogWindow } from '../data/rawLogWindow'
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
  refreshError?: string
  onSendAgentNote: (note: string, branch?: string) => Promise<boolean>
  onCancel: () => void
  onClear: () => void
  showRunMonitor?: boolean
  showEnvironment?: boolean
  emptyMessage?: string
}

type LogView = 'activity' | 'raw'

function sourceKey(source?: JobOutputSource): string { return source ? `${source.kind}:${source.id}` : 'system' }
function isMachineTelemetry(entry: JobOutput): boolean {
  return entry.line.includes('DEPLOOM_PROGRESS_V2 ')
}

// C3: the raw journal view can hold up to MAX_LOG_ENTRIES lines; mounting every
// line on every buffer flush turns a busy job into a full repaint of a
// thousand `<pre>` nodes. The terminal is rendered through a window around the
// scroll position (measured row heights + overscan + leading/trailing spacers),
// so the DOM stays bounded while the full buffer stays intact for Copy.
const RAW_ROW_HEIGHT = 16
const RAW_OVERSCAN = 40

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

export function LogPanel({ logs, knownSources = [], environment, active, activeJobId, activeAction, runStartedAt, migrationProgress, refreshError, onSendAgentNote, onCancel, onClear, showRunMonitor = true, showEnvironment = true, emptyMessage }: Props) {
  const { language, t, text } = useLanguage()
  const [query, setQuery] = useState('')
  const [view, setView] = useState<LogView>('activity')
  const [selectedSource, setSelectedSource] = useState('all')
  const [note, setNote] = useState('')
  const [noteState, setNoteState] = useState<'idle' | 'sending' | 'sent' | 'unavailable'>('idle')
  const [copied, setCopied] = useState(false)
  const activityRef = useRef<HTMLDivElement>(null)
  const terminalRef = useRef<HTMLDivElement>(null)
  const followLogRef = useRef(true)
  const [following, setFollowing] = useState(true)
  const [rawScrollTop, setRawScrollTop] = useState(0)
  const [rawViewport, setRawViewport] = useState(0)
  const [rawHeights, setRawHeights] = useState<Map<JobOutput, number>>(() => new Map())
  const rawWidthRef = useRef(0)

  const sourceLabel = (source?: JobOutputSource): string => !source
    ? t('log.source.system')
    : source.kind === 'planner'
      ? source.label || t('log.source.planner')
      : t('log.source.group', { label: source.label })

  const localizeActivityTitle = (title: string | undefined, kind: PresentedLog['kind']): string => {
    if (!title) return kind === 'system' ? t('log.command') : t('log.output')
    const direct = GENERATED_TITLES[title]
    if (direct) return t(direct)
    const tool = /^Инструмент:\s*(.+)$/i.exec(title)
    return tool ? t('log.tool.generic', { name: tool[1] }) : title
  }

  const visibleLogs = useMemo(() => logs.filter((entry) => (view === 'raw' || !isMachineTelemetry(entry)) && (!query.trim() || entry.line.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))), [logs, query, view])
  const sourceOptions = useMemo(() => mergeLogSources(visibleLogs, knownSources), [visibleLogs, knownSources])
  const filteredLogs = useMemo(() => selectedSource === 'all' ? visibleLogs : selectedSource === 'system' ? visibleLogs.filter((entry) => !entry.source) : visibleLogs.filter((entry) => sourceKey(entry.source) === selectedSource), [selectedSource, visibleLogs])
  const selectedSourceInfo = sourceOptions.find(([key]) => key === selectedSource)?.[1]
  const addressedGroup = selectedSourceInfo?.kind === 'group' ? selectedSourceInfo : undefined
  const activity = useMemo(() => presentLogs(filteredLogs), [filteredLogs])
  const activityWindow = useVirtualLogRows(activity, activityRef, view === 'activity')
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
    const ok = await copyTextToClipboard(copyText)
    if (ok) {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }
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
    setFollowing(followLogRef.current)
  }

  const onTerminalScroll = (event: UIEvent<HTMLDivElement>) => {
    rememberLogPosition(event)
    setRawScrollTop(event.currentTarget.scrollTop)
  }

  useEffect(() => {
    const element = view === 'raw' ? terminalRef.current : undefined
    if (!element) return
    setRawViewport(element.clientHeight)
    const observer = new ResizeObserver(() => {
      setRawViewport(element.clientHeight)
      if (rawWidthRef.current !== element.clientWidth) {
        rawWidthRef.current = element.clientWidth
        setRawHeights(new Map())
      }
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [view])

  // Window of the raw journal currently mounted as DOM nodes. The viewport
  // height comes from a ResizeObserver so the window catches up when the panel
  // is resized, and the overscan keeps a fast-scrolling user from flashing
  // empty space.
  const rawOffsets = useMemo(() => rawLogOffsets(filteredLogs.map((entry) => rawHeights.get(entry) ?? RAW_ROW_HEIGHT)), [filteredLogs, rawHeights])
  const { start: rawStart, end: rawEnd } = rawLogWindow(rawOffsets, rawScrollTop, rawViewport || 480, RAW_OVERSCAN)

  useEffect(() => {
    const root = terminalRef.current
    if (view !== 'raw' || !root) return
    const rows = Array.from(root.querySelectorAll<HTMLElement>('[data-row-index]'))
    const measure = () => setRawHeights((current) => {
      const next = new Map<JobOutput, number>()
      for (const entry of filteredLogs) {
        const height = current.get(entry)
        if (height !== undefined) next.set(entry, height)
      }
      for (const row of rows) {
        const entry = filteredLogs[Number(row.dataset.rowIndex)]
        if (entry) next.set(entry, Math.max(1, row.getBoundingClientRect().height))
      }
      return next.size === current.size && [...next].every(([entry, height]) => current.get(entry) === height) ? current : next
    })
    let frame: number | undefined
    const schedule = () => {
      if (frame === undefined) frame = window.requestAnimationFrame(() => { frame = undefined; measure() })
    }
    schedule()
    const observer = new ResizeObserver(schedule)
    rows.forEach((row) => observer.observe(row))
    return () => { observer.disconnect(); if (frame !== undefined) window.cancelAnimationFrame(frame) }
  }, [view, filteredLogs, rawStart, rawEnd])
  const rawWindowed = rawStart > 0 || rawEnd < filteredLogs.length

  useEffect(() => {
    followLogRef.current = true
    setFollowing(true)
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
  }, [activity, filteredLogs, view, rawHeights])

  return (
    <aside className="log-panel">
      <div className="panel-heading">
        <div><TerminalSquare size={17} /><strong>{t('log.title')}</strong></div>
        {tokens.total > 0 ? <span className="token-counter" title={tokenDetails}>{formatTokens(tokens.total, language)} {t('log.tokensLatest')}</span> : null}
        <div className="heading-actions">
          {active ? <button className="button secondary danger-text" title={t('log.stop')} onClick={onCancel}><Ban size={16} />{text('Остановить процесс', 'Stop process')}</button> : null}
          <button className="button secondary" title={copied ? t('common.copied') : t('log.copy')} disabled={filteredLogs.length === 0} onClick={() => void copyLog()}>{copied ? <Check size={16} /> : <Copy size={16} />}{copied ? t('common.copied') : text('Копировать показанное', 'Copy visible events')}</button>
          <button className="button secondary" title={text('Скрыть показанные события. Полный журнал остаётся в файле.', 'Hide displayed events. The full journal remains on disk.')} onClick={onClear}><RotateCcw size={16} />{text('Очистить представление', 'Clear view')}</button>
        </div>
      </div>

      {showRunMonitor ? <RunMonitor logs={logs} active={active} jobId={activeJobId} action={activeAction} runStartedAt={runStartedAt} migrationProgress={migrationProgress} /> : null}

      {refreshError ? <div className="refresh-error-hint" role="status"><AlertTriangle size={14} />{t('log.refreshFailed')}: {refreshError}</div> : null}

      <label className="journal-search"><span>{text('Поиск в журнале', 'Search journal')}</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder={text('Текст сообщения или команды', 'Message or command text')} /></label>
      <div className="journal-follow-controls"><button type="button" className="button secondary" aria-pressed={following} onClick={() => { const next = !following; followLogRef.current = next; setFollowing(next); if (next) { const element = view === 'activity' ? activityRef.current : terminalRef.current; if (element) element.scrollTop = element.scrollHeight } }}>{following ? text('Следим за новыми событиями', 'Following new events') : text('Следить за новыми событиями', 'Follow new events')}</button></div>
      <div className="log-controls"><div className="log-tabs" role="tablist" aria-label={t('log.viewAria')}>
        <button role="tab" aria-selected={view === 'activity'} className={view === 'activity' ? 'active' : ''} onClick={() => setView('activity')}>{t('log.activity')}</button>
        <button role="tab" aria-selected={view === 'raw'} className={view === 'raw' ? 'active' : ''} onClick={() => setView('raw')}>{text('Исходный лог', 'Raw log')}</button>
      </div><div className="log-source-filter"><span>{t('log.session')}</span><QuickSelect value={selectedSource} onChange={(value) => { setSelectedSource(value); setNoteState('idle') }} ariaLabel={t('log.session')} options={[{ value: 'all', label: t('log.allMessages') }, { value: 'system', label: t('log.systemOrchestrator') }, ...sourceOptions.map(([key, source]) => ({ value: key, label: sourceLabel(source) }))]} /></div></div>

      {active && addressedGroup ? <div className="agent-chat-composer">
        <div><strong>{addressedGroup ? t('log.messageSelected', { source: sourceLabel(addressedGroup) }) : t('log.messageAgent')}</strong><span>{addressedGroup ? t('log.selectedLiveOnly') : t('log.selectGroup')}</span></div>
        <textarea rows={2} value={note} disabled={!active || !addressedGroup} onChange={(event) => { setNote(event.target.value); setNoteState('idle') }} placeholder={addressedGroup ? t('log.groupContextPlaceholder') : t('log.placeholderSelect')} />
        <button className="button secondary" disabled={!active || !addressedGroup || !note.trim() || noteState === 'sending'} onClick={() => void sendNote()}><Send size={14} />{noteState === 'sending' ? t('log.sending') : noteState === 'sent' ? t('log.sent') : t('log.send')}</button>
        {noteState === 'unavailable' ? <small>{t('log.sessionUnavailable')}</small> : null}
      </div> : null}

      {view === 'activity' ? (
        <div ref={activityRef} className="activity-log" aria-live="off" onScroll={event => { rememberLogPosition(event); activityWindow.onScroll(event) }}>
          {activity.length === 0 ? <span className="activity-empty">{emptyMessage ?? t('log.activityEmpty')}</span> : <>
          {activityWindow.before > 0 ? <div style={{height:activityWindow.before}} aria-hidden="true" /> : null}
          {activity.slice(activityWindow.start, activityWindow.end).map((entry, index) => (
            <div className={`activity-entry ${entry.kind}`} key={`${entry.kind}-${activityWindow.start + index}`} data-activity-row={activityWindow.start + index}>
              <span className="activity-icon"><ActivityIcon kind={entry.kind} /></span>
              <div><div className="activity-title"><strong>{localizeActivityTitle(entry.title, entry.kind)}</strong><span className="log-source-badge">{sourceLabel(entry.source)}</span></div><p>{entry.body}</p>{entry.detail ? <small>{entry.detail}</small> : null}</div>
            </div>
          ))}
          {activityWindow.after > 0 ? <div style={{height:activityWindow.after}} aria-hidden="true" /> : null}
          </>}
        </div>
      ) : (
        <div ref={terminalRef} className="terminal" aria-live="off" onScroll={onTerminalScroll}>
          {filteredLogs.length === 0 ? <span className="terminal-muted">{emptyMessage ?? t('log.rawEmpty')}</span> : <>
            {rawStart > 0 ? <div style={{ height: rawOffsets[rawStart] }} aria-hidden="true" data-window-spacer /> : null}
            {filteredLogs.slice(rawStart, rawEnd).map((entry, index) => {
              const i = rawStart + index
              return <div className="raw-log-entry" data-row-index={i} key={`${entry.jobId}-${i}`}><span className="log-source-badge">{sourceLabel(entry.source)}</span><pre className={`${entry.stream}${migrationLogError(entry.line) ? ' migration-error' : ''}`}>{entry.line}</pre></div>
            })}
            {rawEnd < filteredLogs.length ? <div style={{ height: rawOffsets[filteredLogs.length] - rawOffsets[rawEnd] }} aria-hidden="true" data-window-spacer /> : null}
            {rawWindowed ? <span className="terminal-muted" data-window-note>{t('log.windowed', { shown: rawEnd - rawStart, total: filteredLogs.length })}</span> : null}
          </>}
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
