import { FolderGit2, RefreshCw, Search, X } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { useLanguage } from '../i18n'
import type { ProjectDiscoveryCandidate, ProjectDiscoveryResponse } from '../types'

type ProjectInput = { workspaceId?: string; name: string; path: string; packagePath?: string; repositoryPath?: string; sourceBranch?: string; baseBranch?: string; mergedBranch?: string }
type Props = { workspaceId: string; onClose: () => void; onPickDirectory: () => Promise<string | undefined>; onDiscoverProject: (input: { path: string; deep?: boolean }) => Promise<ProjectDiscoveryResponse>; onSubmit: (input: ProjectInput) => Promise<void> }

const SELECTABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function focusTrap(root: HTMLDivElement | null): HTMLInputElement | null {
  if (!root) return null
  const focusable = Array.from(root.querySelectorAll<HTMLElement>(SELECTABLE))
  return (focusable.find((item) => item instanceof HTMLInputElement) as HTMLInputElement | undefined) ?? null
}

export function AddProjectDialog({ workspaceId, onClose, onPickDirectory, onDiscoverProject, onSubmit }: Props) {
  const { t } = useLanguage()
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [repositoryPath, setRepositoryPath] = useState('')
  const [sourceBranch, setSourceBranch] = useState('master')
  const [baseBranch, setBaseBranch] = useState('libs')
  const [mergedBranch, setMergedBranch] = useState('libs-merged')
  const [busy, setBusy] = useState(false)
  const [submitError, setSubmitError] = useState<string>()
  const [discovery, setDiscovery] = useState<ProjectDiscoveryResponse>()
  const [scanDeep, setScanDeep] = useState(false)
  const [discovering, setDiscovering] = useState(false)
  const [discoveryError, setDiscoveryError] = useState<string>()
  const [filter, setFilter] = useState('')
  const [candidatePath, setCandidatePath] = useState<string>()
  const rootRef = useRef<HTMLDivElement>(null)
  const restoreRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (typeof document === 'undefined') return
    restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const root = rootRef.current
    if (!root) return
    const next = focusTrap(root)
    if (next) next.focus()
    const trap = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Tab' || !rootRef.current) return
      const focusable = Array.from(rootRef.current.querySelectorAll<HTMLElement>(SELECTABLE))
      if (!focusable.length) { event.preventDefault(); return }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const current = document.activeElement instanceof HTMLElement ? document.activeElement : null
      if (event.shiftKey) {
        if (!current || current === first || !rootRef.current.contains(current)) { event.preventDefault(); last.focus() }
      } else if (!current || current === last || !rootRef.current.contains(current)) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener('keydown', trap, true)
    return () => {
      document.removeEventListener('keydown', trap, true)
      restoreRef.current?.focus()
    }
  }, [])

  const runDiscovery = async (folder: string, deep: boolean) => {
    if (!folder.trim()) { setDiscovery(undefined); setCandidatePath(undefined); return }
    setDiscovering(true)
    setDiscoveryError(undefined)
    try {
      const response = await onDiscoverProject({ path: folder.trim(), deep })
      setDiscovery(response)
      setScanDeep(deep)
      setCandidatePath(response.status === 'explicit' || response.status === 'single' ? response.path : undefined)
    } catch (error) {
      setDiscoveryError(error instanceof Error ? error.message : String(error))
      setDiscovery(undefined)
      setCandidatePath(undefined)
    } finally {
      setDiscovering(false)
    }
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !path.trim()) return
    setBusy(true)
    setSubmitError(undefined)
    try {
      await onSubmit({
        workspaceId,
        name: name.trim(),
        path: path.trim(),
        packagePath: candidatePath || undefined,
        repositoryPath: repositoryPath.trim() || undefined,
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

  const pickDirectory = async (repository = false) => {
    setSubmitError(undefined)
    try {
      const value = await onPickDirectory()
      if (value) {
        if (repository) {
          setRepositoryPath(value)
        } else {
          setPath(value)
          setCandidatePath(undefined)
          void runDiscovery(value, scanDeep)
        }
      }
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error))
    }
  }

  const onPathBlur = () => { if (!path.trim()) { setDiscovery(undefined); setCandidatePath(undefined) } else { void runDiscovery(path, scanDeep) } }

  const requestClose = () => { if (!busy) onClose() }

  const onRootKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); requestClose() }
  }

  const suggestions = discovery?.status === 'missing' && discovery.report.deeperManifests > 0 && !scanDeep
  const ambiguous = discovery?.status === 'ambiguous'
  const groupKey = (candidate: ProjectDiscoveryCandidate) => candidate.lockfilePath || candidate.lockfileName || 'none'
  const normalized = filter.trim().toLowerCase()
  const visible = ambiguous && discovery
    ? [...discovery.report.candidates].sort((left, right) => {
      const g = groupKey(left).localeCompare(groupKey(right))
      return g !== 0 ? g : left.relative.localeCompare(right.relative)
    }).filter((candidate) => !normalized || candidate.relative.toLowerCase().includes(normalized) || candidate.packageName?.toLowerCase().includes(normalized))
    : []

  const canAdd = Boolean(name.trim() && path.trim() && (discovery?.status === 'explicit' || discovery?.status === 'single' || (discovery?.status === 'ambiguous' && candidatePath)))

  return <div ref={rootRef} className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose() }} onKeyDown={onRootKeyDown}>
    <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-project-dialog-title" onSubmit={(event) => void submit(event)}>
      <div className="dialog-title"><div><h2 id="add-project-dialog-title">{t('addProject.title')}</h2><p>{t('addProject.description')}</p></div><button type="button" className="icon-button" title={t('common.close')} aria-label={t('common.close')} disabled={busy} onClick={requestClose}><X size={17} /></button></div>
      <label>{t('addProject.name')}<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Demo.App" autoFocus /></label>
      <label>{t('addProject.gitFolder')}<div className="input-action"><input value={repositoryPath} onChange={event => setRepositoryPath(event.target.value)} placeholder={t('addProject.autoGit')} /><button type="button" className="icon-button" title={t('setup.chooseFolder')} aria-label={t('setup.chooseFolder')} disabled={busy} onClick={() => void pickDirectory(true)}><FolderGit2 size={17} /></button></div></label>
      <label>{t('addProject.localFolder')}<div className="input-action"><input value={path} onChange={(event) => { setPath(event.target.value); setCandidatePath(undefined) }} onBlur={onPathBlur} /><button type="button" className="icon-button" title={t('setup.chooseFolder')} aria-label={t('setup.chooseFolder')} disabled={busy} onClick={() => void pickDirectory()}><FolderGit2 size={17} /></button></div></label>
      <p className="setup-note">{t('addProject.scopeHint')}</p>
      {discovering ? <p className="setup-note" role="status">{t('addProject.scanning')}</p> : null}
      {discoveryError ? <div className="dialog-error" role="alert">{discoveryError}</div> : null}
      {suggestions ? (
        <div className="project-candidates" role="group" aria-label={t('addProject.deeperAria')}>
          <p className="setup-note">{t('addProject.deeperText', { maxDepth: discovery.report.maxDepth })}</p>
          <div className="dialog-actions"><button type="button" className="button secondary" disabled={busy} onClick={() => void runDiscovery(path, true)}><RefreshCw size={14} /> {t('addProject.expandScan')}</button></div>
        </div>
      ) : null}
      {ambiguous && discovery ? (
        <div className="project-candidates" role="group" aria-label={t('addProject.ambiguousAria')}>
          <label>{t('addProject.chooseCandidate')}</label>
          {discovery.report.truncated && discovery.report.deeperManifests > 0 && !scanDeep ? <p className="setup-note">{t('addProject.depthLimitedText', { maxDepth: discovery.report.maxDepth })} <button type="button" className="link-button" disabled={busy} onClick={() => void runDiscovery(path, true)}>{t('addProject.expandScan')}</button></p> : null}
          <div className="project-candidate-search"><Search size={14} /><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t('addProject.filterByPath')} aria-label={t('addProject.filterAria')} /></div>
          <ul className="project-candidate-list">
            {visible.map((candidate) => (
              <li key={candidate.path}>
                <label className={candidatePath === candidate.path ? 'selected' : ''}>
                  <input type="radio" name="project-candidate" checked={candidatePath === candidate.path} disabled={busy} onChange={() => setCandidatePath(candidate.path)} />
                  <span className="project-candidate-row">
                    <span className="project-candidate-path">{candidate.relative || '.'}{candidate.packageName ? <small> · {candidate.packageName}</small> : null}</span>
                    <span className="project-candidate-lock">{candidate.lockfileName ? `${candidate.lockfileName} ${t('addProject.depthSuffix', { depth: candidate.depth })}` : t('addProject.noLockfile')}</span>
                  </span>
                </label>
              </li>
            ))}
            {visible.length === 0 ? <li className="project-candidate-empty">{t('addProject.noCandidates')}</li> : null}
          </ul>
        </div>
      ) : null}
      {discovery?.status === 'missing' && discovery.report.candidates.length === 0 && discovery.report.deeperManifests === 0 && !discovering ? (
        <p className="setup-note">{t('addProject.missingText')}</p>
      ) : null}
      {discovery?.status === 'explicit' && candidatePath ? <p className="setup-note">{t('addProject.explicitText')}</p> : null}
      {discovery?.status === 'single' && candidatePath ? <p className="setup-note">{t('addProject.singleText', { path: candidatePath })}</p> : null}
      <div className="dialog-grid"><label>{t('addProject.sourceBranch')}<input value={sourceBranch} onChange={(event) => setSourceBranch(event.target.value)} /></label><label>{t('addProject.targetBranch')}<input value={baseBranch} onChange={(event) => { setBaseBranch(event.target.value); setMergedBranch(`${event.target.value}-merged`) }} /></label></div>
      <details><summary>{t('addProject.advanced')}</summary><div className="dialog-grid"><label>{t('addProject.mergedBranch')}<input value={mergedBranch} onChange={(event) => setMergedBranch(event.target.value)} /></label></div></details>
      {submitError ? <div className="dialog-error" role="alert">{submitError}</div> : null}
      <div className="dialog-actions"><button type="button" className="button secondary" disabled={busy} onClick={requestClose}>{t('common.cancel')}</button><button type="submit" className="button primary" disabled={busy || !canAdd}>{busy ? t('addProject.adding') : t('addProject.add')}</button></div>
    </form>
  </div>
}
