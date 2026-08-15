import { ChevronRight, FolderPlus, RefreshCw, Search } from 'lucide-react'
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
  return (
    <aside className="project-rail">
      <div className="rail-title"><strong>Проекты</strong><Search size={17} /></div>
      <div className="project-list">
        {details.projects.map((project) => {
          const level = details.projectLevels[project.name]
          const tone = level?.status === 'red' ? 'danger' : level?.status === 'yellow' ? 'warning' : level?.status === 'green' ? 'success' : 'muted'
          const levelLabel = level?.status === 'red' ? 'Красный' : level?.status === 'yellow' ? 'Жёлтый' : level?.status === 'green' ? 'Зелёный' : 'Не рассчитан'
          return (
            <button key={project.name} className={`project-row ${selected?.name === project.name ? 'selected' : ''}`} onClick={() => onSelectProject(project.name)}>
              <span className={`status-dot ${tone}`} title={`Текущий уровень: ${levelLabel}`} />
              <span className="project-copy"><strong>{project.name}</strong><small>{project.git?.baseBranch || project.git?.sourceBranch || 'не настроено'}</small></span>
              <ChevronRight size={16} />
            </button>
          )
        })}
        {details.projects.length === 0 ? <p className="empty-copy">Добавьте проекты в <code>{details.workspace.settingsPath}</code>.</p> : null}
      </div>
      <div className="rail-actions"><button className="button secondary wide rail-refresh" disabled={active || details.projects.length === 0} onClick={onRefreshAll}><RefreshCw className={active ? 'spin' : ''} size={16} /> Актуализировать все</button><button className="add-workspace" onClick={onAddProject}><FolderPlus size={17} /> Добавить проект</button><button className="add-workspace subtle" onClick={onAddWorkspace}>Другой workspace</button></div>
    </aside>
  )
}
