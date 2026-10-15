import { FolderOpen, FolderPlus, LayoutGrid, RefreshCw, Search, ShieldCheck, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { useLanguage } from '../i18n'
import type { ProjectSpec, WorkspaceDetails } from '../types'

type Props = {
  details: WorkspaceDetails
  selected?: ProjectSpec
  onSelectProject: (name: string) => void
  active: boolean
  onRefreshAll: () => void
  onAddProject: () => void
  onAddWorkspace: () => void
  onRemoveWorkspace: () => void
  onRemoveProject: (name: string) => void
  onAuditProject?: (name: string) => void
  onOpenProject: (path: string) => void
  onOverview: () => void
}

export function ProjectRail({ details, selected, active, onSelectProject, onRefreshAll, onAddProject, onAddWorkspace, onRemoveWorkspace, onRemoveProject, onAuditProject, onOpenProject, onOverview }: Props) {
  const { t } = useLanguage()
  const [query, setQuery] = useState('')

  const requestRemove = (project: ProjectSpec) => {
    if (!window.confirm(t('projectRail.remove.confirm', { name: project.name }))) return
    onRemoveProject(project.name)
  }

  const needle = query.trim().toLowerCase()
  const visible = needle.length === 0
    ? details.projects
    : details.projects.filter((project) => {
        const haystack = `${project.name} ${project.path} ${project.repositoryPath || ''} ${project.git?.baseBranch || ''} ${project.git?.sourceBranch || ''}`.toLowerCase()
        return haystack.includes(needle)
      })

  return (
    <aside className="project-rail">
      <div className="rail-title"><strong>{t('projectRail.projects')}</strong><span className="rail-count">{details.projects.length}</span></div>
      <label className="rail-search"><Search size={13} /><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('projectRail.searchPlaceholder')} aria-label={t('projectRail.searchPlaceholder')} autoComplete="off" /></label>
      <div className="project-list">
        {visible.map((project) => {
          const level = details.projectLevels[project.name]
          const tone = level?.status === 'red' ? 'danger' : level?.status === 'yellow' ? 'warning' : level?.status === 'green' ? 'success' : 'muted'
          const levelLabel = level?.status === 'red' ? t('levels.red') : level?.status === 'yellow' ? t('levels.yellow') : level?.status === 'green' ? t('levels.green') : t('levels.unknown')
          return <div key={project.name} className={`project-row-shell ${selected?.name === project.name ? 'selected' : ''}`}>
            <button className="project-row-main" onClick={() => onSelectProject(project.name)}>
              <span className={`status-dot ${tone}`} title={t('projectRail.currentLevel', { level: levelLabel })} />
              <span className="project-copy"><strong title={project.name}>{project.name}</strong></span>
            </button>
            <div className="project-row-utilities">{onAuditProject ? <button className="project-row-audit" disabled={active} aria-label={t('projectRail.audit.aria', { name: project.name })} title={t('projectRail.audit')} onClick={() => onAuditProject(project.name)}><ShieldCheck size={14} /></button> : null}<button className="project-row-open" aria-label={t('projectRail.openFolder') + ': ' + project.name} title={t('projectRail.openFolder')} onClick={() => onOpenProject(project.path)}><FolderOpen size={14} /></button><button
              className="project-row-remove"
              disabled={active}
              title={t('projectRail.remove.title')}
              aria-label={t('projectRail.remove.aria', { name: project.name })}
              onClick={() => requestRemove(project)}
            >
              <Trash2 size={14} />
            </button></div>
          </div>
        })}
        {details.projects.length === 0 ? <p className="empty-copy">{t('projectRail.empty', { path: details.workspace.settingsPath })}</p> : null}
        {details.projects.length > 0 && visible.length === 0 ? <p className="empty-copy">{t('projectRail.noMatches')}</p> : null}
      </div>
      <div className="rail-actions">
        {/* #5: explicit way back to the workspace overview; only shown while a
            project is selected (in the overview itself there is nothing to
            leave). */}
        {selected ? <button className="button secondary wide" disabled={active} onClick={onOverview}><LayoutGrid size={16} /> {t('projectRail.overview')}</button> : null}
        <details className="workspace-actions-menu"><summary>{t('projectRail.manageWorkspace')}</summary><div>
        <button className="button secondary wide rail-refresh" disabled={active || details.projects.length === 0} onClick={onRefreshAll}><RefreshCw className={active ? 'spin' : ''} size={16} /> {t('projectRail.refreshAll')}</button>
        <button className="button secondary wide" disabled={active} onClick={onAddProject}><FolderPlus size={17} /> {t('projectRail.addProject')}</button>
        <button className="add-workspace subtle" onClick={onAddWorkspace}><FolderPlus size={16} /> {t('projectRail.workspaceAction')}</button>
        <button className="button secondary wide" disabled={active} onClick={() => { if (window.confirm(t('projectRail.removeWorkspace.confirm', { name: details.workspace.name }))) onRemoveWorkspace() }}><Trash2 size={16} /> {t('projectRail.removeWorkspace')}</button>
        </div></details>
      </div>
    </aside>
  )
}
