import { FolderGit2, X } from 'lucide-react'
import { useState, type FormEvent } from 'react'
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
  const [submitError, setSubmitError] = useState<string>()

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setSubmitError(undefined)
    try {
      await onSubmit({
        workspaceId,
        name: name.trim(),
        path: path.trim(),
        sourceBranch: sourceBranch.trim(),
        baseBranch: baseBranch.trim(),
        mergedBranch: mergedBranch.trim(),
      })
      onClose()
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const pickDirectory = async () => {
    setSubmitError(undefined)
    try {
      const value = await onPickDirectory()
      if (value) setPath(value)
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error))
    }
  }

  const requestClose = () => {
    if (!busy) onClose()
  }

  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose() }}>
    <form className="dialog" onSubmit={(event) => void submit(event)}>
      <div className="dialog-title"><div><h2>{t('addProject.title')}</h2><p>{t('addProject.description')}</p></div><button type="button" className="icon-button" title={t('common.close')} disabled={busy} onClick={requestClose}><X size={17} /></button></div>
      <label>{t('addProject.name')}<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Demo.App" autoFocus /></label>
      <label>{t('addProject.localFolder')}<div className="input-action"><input value={path} onChange={(event) => setPath(event.target.value)} /><button type="button" className="icon-button" title={t('setup.chooseFolder')} disabled={busy} onClick={() => void pickDirectory()}><FolderGit2 size={17} /></button></div></label>
      <div className="dialog-grid"><label>Source branch<input value={sourceBranch} onChange={(event) => setSourceBranch(event.target.value)} /></label><label>Base branch<input value={baseBranch} onChange={(event) => { setBaseBranch(event.target.value); setMergedBranch(`${event.target.value}-merged`) }} /></label></div>
      <label>Merged branch<input value={mergedBranch} onChange={(event) => setMergedBranch(event.target.value)} /></label>
      {submitError ? <div className="dialog-error" role="alert">{submitError}</div> : null}
      <div className="dialog-actions"><button type="button" className="button secondary" disabled={busy} onClick={requestClose}>{t('common.cancel')}</button><button type="submit" className="button primary" disabled={busy || !name.trim() || !path.trim()}>{busy ? t('addProject.adding') : t('addProject.add')}</button></div>
    </form>
  </div>
}
