import { FolderGit2, GitFork, X } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useLanguage } from '../i18n'

type Props = {
  onClose: () => void
  onPickDirectory: () => Promise<string | undefined>
  onConnectExisting: (path: string) => Promise<void>
  onCreate: (input: { parentPath: string; folderName: string; teamRemote?: string }) => Promise<void>
}

export function WorkspaceDialog({ onClose, onPickDirectory, onConnectExisting, onCreate }: Props) {
  const { t } = useLanguage()
  const [parentPath, setParentPath] = useState('')
  const [folderName, setFolderName] = useState('deploom-workspace')
  const [teamRemote, setTeamRemote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  const chooseParent = async () => {
    setError(undefined)
    try {
      const path = await onPickDirectory()
      if (path) setParentPath(path)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const connectExisting = async () => {
    if (busy) return
    setError(undefined)
    try {
      const path = await onPickDirectory()
      if (!path) return
      setBusy(true)
      await onConnectExisting(path)
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !parentPath.trim() || !folderName.trim()) return
    setBusy(true)
    setError(undefined)
    try {
      await onCreate({
        parentPath: parentPath.trim(),
        folderName: folderName.trim(),
        teamRemote: teamRemote.trim() || undefined,
      })
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose() }}>
      <section className="dialog workspace-dialog" role="dialog" aria-modal="true" aria-labelledby="workspace-dialog-title">
        <div className="dialog-title">
          <div>
            <h2 id="workspace-dialog-title">{t('workspaceDialog.title')}</h2>
            <p>{t('workspaceDialog.description')}</p>
          </div>
          <button type="button" className="icon-button" disabled={busy} aria-label={t('common.close')} onClick={onClose}><X size={17} /></button>
        </div>

        <div className="workspace-connect-card">
          <div><FolderGit2 size={19} /><div><strong>{t('workspaceDialog.connect.title')}</strong><span>{t('workspaceDialog.connect.description')}</span></div></div>
          <button type="button" className="button secondary" disabled={busy} onClick={() => void connectExisting()}>{t('workspaceDialog.connect.choose')}</button>
        </div>

        <div className="setup-divider"><span>{t('workspaceDialog.orCreate')}</span></div>

        <form className="workspace-create-form" onSubmit={(event) => void create(event)}>
          <label>{t('workspaceDialog.parentFolder')}<div className="input-action"><input value={parentPath} onChange={(event) => setParentPath(event.target.value)} /><button type="button" className="icon-button" disabled={busy} aria-label={t('workspaceDialog.parentFolder')} onClick={() => void chooseParent()}><FolderGit2 size={17} /></button></div></label>
          <label>{t('workspaceDialog.folderName')}<input value={folderName} onChange={(event) => setFolderName(event.target.value)} /></label>
          <label>{t('workspaceDialog.teamRemote')} <span className="optional">{t('common.optional')}</span><input value={teamRemote} onChange={(event) => setTeamRemote(event.target.value)} placeholder="git@example.invalid/team/dependency-roadmap.git" /></label>
          <div className="workspace-registry-note">{t('workspaceDialog.registryNote')}</div>
          {error ? <div className="dialog-error" role="alert">{error}</div> : null}
          <div className="dialog-actions">
            <button type="button" className="button secondary" disabled={busy} onClick={onClose}>{t('common.cancel')}</button>
            <button type="submit" className="button primary" disabled={busy || !parentPath.trim() || !folderName.trim()}><GitFork size={15} /> {busy ? t('workspaceDialog.creating') : t('workspaceDialog.create')}</button>
          </div>
        </form>
      </section>
    </div>
  )
}
