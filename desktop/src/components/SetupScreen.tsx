import { useState } from 'react'
import { FolderGit2, GitFork, LoaderCircle } from 'lucide-react'
import { useLanguage } from '../i18n'

type Props = {
  busy: boolean
  onSelectExisting: () => Promise<void>
  onPickParent: () => Promise<string | undefined>
  onCreate: (input: { parentPath: string; folderName: string; teamRemote?: string }) => Promise<void>
}

export function SetupScreen({ busy, onSelectExisting, onPickParent, onCreate }: Props) {
  const { t } = useLanguage()
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
            <h1 id="setup-title">{t('setup.title')}</h1>
            <p>{t('setup.subtitle')}</p>
          </div>
        </div>

        <div className="setup-choice">
          <div className="setup-choice-copy">
            <FolderGit2 size={20} />
            <div><strong>{t('setup.existing.title')}</strong><span>{t('setup.existing.description')}</span></div>
          </div>
          <button className="button primary" disabled={busy} onClick={() => void onSelectExisting()}>{t('setup.chooseFolder')}</button>
        </div>

        <div className="setup-divider"><span>{t('setup.orCreate')}</span></div>

        <form className="setup-form" onSubmit={(event) => { event.preventDefault(); void onCreate({ parentPath, folderName, teamRemote: teamRemote || undefined }) }}>
          <label>{t('setup.parentFolder')}<div className="input-action"><input value={parentPath} onChange={(event) => setParentPath(event.target.value)} placeholder={t('setup.chooseFolder')} /><button type="button" className="icon-button" title={t('setup.chooseFolder')} onClick={() => void chooseParent()}><FolderGit2 size={17} /></button></div></label>
          <label>{t('setup.folderName')}<input value={folderName} onChange={(event) => setFolderName(event.target.value)} /></label>
          <label>{t('setup.teamRemote')} <span className="optional">{t('common.optional')}</span><input value={teamRemote} onChange={(event) => setTeamRemote(event.target.value)} placeholder="git@git.example/team/dependency-roadmap.git" /></label>
          <div className="setup-note">{t('setup.note')}</div>
          <button className="button primary wide" disabled={busy || !parentPath.trim() || !folderName.trim()}>{busy ? <LoaderCircle className="spin" size={17} /> : <GitFork size={17} />} {busy ? t('setup.creating') : t('setup.create')}</button>
        </form>
      </section>
    </main>
  )
}
