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
  const { text } = useLanguage()
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
            <h2 id="workspace-dialog-title">{text('Добавить workspace', 'Add workspace')}</h2>
            <p>{text('Workspace хранит общую registry/policy конфигурацию. Проекты с разной registry-политикой удобно держать в разных workspace.', 'A workspace owns shared registry/policy configuration. Projects with different registry policies are best kept in separate workspaces.')}</p>
          </div>
          <button type="button" className="icon-button" disabled={busy} aria-label={text('Закрыть', 'Close')} onClick={onClose}><X size={17} /></button>
        </div>

        <div className="workspace-connect-card">
          <div><FolderGit2 size={19} /><div><strong>{text('Подключить существующий', 'Connect existing')}</strong><span>{text('Выберите уже созданный Git workspace.', 'Choose an existing Git workspace.')}</span></div></div>
          <button type="button" className="button secondary" disabled={busy} onClick={() => void connectExisting()}>{text('Выбрать папку', 'Choose folder')}</button>
        </div>

        <div className="setup-divider"><span>{text('или создать новый', 'or create new')}</span></div>

        <form className="workspace-create-form" onSubmit={(event) => void create(event)}>
          <label>{text('Родительская папка', 'Parent folder')}<div className="input-action"><input value={parentPath} onChange={(event) => setParentPath(event.target.value)} /><button type="button" className="icon-button" disabled={busy} onClick={() => void chooseParent()}><FolderGit2 size={17} /></button></div></label>
          <label>{text('Имя папки workspace', 'Workspace folder name')}<input value={folderName} onChange={(event) => setFolderName(event.target.value)} /></label>
          <label>{text('Team remote', 'Team remote')} <span className="optional">{text('необязательно', 'optional')}</span><input value={teamRemote} onChange={(event) => setTeamRemote(event.target.value)} placeholder="git@example/team/dependency-roadmap.git" /></label>
          <div className="workspace-registry-note">{text('Новый workspace создаётся без registry override: по умолчанию используется публичный npm registry. Nexus можно задать позже в settings.project.json.', 'A new workspace is created without a registry override: the public npm registry is used by default. Nexus can be configured later in settings.project.json.')}</div>
          {error ? <div className="dialog-error" role="alert">{error}</div> : null}
          <div className="dialog-actions">
            <button type="button" className="button secondary" disabled={busy} onClick={onClose}>{text('Отмена', 'Cancel')}</button>
            <button type="submit" className="button primary" disabled={busy || !parentPath.trim() || !folderName.trim()}><GitFork size={15} /> {busy ? text('Создаю…', 'Creating…') : text('Создать workspace', 'Create workspace')}</button>
          </div>
        </form>
      </section>
    </div>
  )
}
