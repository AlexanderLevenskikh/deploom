import { useEffect, useRef, useState } from 'react'
import { X, FolderOpen } from 'lucide-react'
import { useLanguage } from '../i18n'
import type { DependencyFlowApi, StorageProgress, StorageResult, StorageStatus } from '../types'

export function StorageMaintenanceDialog({ onClose, onMaintenance, onProgress, status }: { status?: StorageStatus; onClose: () => void; onMaintenance: DependencyFlowApi['storageMaintenance']; onProgress: DependencyFlowApi['onStorageProgress'] }) {
  const { language } = useLanguage()
  const text = (ru: string, en: string) => language === 'ru' ? ru : en
  const [result, setResult] = useState<StorageResult>()
  const [busy, setBusy] = useState(false)
  const [action, setAction] = useState<'inspect' | 'clean'>('inspect')
  const [progress, setProgress] = useState<StorageProgress>()
  const [error, setError] = useState<string>()
  const activeOperation = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (!status || status.phase === 'idle') return
    setBusy(status.phase === 'inspect' || status.phase === 'clean')
    if (status.action) setAction(status.action)
    setProgress(status.progress); setResult(status.result); setError(status.error)
    activeOperation.current = status.phase === 'inspect' || status.phase === 'clean' ? status.operationId : undefined
  }, [status])
  useEffect(() => onProgress(event => {
    if (event.operationId === activeOperation.current) setProgress(event)
  }), [onProgress])
  const run = async (next: 'inspect' | 'clean') => {
    if (activeOperation.current) return
    const operationId = crypto.randomUUID()
    activeOperation.current = operationId
    setBusy(true); setAction(next); setError(undefined); setProgress(undefined)
    if (next === 'inspect') setResult(undefined)
    try { setResult(await onMaintenance(next, operationId)) }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      if (next === 'clean') setResult(undefined)
    } finally { activeOperation.current = undefined; setBusy(false) }
  }
  const gib = (bytes: number) => `${(bytes / 1024 ** 3).toFixed(1)} ${text('ГиБ', 'GiB')}`
  const label = (category: string) => ({
    'snapshot-compaction': text('Уплотнение старых снимков', 'Compact historical snapshots'),
    'snapshot-objects': text('Сжатое содержимое снимков', 'Compressed snapshot contents'),
    'packed-tree-garbage': text('Остатки завершённого уплотнения', 'Completed compaction residue'),
    'retired-materialization': text('Сохранённые рабочие копии', 'Preserved working copies'),
    'obsolete-upgrade-copy': text('Копии предыдущих проверок', 'Previous verification copies'),
    'verification-trials': text('Временные проверки', 'Temporary verification'),
    'prepared-trash': text('Отсоединённые подготовленные копии', 'Detached prepared copies'),
    'verification-cache': text('Общие кэши', 'Shared caches'),
    'current-run': text('Текущие прогоны и checkpoints', 'Current runs and checkpoints'),
    'archive-history': text('История и снимки архивов', 'Archive history and snapshots'),
    'archived-workspace': text('Рабочие копии архивных прогонов', 'Archived trial workspaces'),
    delivery: text('Ветки доставки', 'Delivery branches'),
  }[category] || category)
  const reasons: Record<string, string> = {
    'current-checkpoint-or-run-data': text('Данные текущего прогона', 'Current run data'),
    'archive-checkpoints-and-evidence': text('Снимки, история и доказательства сохранены', 'Snapshots, history and evidence preserved'),
    'live-or-unknown-owner': text('Владелец активен или не подтверждён', 'Owner is active or unknown'),
    'unknown-ownership': text('Принадлежность не подтверждена', 'Ownership unconfirmed'),
    'links-or-shared-files': text('Ссылки или общие файлы', 'Links or shared files'),
    recent: text('Создано менее суток назад', 'Less than one day old'),
    'shared-cache': text('Общий действующий кэш', 'Active shared cache'),
    'shared-snapshot-objects': text('Общие данные для восстановления снимков', 'Shared contents for snapshot restoration'),
    'delivery-branch': text('Рабочая ветка результата', 'Result worktree'),
    'current-run-reference': text('Используется текущим прогоном', 'Referenced by current run'),
    'changed-after-preview': text('Изменилось после диагностики', 'Changed after inspection'),
    'unreadable': text('Не удалось проверить', 'Could not inspect'),
    'unfinished-archive': text('Незавершённый архивный прогон', 'Unfinished archived run'),
    'archived-candidate-work': text('Сохранённая работа кандидата', 'Saved candidate work'),
    'missing-archive-source': text('Сохранность исходного снимка не подтверждена', 'Source snapshot preservation unconfirmed'),
    'unknown-archive': text('Принадлежность архива не подтверждена', 'Archive ownership unconfirmed'),
    'unknown-archive-evidence': text('Данные архива не удалось проверить', 'Archive evidence could not be checked'),
    'unknown-cache-evidence': text('Данные кэша не удалось проверить', 'Cache evidence could not be checked'),
    'published-cache-reference': text('Используется опубликованным кэшем', 'Referenced by a published cache'),
    'agent-session': text('Сохранена сессия агента', 'Saved agent session'),
    'restart-pending': text('Не завершено архивирование', 'Archival transaction pending'),
    'no-verified-archive-checkpoint': text('Нет сохранённого проверенного результата', 'No saved verified result'),
    'unsafe-path': text('Безопасность пути не подтверждена', 'Path safety unconfirmed'),
    'unpreserved-materialization-edits': text('Есть изменения без сохранённого снимка', 'Edits without a preserved snapshot'),
    'unknown-materialization-evidence': text('Не удалось подтвердить сохранность копии', 'Copy preservation could not be verified'),
    'active-materialization': text('Рабочая копия незавершённой операции', 'Unfinished operation workspace'),
    'current-verifier-copy': text('Копия текущей версии инструмента', 'Current verifier copy'),
    'scope-changed': text('Изменился состав папок', 'Folder scope changed'),
    'disk-space-low': text('Недостаточно места для безопасной операции; прежние данные сохранены', 'Insufficient room for a safe operation; previous data preserved'),
  }
  const groups = new Map<string, { category: string; volume: string; bytes: number; eligible: number; count: number }>()
  for (const item of result?.items || []) {
    const volume = item.path.match(/^[A-Za-z]:/)?.[0] || '/'
    const key = `${volume}:${item.category}`
    const group = groups.get(key) || { category: item.category, volume, bytes: 0, eligible: 0, count: 0 }
    group.bytes += item.bytes; group.eligible += item.eligible ? item.bytes : 0; group.count++
    groups.set(key, group)
  }
  const outcomes = new Map((result?.results || []).map(item => [item.path, item]))
  const completed = Boolean(result?.results)
  const canClean = !busy && result && !completed && result.eligible > 0
  const stage = progress?.stage === 'clean' ? text('Удаляем подтверждённый мусор', 'Removing confirmed garbage') : progress?.stage === 'validate' ? text('Повторно проверяем папки перед удалением', 'Rechecking folders before deletion') : text('Измеряем папки и проверяем принадлежность', 'Measuring folders and verifying ownership')
  return <div className="dialog-backdrop">
    <section className="dialog workspace-dialog storage-dialog" role="dialog" aria-modal="true" aria-labelledby="storage-title">
      <div className="dialog-title"><h2 id="storage-title">{text('Хранилище и безопасная очистка', 'Storage and safe cleanup')}</h2><button className="icon-button" aria-label={text('Закрыть', 'Close')} onClick={onClose}><X size={17} /></button></div>
      <p>{text('Очистка работает в фоне, диалог можно закрыть. Удаляем подтверждённый мусор; старые снимки уплотняем без потери файлов, одинаковое содержимое храним один раз. Последний результат, отчёты, настройки и рабочие ветки сохраняются.', 'Cleanup runs in the background; you can close this dialog. Remove confirmed garbage; compact historical snapshots losslessly, storing identical contents once. Preserve the latest result, reports, settings and result worktrees.')}</p>
      {busy ? <div className="storage-progress" role="status" aria-live="polite">
        <strong>{stage}{progress?.percent != null && progress.stage === 'clean' ? ` · ${progress.percent}%` : ''}</strong>
        <progress max={100} value={progress?.stage === 'clean' && progress.percent != null ? progress.percent : undefined} aria-label={stage} />
        <span>{action === 'clean' && progress?.stage === 'clean' ? `${progress.processed.toLocaleString()} / ${progress.total.toLocaleString()} ${text('файлов обработано', 'files processed')}` : `${(progress?.files || 0).toLocaleString()} ${text('файлов в текущей папке', 'files in current folder')}`}</span>
        <small className="storage-path">{progress?.path}</small>
      </div> : null}
      {result ? <>
        <div className="storage-metrics">
          <p><strong>{gib(result.eligibleBytes)}</strong><span>{completed ? text('Было выбрано для очистки', 'Selected before cleanup') : text('Доступно для безопасной очистки', 'Available for safe cleanup')}</span></p>
          <p><strong>{gib(result.protectedBytes)}</strong><span>{text('Сохраняем', 'Preserved')}</span></p>
          {result.volumes.map(volume => <p key={volume.path}><strong>{gib(volume.freeBytes)}</strong><span>{text('Свободно', 'Free')} · {volume.path}</span></p>)}
        </div>
        <p className="muted">{text('Объём файлов — оценка: общие блоки и сжатие могут влиять на фактически освобождённое место.', 'File volume is an estimate: shared blocks and compression can affect actual disk space reclaimed.')}</p>
        <div className="storage-table-scroll"><table className="storage-table"><thead><tr><th>{text('Категория / том', 'Category / volume')}</th><th>{text('Всего', 'Total')}</th><th>{text('Можно очистить', 'Can clean')}</th></tr></thead><tbody>{[...groups.values()].map(group => <tr key={`${group.volume}:${group.category}`}><td>{label(group.category)} · {group.volume}<small>{text('Каталогов', 'Folders')}: {group.count}</small></td><td>{gib(group.bytes)}</td><td>{gib(group.eligible)}</td></tr>)}</tbody></table></div>
        <details><summary>{text('Папки и причины защиты', 'Folders and protection reasons')}</summary><div className="storage-folder-list">{result.items.map(item => <div key={item.path}><button className="icon-button" title={text('Открыть папку', 'Open folder')} aria-label={text('Открыть папку', 'Open folder')} disabled={outcomes.get(item.path)?.status === 'removed'} onClick={() => void window.dependencyFlow?.openPath(item.path)}><FolderOpen size={15} /></button><span className="storage-path">{item.path}<small>{gib(item.bytes)} · {outcomes.get(item.path)?.status === 'removed' ? text('Удалено', 'Removed') : outcomes.get(item.path)?.status === 'compacted' ? text('Сохранено в сжатом виде; восстановится при обращении', 'Compacted; restored on access') : outcomes.get(item.path)?.status === 'failed' ? text('Не удалось удалить; папка сохранена или удалена частично', 'Deletion failed; folder retained or partially removed') : outcomes.get(item.path)?.reason ? reasons[outcomes.get(item.path)!.reason!] || outcomes.get(item.path)!.reason : item.eligible ? text('Можно очистить', 'Can clean') : reasons[item.reason] || item.reason}</small></span></div>)}</div></details>
        {completed ? <p role="status">{text('Очистка завершена', 'Cleanup finished')}: {text('удалено каталогов', 'folders removed')}: {result.removed}; {text('уплотнено снимков', 'snapshots compacted')}: {result.compacted || 0}; {gib(result.reclaimedBytes)} {text('освобождено по объёму файлов', 'reclaimed by file volume')}. {text('Пропущено', 'Skipped')}: {result.protected}; {text('ошибок', 'errors')}: {result.failed}. {text('Свободное место на томах обновлено.', 'Volume free space refreshed.')}</p> : null}
      </> : null}
      {error ? <p role="alert">{error}</p> : null}
      <div className="dialog-actions"><button className="button secondary" disabled={busy} onClick={() => void run('inspect')}>{text('Диагностика всех хранилищ', 'Inspect all storage')}</button><button className="button primary" disabled={!canClean} onClick={() => void run('clean')}>{text('Очистить весь подтверждённый мусор', 'Clean all confirmed garbage')}</button></div>
    </section>
  </div>
}
