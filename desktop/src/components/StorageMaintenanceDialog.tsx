import { useState } from 'react'
import { X } from 'lucide-react'
import { useLanguage } from '../i18n'
import type { DependencyFlowApi } from '../types'

type Result = Awaited<ReturnType<DependencyFlowApi['storageMaintenance']>>
export function StorageMaintenanceDialog({ onClose, onMaintenance }: { onClose: () => void; onMaintenance: DependencyFlowApi['storageMaintenance'] }) {
  const { language } = useLanguage()
  const text = (ru: string, en: string) => language === 'ru' ? ru : en
  const [result, setResult] = useState<Result>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const run = async (action: 'inspect' | 'clean') => {
    if (busy) return
    setBusy(true); setError(undefined)
    try { setResult(await onMaintenance(action)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setBusy(false) }
  }
  return <div className="dialog-backdrop">
    <section className="dialog workspace-dialog" role="dialog" aria-modal="true" aria-labelledby="storage-title">
      <div className="dialog-title"><h2 id="storage-title">{text('Очистка хранилища', 'Storage cleanup')}</h2><button className="icon-button" aria-label={text('Закрыть', 'Close')} disabled={busy} onClick={onClose}><X size={17} /></button></div>
      <p>{text('Удаляем только отложенный мусор DepLoom старше суток. Активные запуски, checkpoints, проекты, история и кэши пакетов сохраняются.', 'Remove only detached DepLoom garbage older than one day. Active runs, checkpoints, projects, history and package caches are preserved.')}</p>
      {result ? <div>
        <p>{result.root} · {result.filesystem || text('тип диска неизвестен', 'filesystem unknown')}</p>
        <p>{text('Свободно на диске', 'Free disk space')}: {(result.freeBytes / 1024 ** 3).toFixed(1)} {text('ГиБ', 'GiB')}</p>
        <p>{text('Найдено каталогов для очистки', 'Garbage directories found')}: {result.eligible}</p>
        <p>{text('Удалено', 'Removed')}: {result.removed} · {text('защищено', 'protected')}: {result.protected} · {text('не удалось удалить', 'failed to remove')}: {result.failed}</p>
      </div> : null}
      {busy ? <p role="status">{text('Обрабатываем хранилище. Большие каталоги могут удаляться несколько минут…', 'Processing storage. Large directories can take several minutes to remove…')}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      <div className="dialog-actions"><button className="button secondary" disabled={busy} onClick={() => void run('inspect')}>{text('Проверить хранилище', 'Inspect storage')}</button><button className="button primary" disabled={busy || !result || result.eligible <= result.removed + result.protected + result.failed} onClick={() => void run('clean')}>{text('Очистить безопасный мусор', 'Clean safe garbage')}</button></div>
    </section>
  </div>
}
