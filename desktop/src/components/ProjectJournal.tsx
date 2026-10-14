import { Download, ExternalLink, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { useLanguage } from '../i18n'
import type { EnvironmentInfo, JobOutput } from '../types'
import type { ProjectJournalController } from '../hooks/useProjectJournal'
import { LogPanel } from './LogPanel'
import { QuickSelect } from './QuickSelect'
import './ProjectJournal.css'

type Props = { journal: ProjectJournalController; fallbackLogs: JobOutput[]; environment: EnvironmentInfo; appVersion: string; workspaceId: string; projectName: string; active: boolean; refreshError?: string; onClearFallback: () => void; onCancel: () => void; onOpenPath: (path?: string) => Promise<void>; onSendAgentNote: (note: string, branch?: string) => Promise<boolean> }
export function ProjectJournal({ journal, fallbackLogs, environment, appVersion, workspaceId, projectName, active, refreshError, onClearFallback, onCancel, onOpenPath, onSendAgentNote }: Props) {
  const { text } = useLanguage()
  const [exportError, setExportError] = useState<string>()
  const selected = journal.sources.find(source => source.id === journal.sourceId)
  const logs = selected ? journal.logs : fallbackLogs
  const current = !selected || selected.current || selected.runId === fallbackLogs.at(-1)?.jobId
  const emptyMessage = journal.loading ? text('Загружаем сохранённый журнал…', 'Loading the saved journal…') : journal.view?.cleared ? text('Представление очищено. Полный журнал сохранён в файле.', 'View cleared. The full journal remains on disk.') : active && current ? text('Ожидаем первые сообщения…', 'Waiting for first messages…') : text('В этом запуске событий нет.', 'This run has no events.')
  const exportLog = async () => { try { setExportError(undefined); await journal.exportLog() } catch (error) { setExportError(error instanceof Error ? error.message : String(error)) } }
  return <section className="project-journal" aria-label={text('Единый журнал проекта', 'Project journal')}>
    <header className="journal-heading"><div><h2>{text('Единый журнал проекта', 'Project journal')}</h2><p>{projectName}{selected ? ' · ' + selected.action : ''}</p></div><button type="button" className="button secondary" onClick={() => void journal.refresh()}><RefreshCw size={15} />{text('Обновить представление', 'Refresh view')}</button></header>
    <div className="journal-history"><label><span>{text('Запуск / источник', 'Run / source')}</span><QuickSelect value={journal.sourceId ?? ''} ariaLabel={text('История запусков и источников', 'Run and source history')} onChange={journal.selectSource} options={journal.sources.map(source => ({ value: source.id, label: [source.kind === 'iterative' ? text('Миграция', 'Migration') : source.action, source.runId, ...(source.startOffset !== undefined ? [text('Попытка', 'Attempt') + ' ' + source.attemptId] : []), source.format === 'legacy' ? text('исходный raw-лог', 'original raw log') : text('события', 'events')].join(' · ') }))} /></label><button type="button" className="button secondary" disabled={!selected} onClick={() => void exportLog()}><Download size={15} />{text('Экспорт полного журнала', 'Export full journal')}</button></div>
    {journal.error || exportError ? <p className="error-text" role="alert">{journal.error || exportError}</p> : null}
    {journal.view?.truncated ? <p className="journal-window-note">{text('Показано ограниченное окно событий. Полный журнал доступен через экспорт.', 'Showing a bounded event window. Export includes the full journal.')}</p> : null}
    <LogPanel refreshError={refreshError} key={journal.sourceId ?? 'live'} logs={logs} environment={environment} active={active && current} onSendAgentNote={onSendAgentNote} onCancel={onCancel} onClear={selected ? journal.clear : onClearFallback} showRunMonitor={false} showEnvironment={false} emptyMessage={emptyMessage} />
    <details className="journal-diagnostics"><summary>{text('Диагностика выбранного запуска', 'Selected run diagnostics')}</summary><dl><div><dt>{text('Приложение', 'Application')}</dt><dd>DepLoom {appVersion}</dd></div><div><dt>Workspace / {text('проект', 'project')}</dt><dd>{workspaceId} / {projectName}</dd></div><div><dt>Run / attempt</dt><dd>{selected?.runId ?? '—'} / {selected?.attemptId ?? '—'}</dd></div><div><dt>{text('Артефакт', 'Artifact')}</dt><dd>{selected?.artifactPath ?? '—'}</dd></div>{Object.entries(environment).map(([name, info]) => <div key={name}><dt>{name}</dt><dd>{info.available ? info.version : text('Не найден', 'Unavailable')}</dd></div>)}</dl>{selected ? <button type="button" className="button secondary" onClick={() => void onOpenPath(selected.artifactPath)}><ExternalLink size={15} />{text('Открыть исходный полный журнал', 'Open original full journal')}</button> : null}</details>
  </section>
}
