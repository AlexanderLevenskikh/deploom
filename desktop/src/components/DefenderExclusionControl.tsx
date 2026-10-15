import { useState } from 'react'
import { Shield } from 'lucide-react'
import { useLanguage } from '../i18n'
export function DefenderExclusionControl({ workspaceId }: { workspaceId: string }) {
  const { text } = useLanguage()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  if (!/Windows/i.test(navigator.userAgent)) return null
  const add = async () => {
    setBusy(true); setMessage('')
    try {
      const result = await window.dependencyFlow!.addDefenderExclusion(workspaceId)
      if (!result.canceled) setMessage(text('Исключение добавлено: ', 'Exclusion added: ') + result.path)
    } catch (error) { setMessage(String(error)) } finally { setBusy(false) }
  }
  return <div><button className="button secondary" disabled={busy} onClick={() => void add()}><Shield size={16} />{busy ? text('Ожидаем Windows…', 'Waiting for Windows…') : text('Добавить в исключения Защитника Windows', 'Add Windows Defender exclusion')}</button>{message ? <p role="status">{message}</p> : null}</div>
}
