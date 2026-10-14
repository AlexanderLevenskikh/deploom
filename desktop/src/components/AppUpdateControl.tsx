import { Download, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import type { UpdateStatus } from '../types'
import { useLanguage } from '../i18n'
import './AppUpdateControl.css'
export function AppUpdateControl({ status, currentVersion, blocked, onCheck, onInstall }: { status: UpdateStatus; currentVersion: string; blocked: boolean; onCheck: () => Promise<void>; onInstall: () => Promise<void> }) {
  const { text } = useLanguage()
  const [error, setError] = useState<string>()
  const busy = status.state === 'checking' || status.state === 'downloading'
  const label = status.state === 'idle' ? text('Обновления DepLoom', 'DepLoom updates') : status.state === 'checking' ? text('Проверяем обновления…', 'Checking for updates…') : status.state === 'available' ? text('Доступно обновление', 'Update available') : status.state === 'downloading' ? text('Загрузка обновления', 'Downloading update') + (typeof status.percent === 'number' ? ' · ' + Math.round(status.percent) + '%' : '') : status.state === 'ready' ? text('Готово к установке', 'Ready to install') : status.state === 'error' ? text('Ошибка обновления', 'Update error') : text('Установлена актуальная версия', 'Up to date')
  const invoke = async (action: () => Promise<void>) => { try { setError(undefined); await action() } catch (reason) { setError(String(reason)) } }
  return <details className="app-update-control"><summary aria-label={label}><Download size={16} /><span>{label}</span></summary><div className="app-update-popover"><strong>DepLoom {currentVersion}</strong><p role="status">{label}{status.version ? ' · ' + status.version : ''}</p>{status.message || error ? <p role="alert">{error || status.message}</p> : null}{status.state === 'downloading' ? <progress max={100} value={typeof status.percent === 'number' ? status.percent : undefined} aria-label={text('Загрузка DepLoom', 'DepLoom download')} /> : null}{status.state === 'ready' ? <><button className="button primary" disabled={blocked} onClick={() => void invoke(onInstall)}>{text('Установить и перезапустить', 'Install and restart')}</button>{blocked ? <p>{text('Завершите или остановите активные операции перед перезапуском.', 'Finish or stop active operations before restarting.')}</p> : null}</> : <button className="button secondary" disabled={busy} onClick={() => void invoke(onCheck)}><RefreshCw size={14} />{status.state === 'error' ? text('Повторить проверку', 'Retry check') : text('Проверить обновления', 'Check for updates')}</button>}</div></details>
}
