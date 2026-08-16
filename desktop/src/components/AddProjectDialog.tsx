import { FolderGit2, X } from 'lucide-react'
import { useState } from 'react'
import { useLanguage } from '../i18n'

type ProjectInput = { workspaceId?: string; name: string; path: string; sourceBranch?: string; baseBranch?: string; mergedBranch?: string }
type Props = { workspaceId: string; onClose: () => void; onPickDirectory: () => Promise<string | undefined>; onSubmit: (input: ProjectInput) => Promise<void> }

export function AddProjectDialog({ workspaceId, onClose, onPickDirectory, onSubmit }: Props) {
  const { t } = useLanguage()
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [sourceBranch, setSourceBranch] = useState('master')
  const [baseBranch, setBaseBranch] = useState('libs')
  const [mergedBranch, setMergedBranch] = useState('libs-merged')
  const [busy, setBusy] = useState(false)
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <form className="dialog" onSubmit={(event) => { event.preventDefault(); setBusy(true); void onSubmit({ workspaceId, name, path, sourceBranch, baseBranch, mergedBranch }).then(onClose).finally(() => setBusy(false)) }}>
      <div className="dialog-title"><div><h2>{t('addProject.title')}</h2><p>{t('addProject.description')}</p></div><button type="button" className="icon-button" title={t('common.close')} onClick={onClose}><X size={17} /></button></div>
      <label>{t('addProject.name')}<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Admin.App" autoFocus /></label>
      <label>{t('addProject.localFolder')}<div className="input-action"><input value={path} onChange={(event) => setPath(event.target.value)} /><button type="button" className="icon-button" title={t('setup.chooseFolder')} onClick={() => void onPickDirectory().then((value) => value && setPath(value))}><FolderGit2 size={17} /></button></div></label>
      <div className="dialog-grid"><label>Source branch<input value={sourceBranch} onChange={(event) => setSourceBranch(event.target.value)} /></label><label>Base branch<input value={baseBranch} onChange={(event) => { setBaseBranch(event.target.value); setMergedBranch(`${event.target.value}-merged`) }} /></label></div>
      <label>Merged branch<input value={mergedBranch} onChange={(event) => setMergedBranch(event.target.value)} /></label>
      <div className="dialog-actions"><button type="button" className="button secondary" onClick={onClose}>{t('common.cancel')}</button><button className="button primary" disabled={busy || !name.trim() || !path.trim()}>{busy ? t('addProject.adding') : t('addProject.add')}</button></div>
    </form>
  </div>
}
