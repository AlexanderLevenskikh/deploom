import { useState } from 'react'
import { FolderGit2, GitFork, LoaderCircle } from 'lucide-react'

type Props = {
  busy: boolean
  onSelectExisting: () => Promise<void>
  onPickParent: () => Promise<string | undefined>
  onCreate: (input: { parentPath: string; folderName: string; teamRemote?: string }) => Promise<void>
}

export function SetupScreen({ busy, onSelectExisting, onPickParent, onCreate }: Props) {
  const [parentPath, setParentPath] = useState('')
  const [folderName, setFolderName] = useState('frontend-deps-workspace')
  const [teamRemote, setTeamRemote] = useState('')

  const chooseParent = async () => {
    const path = await onPickParent()
    if (path) setParentPath(path)
  }

  return (
    <main className="setup-screen">
      <section className="setup-panel" aria-labelledby="setup-title">
        <div className="setup-heading">
          <div className="brand-mark"><GitFork size={21} /></div>
          <div>
            <h1 id="setup-title">Подключите рабочий набор команды</h1>
            <p>В нём живут конфиги, история, dashboard-state, knowledge и артефакты обновлений.</p>
          </div>
        </div>

        <div className="setup-choice">
          <div className="setup-choice-copy">
            <FolderGit2 size={20} />
            <div><strong>Уже есть локальная копия</strong><span>Выберите существующий Git-репозиторий workspace — старые template-based workspace продолжают работать без миграции.</span></div>
          </div>
          <button className="button primary" disabled={busy} onClick={() => void onSelectExisting()}>Выбрать папку</button>
        </div>

        <div className="setup-divider"><span>или создать новый workspace</span></div>

        <form className="setup-form" onSubmit={(event) => { event.preventDefault(); void onCreate({ parentPath, folderName, teamRemote: teamRemote || undefined }) }}>
          <label>Родительская папка<div className="input-action"><input value={parentPath} onChange={(event) => setParentPath(event.target.value)} placeholder="Выберите папку" /><button type="button" className="icon-button" title="Выбрать папку" onClick={() => void chooseParent()}><FolderGit2 size={17} /></button></div></label>
          <label>Имя папки<input value={folderName} onChange={(event) => setFolderName(event.target.value)} /></label>
          <label>Командный remote <span className="optional">необязательно</span><input value={teamRemote} onChange={(event) => setTeamRemote(event.target.value)} placeholder="git@git.example/team/dependency-roadmap.git" /></label>
          <div className="setup-note">DepLoom сам создаст Git-репозиторий, <code>.dependency-roadmap</code>, начальный конфиг и commit. Если указан командный remote, он будет добавлен как <code>origin</code>.</div>
          <button className="button primary wide" disabled={busy || !parentPath.trim() || !folderName.trim()}>{busy ? <LoaderCircle className="spin" size={17} /> : <GitFork size={17} />} Создать и подключить</button>
        </form>
      </section>
    </main>
  )
}
