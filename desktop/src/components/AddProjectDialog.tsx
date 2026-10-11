import { FolderGit2, RefreshCw, Search, X } from 'lucide-react'
import { useRef, useState, type FormEvent } from 'react'
import { useLanguage } from '../i18n'
import { useDialogFocus } from '../hooks/useDialogFocus'
import type { ProjectDiscoveryCandidate, ProjectDiscoveryResponse } from '../types'

type ProjectInput = { workspaceId?: string; name: string; path: string; packagePath?: string; repositoryPath?: string; sourceBranch?: string; baseBranch?: string; mergedBranch?: string }
type Props = { workspaceId: string; onClose: () => void; onPickDirectory: () => Promise<string | undefined>; onDiscoverProject: (input: { path: string; deep?: boolean }) => Promise<ProjectDiscoveryResponse>; onSubmit: (input: ProjectInput) => Promise<void> }

function baseName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, '')
  const parts = trimmed.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] ?? trimmed
}

export function AddProjectDialog({ workspaceId, onClose, onPickDirectory, onDiscoverProject, onSubmit }: Props) {
  const { t } = useLanguage()
  const rootRef = useRef<HTMLDivElement>(null)
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [repositoryPath, setRepositoryPath] = useState('')
  const [sourceBranch, setSourceBranch] = useState('')
  const [baseBranch, setBaseBranch] = useState('libs')
  const [mergedBranch, setMergedBranch] = useState('libs-merged')
  const [busy, setBusy] = useState(false)
  const [submitError, setSubmitError] = useState<string>()
  const [discovery, setDiscovery] = useState<ProjectDiscoveryResponse>()
  const [scanDeep, setScanDeep] = useState(false)
  const [discovering, setDiscovering] = useState(false)
  const [discoveryError, setDiscoveryError] = useState<string>()
  const [filter, setFilter] = useState('')
  const [selectedPaths, setSelectedPaths] = useState<string[]>([])
  useDialogFocus(rootRef, { onClose: () => { if (!busy) onClose() } })
  // Guards against stale discovery responses: a response that arrives after the
  // path was changed again (or a newer scan started) is discarded, so the
  // chooser can never show candidates for the wrong folder.
  const discoveryRequestRef = useRef(0)

  const runDiscovery = async (folder: string, deep: boolean) => {
    const trimmed = folder.trim()
    if (!trimmed) { setDiscovery(undefined); setSelectedPaths([]); return }
    const requestId = ++discoveryRequestRef.current
    setDiscovering(true)
    setDiscoveryError(undefined)
    try {
      const response = await onDiscoverProject({ path: trimmed, deep })
      // Discard stale responses: only apply the result when THIS request is
      // still the newest and the path was not changed since it started.
      if (requestId !== discoveryRequestRef.current) return
      setDiscovery(response)
      setScanDeep(deep)
      setSelectedPaths((current) => {
        const valid = response.status === 'explicit' || response.status === 'single'
          ? [response.path]
          : current.filter((selected) => response.report.candidates.some((candidate) => candidate.path === selected))
        return valid
      })
    } catch (error) {
      if (requestId !== discoveryRequestRef.current) return
      setDiscoveryError(error instanceof Error ? error.message : String(error))
      setDiscovery(undefined)
      setSelectedPaths([])
    } finally {
      if (requestId === discoveryRequestRef.current) setDiscovering(false)
    }
  }

  const toggleCandidate = (candidate: ProjectDiscoveryCandidate) => {
    setSelectedPaths((current) => current.includes(candidate.path)
      ? current.filter((item) => item !== candidate.path)
      : [...current, candidate.path])
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !path.trim()) return
    const explicitOrSingle = discovery?.status === 'explicit' || discovery?.status === 'single'
    const selected = discovery?.status === 'ambiguous' ? selectedPaths : []
    if (!explicitOrSingle && selected.length === 0) return
    setBusy(true)
    setSubmitError(undefined)
    try {
      const entries: ProjectInput[] = explicitOrSingle
        ? [{ workspaceId, name: name.trim(), path: path.trim(), packagePath: (discovery?.status === 'single' ? discovery.path : undefined) || undefined, repositoryPath: repositoryPath.trim() || undefined, sourceBranch: sourceBranch.trim() || undefined, baseBranch: baseBranch.trim(), mergedBranch: mergedBranch.trim() }]
        : selected.map((candidate) => {
          const found = discovery?.report.candidates.find((item) => item.path === candidate)
          return {
            workspaceId,
            name: found?.packageName || baseName(found?.path ?? candidate),
            path: path.trim(),
            packagePath: candidate,
            repositoryPath: repositoryPath.trim() || undefined,
            sourceBranch: sourceBranch.trim() || undefined,
            baseBranch: baseBranch.trim(),
            mergedBranch: mergedBranch.trim(),
          }
        })
      for (const entry of entries) await onSubmit(entry)
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
          onPathChange(value)
          void runDiscovery(value, scanDeep)
        }
      }
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error))
    }
  }

  const onPathChange = (value: string) => {
    setPath(value)
    setSelectedPaths([])
    discoveryRequestRef.current += 1
    setDiscovering(false)
    setDiscovery(undefined)
  }

  const onPathBlur = () => { if (!path.trim()) { setDiscovery(undefined); setSelectedPaths([]) } else { void runDiscovery(path, scanDeep) } }

  const requestClose = () => { if (!busy) onClose() }

  const showExpand = Boolean(discovery?.report && discovery.report.truncated && discovery.report.deeperManifests > 0 && !scanDeep)
  const ambiguous = discovery?.status === 'ambiguous'
  const groupKey = (candidate: ProjectDiscoveryCandidate) => candidate.lockfilePath || candidate.lockfileName || 'none'
  const normalized = filter.trim().toLowerCase()
  const visible = ambiguous && discovery
    ? [...discovery.report.candidates].sort((left, right) => {
      const g = groupKey(left).localeCompare(groupKey(right))
      return g !== 0 ? g : left.relative.localeCompare(right.relative)
    }).filter((candidate) => !normalized || candidate.relative.toLowerCase().includes(normalized) || candidate.packageName?.toLowerCase().includes(normalized))
    : []

  const canAdd = Boolean(!discovering && name.trim() && path.trim() && (discovery?.status === 'explicit' || discovery?.status === 'single' || (discovery?.status === 'ambiguous' && selectedPaths.length > 0)))

  return <div ref={rootRef} className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose() }}>
    <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-project-dialog-title" onSubmit={(event) => void submit(event)}>
      <div className="dialog-title"><div><h2 id="add-project-dialog-title">{t('addProject.title')}</h2><p>{t('addProject.description')}</p></div><button type="button" className="icon-button" title={t('common.close')} aria-label={t('common.close')} disabled={busy} onClick={requestClose}><X size={17} /></button></div>
      <label>{t('addProject.name')}<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Demo.App" autoFocus /></label>
      <label>{t('addProject.gitFolder')}<div className="input-action"><input value={repositoryPath} onChange={event => setRepositoryPath(event.target.value)} placeholder={t('addProject.autoGit')} /><button type="button" className="icon-button" title={t('setup.chooseFolder')} aria-label={t('setup.chooseFolder')} disabled={busy} onClick={() => void pickDirectory(true)}><FolderGit2 size={17} /></button></div></label>
      <label>{t('addProject.localFolder')}<div className="input-action"><input value={path} onChange={(event) => onPathChange(event.target.value)} onBlur={onPathBlur} /><button type="button" className="icon-button" title={t('setup.chooseFolder')} aria-label={t('setup.chooseFolder')} disabled={busy} onClick={() => void pickDirectory()}><FolderGit2 size={17} /></button></div></label>
      <p className="setup-note">{t('addProject.scopeHint')}</p>
      {discovering ? <p className="setup-note" role="status">{t('addProject.scanning')}</p> : null}
      {discoveryError ? <div className="dialog-error" role="alert">{discoveryError}</div> : null}
      {showExpand && !ambiguous && discovery ? (
        <div className="project-candidates" role="group" aria-label={t('addProject.deeperAria')}>
          <p className="setup-note">{t('addProject.deeperText', { maxDepth: discovery.report.maxDepth })}</p>
          <div className="dialog-actions"><button type="button" className="button secondary" disabled={busy} onClick={() => void runDiscovery(path, true)}><RefreshCw size={14} /> {t('addProject.expandScan')}</button></div>
        </div>
      ) : null}
      {ambiguous && discovery ? (
        <div className="project-candidates" role="group" aria-label={t('addProject.ambiguousAria')}>
          <label>{t('addProject.chooseCandidate')}</label>
          {showExpand ? <p className="setup-note">{t('addProject.depthLimitedText', { maxDepth: discovery.report.maxDepth })} <button type="button" className="link-button" disabled={busy} onClick={() => void runDiscovery(path, true)}>{t('addProject.expandScan')}</button></p> : null}
          <div className="project-candidate-search"><Search size={14} /><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t('addProject.filterByPath')} aria-label={t('addProject.filterAria')} /></div>
          <ul className="project-candidate-list">
            {visible.map((candidate) => (
              <li key={candidate.path}>
                <label className={selectedPaths.includes(candidate.path) ? 'selected' : ''}>
                  <input type="checkbox" name="project-candidate" checked={selectedPaths.includes(candidate.path)} disabled={busy} onChange={() => toggleCandidate(candidate)} />
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
      {discovery?.status === 'explicit' && discovery.path ? <p className="setup-note">{t('addProject.explicitText')}</p> : null}
      {discovery?.status === 'single' && discovery.path ? <p className="setup-note">{t('addProject.singleText', { path: discovery.path })}</p> : null}
      <div className="dialog-grid"><label>{t('addProject.sourceBranch')}<input value={sourceBranch} onChange={(event) => setSourceBranch(event.target.value)} placeholder={t('addProject.sourceBranchAuto')} /></label><label>{t('addProject.targetBranch')}<input value={baseBranch} onChange={(event) => { setBaseBranch(event.target.value); setMergedBranch(`${event.target.value}-merged`) }} /></label></div>
      <details><summary>{t('addProject.advanced')}</summary><div className="dialog-grid"><label>{t('addProject.mergedBranch')}<input value={mergedBranch} onChange={(event) => setMergedBranch(event.target.value)} /></label></div></details>
      {submitError ? <div className="dialog-error" role="alert">{submitError}</div> : null}
      <div className="dialog-actions"><button type="button" className="button secondary" disabled={busy} onClick={requestClose}>{t('common.cancel')}</button><button type="submit" className="button primary" disabled={busy || !canAdd}>{busy ? t('addProject.adding') : t('addProject.add')}</button></div>
    </form>
  </div>
}
