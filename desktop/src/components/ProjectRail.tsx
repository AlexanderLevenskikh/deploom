import { ChevronRight, FolderPlus, RefreshCw, Search } from 'lucide-react'
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
}

export function ProjectRail({ details, selected, active, onSelectProject, onRefreshAll, onAddProject, onAddWorkspace }: Props) {
  const { t } = useLanguage()
  return (
    <aside className="project-rail">
      <div className="rail-title"><strong>{t('projectRail.projects')}</strong><Search size={17} /></div>
      <div className="project-list">
        {details.projects.map((project) => {
          const level = details.projectLevels[project.name]
          const tone = level?.status === 'red' ? 'danger' : level?.status === 'yellow' ? 'warning' : level?.status === 'green' ? 'success' : 'muted'
          const levelLabel = level?.status === 'red' ? t('levels.red') : level?.status === 'yellow' ? t('levels.yellow') : level?.status === 'green' ? t('levels.green') : t('levels.unknown')
          return (
            <button key={project.name} className={`project-row ${selected?.name === project.name ? 'selected' : ''}`} onClick={() => onSelectProject(project.name)}>
              <span className={`status-dot ${tone}`} title={t('projectRail.currentLevel', { level: levelLabel })} />
              <span className="project-copy"><strong>{project.name}</strong><small>{project.git?.baseBranch || project.git?.sourceBranch || t('projectRail.notConfigured')}</small></span>
              <ChevronRight size={16} />
            </button>
          )
        })}
        {details.projects.length === 0 ? <p className="empty-copy">{t('projectRail.empty', { path: details.workspace.settingsPath })}</p> : null}
      </div>
      <div className="rail-actions"><button className="button secondary wide rail-refresh" disabled={active || details.projects.length === 0} onClick={onRefreshAll}><RefreshCw className={active ? 'spin' : ''} size={16} /> {t('projectRail.refreshAll')}</button><button className="add-workspace" onClick={onAddProject}><FolderPlus size={17} /> {t('projectRail.addProject')}</button><button className="add-workspace subtle" onClick={onAddWorkspace}>{t('projectRail.otherWorkspace')}</button></div>
    </aside>
  )
}
