import { Download, RefreshCw } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { UpdateStatus } from '../types'
import { useLanguage } from '../i18n'
import './AppUpdateControl.css'
export function AppUpdateControl({ status, currentVersion, blocked, onCheck, onInstall }: { status: UpdateStatus; currentVersion: string; blocked: boolean; onCheck: () => Promise<void>; onInstall: () => Promise<void> }) {
  const { text } = useLanguage()
  const [error, setError] = useState<string>()
  const [operations, setOperations] = useState<string[]>([])
  const [stopping, setStopping] = useState(false)
  const [observed, setObserved] = useState(false)
  useEffect(() => {
    if (status.state !== 'ready' || !window.dependencyFlow?.updateOperations) return
    let disposed = false
    let pending = false
    const refresh = async () => {
      if (pending) return
      pending = true
      try { const next = await window.dependencyFlow!.updateOperations(); if (!disposed) { setOperations(next); setObserved(true) } }
      catch (error) { if (!disposed) { setObserved(false); setError(String(error)) } }
      finally { pending = false }
    }
    void refresh()
    const timer = setInterval(() => void refresh(), 2000)
    return () => { disposed = true; clearInterval(timer) }
  }, [status.state])
  const restartBlocked = observed ? operations.length > 0 : blocked
  const statusMessage = observed && !restartBlocked && status.message?.startsWith('UPDATE_BUSY:') ? undefined : status.message
  const stopAll = async () => {
    setStopping(true); setError(undefined)
    try {
      const result = await window.dependencyFlow!.stopAllOperations()
      setOperations(result.remaining); setObserved(true)
      if (result.remaining.length) setError(text('Операции ещё останавливаются. Дождитесь освобождения файлов.', 'Operations are still stopping. Wait for files to be released.'))
    } catch (reason) { setError(String(reason)) } finally { setStopping(false) }
  }
  const busy = status.state === 'checking' || status.state === 'downloading'
  const label = status.state === 'idle' ? text('Обновления DepLoom', 'DepLoom updates') : status.state === 'checking' ? text('Проверяем обновления…', 'Checking for updates…') : status.state === 'available' ? text('Доступно обновление', 'Update available') : status.state === 'downloading' ? text('Загрузка обновления', 'Downloading update') + (typeof status.percent === 'number' ? ' · ' + Math.round(status.percent) + '%' : '') : status.state === 'ready' ? text('Готово к установке', 'Ready to install') : status.state === 'error' ? text('Ошибка обновления', 'Update error') : text('Установлена актуальная версия', 'Up to date')
  const invoke = async (action: () => Promise<void>) => { try { setError(undefined); await action() } catch (reason) { setError(String(reason)) } }
  return <details className="app-update-control"><summary aria-label={label}><Download size={16} /><span>{label}</span></summary><div className="app-update-popover"><strong>DepLoom {currentVersion}</strong><p role="status">{label}{status.version ? ' · ' + status.version : ''}</p>{statusMessage || error ? <p role="alert">{error || statusMessage}</p> : null}{status.state === 'downloading' ? <progress max={100} value={typeof status.percent === 'number' ? status.percent : undefined} aria-label={text('Загрузка DepLoom', 'DepLoom download')} /> : null}{status.state === 'ready' ? <><button className="button primary" disabled={restartBlocked || stopping} onClick={() => void invoke(onInstall)}>{text('Установить и перезапустить', 'Install and restart')}</button>{restartBlocked ? <div><p>{text('Перезапуск ожидает завершения:', 'Restart is waiting for:')}</p>{operations.length ? <ul>{operations.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul> : <p>{text('Активных операций проекта или очистки', 'Active project or cleanup operations')}</p>}<button className="button secondary" disabled={stopping} onClick={() => void stopAll()}>{stopping ? text('Останавливаем операции…', 'Stopping operations…') : text('Остановить все операции', 'Stop all operations')}</button></div> : null}</> : <button className="button secondary" disabled={busy} onClick={() => void invoke(onCheck)}><RefreshCw size={14} />{status.state === 'error' ? text('Повторить проверку', 'Retry check') : text('Проверить обновления', 'Check for updates')}</button>}</div></details>
}
