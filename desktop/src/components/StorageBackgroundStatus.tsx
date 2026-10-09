import type { StorageStatus } from '../types'
import { useLanguage } from '../i18n'

export function StorageBackgroundStatus({ status, onOpen }: { status?: StorageStatus; onOpen: () => void }) {
  const { language } = useLanguage()
  if (!status || status.phase === 'idle') return null
  const text = (ru: string, en: string) => language === 'ru' ? ru : en
  const running = status.phase === 'inspect' || status.phase === 'clean'
  const percent = status.progress?.stage === 'clean' ? status.progress.percent : null
  const summary = status.phase === 'failed' ? text('ошибка — открыть', 'error — open') : running ? text('в фоне', 'in background') : text('завершено — открыть', 'finished — open')
  return <button className="storage-background-status" onClick={onOpen}>
    {text('Очистка', 'Storage')}: {summary}
    {running ? <progress max={100} value={percent ?? undefined} aria-label={text('Прогресс очистки', 'Cleanup progress')} /> : null}
    {running && percent != null ? ` ${percent}%` : ''}
  </button>
}
